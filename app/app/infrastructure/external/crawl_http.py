"""Bounded, public-internet-only HTTP for Info discovery and document crawling.

Not a transport for trusted internal APIs, object storage or model providers.
Resolve each hop once and connect to the validated numeric address, preserving
Host and TLS SNI through HTTPX's documented sni_hostname extension. No ambient
proxy, netrc, cookies between hops, caller-supplied Host, or unbounded decoding.
"""

from __future__ import annotations

import asyncio
import ipaddress
import math
import socket
import threading
from collections.abc import Mapping

import httpx

_REDIRECTS = {301, 302, 303, 307, 308}
_CROSS_ORIGIN_HEADERS = {"user-agent", "accept", "accept-language"}
_FORBIDDEN_HEADERS = {
    "host",
    "connection",
    "proxy-authorization",
    "proxy-connection",
    "content-length",
    "transfer-encoding",
    "upgrade",
    "te",
    "trailer",
}
_TRANSLATION_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
    ipaddress.ip_network("2002::/16"),
    ipaddress.ip_network("2001::/32"),
)
# libc DNS is not cancellable. Bound outstanding lookups and do not attach them
# to asyncio.run()'s default executor, whose shutdown would wait after timeout.
_DNS_SLOTS = threading.BoundedSemaphore(4)


class CrawlFetchError(ValueError):
    """Stable error code without URL, query, headers or provider credentials."""


def _url(value: str) -> httpx.URL:
    if any(ord(char) <= 32 or ord(char) == 127 for char in value) or "\\" in value:
        raise CrawlFetchError("crawl_url_invalid")
    try:
        url = httpx.URL(value)
    except (httpx.InvalidURL, ValueError):
        raise CrawlFetchError("crawl_url_invalid") from None
    if (
        url.scheme not in {"http", "https"}
        or not url.host
        or url.userinfo
        or url.port not in {None, 80 if url.scheme == "http" else 443}
        or "%" in url.host
    ):
        raise CrawlFetchError("crawl_url_forbidden")
    return url.copy_with(fragment=None)


def _public_ip(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise CrawlFetchError("crawl_dns_invalid") from None
    if (
        not address.is_global
        or address.is_multicast
        or address.is_reserved
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or (isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped)
        or any(address in network for network in _TRANSLATION_NETWORKS)
    ):
        raise CrawlFetchError("crawl_address_forbidden")
    return str(address)


async def _resolve(host: str, port: int) -> list[str]:
    if not _DNS_SLOTS.acquire(blocking=False):
        raise CrawlFetchError("crawl_dns_capacity_exhausted")
    loop = asyncio.get_running_loop()
    result: asyncio.Future[list[str]] = loop.create_future()

    def deliver(addresses: list[str], failed: bool):
        if result.done():
            return
        if failed:
            result.set_exception(CrawlFetchError("crawl_dns_failed"))
        else:
            result.set_result(addresses)

    def lookup():
        addresses: list[str] = []
        failed = False
        try:
            answers = socket.getaddrinfo(
                host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
            )
            addresses = list(dict.fromkeys(str(answer[4][0]) for answer in answers))
        except OSError:
            failed = True
        finally:
            _DNS_SLOTS.release()
        try:
            loop.call_soon_threadsafe(deliver, addresses, failed)
        except RuntimeError:
            pass  # Caller timed out and its event loop has already closed.

    try:
        threading.Thread(target=lookup, name="info-crawl-dns", daemon=True).start()
    except RuntimeError:
        _DNS_SLOTS.release()
        raise CrawlFetchError("crawl_dns_unavailable") from None
    return await result


async def _target(url: httpx.URL) -> str:
    host = url.raw_host.decode("ascii")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        try:
            answers = await _resolve(
                host, url.port or (443 if url.scheme == "https" else 80)
            )
        except OSError:
            raise CrawlFetchError("crawl_dns_failed") from None
        if not answers:
            raise CrawlFetchError("crawl_dns_empty") from None
        # Reject mixed public/private answers, not merely the selected answer.
        return [_public_ip(answer) for answer in answers][0]
    return _public_ip(host)


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    return url.scheme, url.host, url.port


async def fetch_crawl_url(
    url: str,
    *,
    timeout_seconds: float,
    max_bytes: int,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, str] | None = None,
    max_redirects: int = 5,
) -> httpx.Response:
    """Fetch a bounded identity-encoded response; redirects share one deadline.

    A fresh client per hop prevents credential/cookie/connection reuse across
    origins (including different TLS hostnames sharing the same pinned IP).
    Responses that ignore Accept-Encoding: identity fail closed rather than
    expanding an attacker-controlled compressed body in memory.
    """
    if (
        not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or max_bytes <= 0
        or max_redirects < 0
    ):
        raise CrawlFetchError("crawl_limits_invalid")
    current = _url(url)
    if params:
        current = current.copy_merge_params(params)
    request_headers = httpx.Headers(headers)
    if any(key in _FORBIDDEN_HEADERS for key in request_headers):
        raise CrawlFetchError("crawl_header_forbidden")
    request_headers["Accept-Encoding"] = "identity"
    try:
        async with asyncio.timeout(timeout_seconds):
            for hop in range(max_redirects + 1):
                address = await _target(current)
                hop_headers = httpx.Headers(request_headers)
                hop_headers["Host"] = current.netloc.decode("ascii")
                async with httpx.AsyncClient(
                    timeout=timeout_seconds,
                    trust_env=False,
                    follow_redirects=False,
                ) as client:
                    async with client.stream(
                        "GET",
                        current.copy_with(host=address),
                        headers=hop_headers,
                        extensions={"sni_hostname": current.raw_host.decode("ascii")},
                    ) as response:
                        if response.status_code in _REDIRECTS:
                            location = response.headers.get("location")
                            if not location or hop == max_redirects:
                                raise CrawlFetchError("crawl_redirect_limit_or_missing")
                            # Validate raw Location too: URL parsers may normalize controls.
                            if (
                                any(ord(c) <= 32 or ord(c) == 127 for c in location)
                                or "\\" in location
                            ):
                                raise CrawlFetchError("crawl_redirect_invalid")
                            following = _url(str(current.join(location)))
                            if (
                                current.scheme == "https"
                                and following.scheme != "https"
                            ):
                                raise CrawlFetchError("crawl_tls_downgrade_forbidden")
                            if _origin(current) != _origin(following):
                                request_headers = httpx.Headers(
                                    {
                                        key: value
                                        for key, value in request_headers.items()
                                        if key in _CROSS_ORIGIN_HEADERS
                                    }
                                )
                                request_headers["Accept-Encoding"] = "identity"
                            current = following
                            continue
                        if (
                            response.headers.get("content-encoding", "identity").lower()
                            != "identity"
                        ):
                            raise CrawlFetchError("crawl_content_encoding_forbidden")
                        length = response.headers.get("content-length")
                        if length is not None:
                            if not length.isascii() or not length.isdecimal():
                                raise CrawlFetchError("crawl_content_length_invalid")
                            if len(length) > 20 or int(length) > max_bytes:
                                raise CrawlFetchError("crawl_response_too_large")
                        content = bytearray()
                        async for chunk in response.aiter_raw():
                            if len(chunk) > max_bytes - len(content):
                                raise CrawlFetchError("crawl_response_too_large")
                            content.extend(chunk)
                        return httpx.Response(
                            response.status_code,
                            headers=response.headers,
                            content=bytes(content),
                            request=httpx.Request("GET", current),
                        )
    except TimeoutError:
        raise CrawlFetchError("crawl_deadline_exceeded") from None
    except (httpx.HTTPError, httpx.InvalidURL):
        raise CrawlFetchError("crawl_transport_failed") from None
    raise CrawlFetchError("crawl_redirect_limit_or_missing")
