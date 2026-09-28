from __future__ import annotations

import asyncio
import gzip
import subprocess
import sys
import zlib
from collections.abc import AsyncIterator

import httpx
import pytest

from app.infrastructure.external import crawl_http


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], delay: float = 0):
        self.chunks = chunks
        self.delay = delay
        self.reads = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.fixture
def network(monkeypatch):
    requests = []
    clients = []
    responses = []
    resolutions = []
    addresses = ["93.184.216.34"]
    original_client = httpx.AsyncClient

    async def resolve(host, port):
        resolutions.append((host, port))
        return addresses

    async def handler(request):
        requests.append(request)
        return responses.pop(0)

    def client(**kwargs):
        clients.append(kwargs)
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(crawl_http, "_resolve", resolve)
    monkeypatch.setattr(crawl_http.httpx, "AsyncClient", client)
    return requests, responses, resolutions, addresses, clients


async def fetch(url="https://example.org/path", **kwargs):
    return await crawl_http.fetch_crawl_url(
        url,
        timeout_seconds=kwargs.pop("timeout_seconds", 1),
        max_bytes=kwargs.pop("max_bytes", 20),
        **kwargs,
    )


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.org/x",
        "http://user:secret@example.org/",
        "https://example.org:8080/",
        "http://example.org:443/",
        "http:///a",
        "https://example.org\\@127.0.0.1/",
        "https://example.org/\nfoo",
        "http://[fe80::1%25eth0]/",
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://169.254.169.254/",
        "http://100.64.0.1/",
        "http://0.0.0.0/",
        "http://224.0.0.1/",
        "http://[::1]/",
        "http://[fc00::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[64:ff9b::7f00:1]/",
        "http://[2002:7f00:1::]/",
    ],
)
async def test_rejects_forbidden_url_without_connection(network, url):
    with pytest.raises(crawl_http.CrawlFetchError):
        await fetch(url)
    assert network[0] == []


@pytest.mark.parametrize(
    "answers",
    [[], ["127.0.0.1"], ["::1"], ["93.184.216.34", "10.0.0.1"], ["not-an-address"]],
)
async def test_all_dns_answers_must_be_public(network, answers):
    network[3][:] = answers
    with pytest.raises(crawl_http.CrawlFetchError):
        await fetch()
    assert network[0] == []


async def test_pins_dns_preserves_tls_host_and_returns_logical_url(network):
    body = Body([b"hello"])
    network[1].append(httpx.Response(200, stream=body))
    response = await fetch(params={"page": "1"})
    request = network[0][0]
    assert str(request.url) == "https://93.184.216.34/path?page=1"
    assert request.headers["host"] == "example.org"
    assert request.extensions["sni_hostname"] == "example.org"
    assert request.headers["accept-encoding"] == "identity"
    assert network[2] == [("example.org", 443)]
    assert network[4][0]["trust_env"] is False
    assert network[4][0]["follow_redirects"] is False
    assert network[4][0].get("verify", True) is True
    assert str(response.url) == "https://example.org/path?page=1"
    assert response.content == b"hello" and body.closed


async def test_rebinding_during_redirect_cannot_connect_to_second_answer(
    network, monkeypatch
):
    calls = 0

    async def resolve(host, port):
        nonlocal calls
        calls += 1
        return ["93.184.216.34"] if calls == 1 else ["127.0.0.1"]

    monkeypatch.setattr(crawl_http, "_resolve", resolve)
    network[1].append(
        httpx.Response(302, headers={"location": "/next"}, stream=Body([]))
    )
    with pytest.raises(crawl_http.CrawlFetchError, match="address_forbidden"):
        await fetch()
    assert calls == 2 and len(network[0]) == 1


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/",
        "https://10.0.0.1/",
        "http://example.org/",
        "file:///etc/passwd",
        "https://secret@other.org/",
        "https://other.org:8443/",
        "https://other.org/\tbad",
    ],
)
async def test_redirect_policy_is_rechecked(network, location):
    body = Body([b"must not read redirect body"])
    network[1].append(httpx.Response(302, headers={"location": location}, stream=body))
    with pytest.raises(crawl_http.CrawlFetchError):
        await fetch()
    assert len(network[0]) == 1
    assert body.reads == 0 and body.closed


async def test_cross_origin_drops_all_private_headers_and_cookies(network):
    network[1].extend(
        [
            httpx.Response(
                302,
                headers={
                    "location": "https://other.org/final",
                    "set-cookie": "s=secret",
                },
                stream=Body([]),
            ),
            httpx.Response(200, stream=Body([b"ok"])),
        ]
    )
    response = await fetch(
        headers={
            "Authorization": "Bearer secret",
            "Cookie": "private=1",
            "X-Api-Key": "secret",
            "User-Agent": "InfoBot",
        }
    )
    assert response.content == b"ok"
    assert network[0][0].headers["authorization"] == "Bearer secret"
    second = network[0][1]
    assert second.headers["host"] == "other.org"
    assert second.extensions["sni_hostname"] == "other.org"
    assert second.headers["user-agent"] == "InfoBot"
    assert not {"authorization", "cookie", "x-api-key"} & set(second.headers)
    assert len(network[4]) == 2


async def test_relative_redirect_preserves_same_origin_auth(network):
    network[1].extend(
        [
            httpx.Response(307, headers={"location": "next"}, stream=Body([])),
            httpx.Response(200, stream=Body([b"ok"])),
        ]
    )
    response = await fetch(headers={"Authorization": "Bearer secret"})
    assert response.url == httpx.URL("https://example.org/next")
    assert network[0][1].headers["authorization"] == "Bearer secret"


async def test_redirect_limit(network):
    network[1].extend(
        httpx.Response(302, headers={"location": "/again"}, stream=Body([]))
        for _ in range(3)
    )
    with pytest.raises(crawl_http.CrawlFetchError, match="redirect_limit"):
        await fetch(max_redirects=2)
    assert len(network[0]) == 3


@pytest.mark.parametrize(
    "headers,code",
    [
        ({"Content-Length": "21"}, "too_large"),
        ({"Content-Length": "-1"}, "length_invalid"),
        ({"Content-Length": "1, 1"}, "length_invalid"),
        ({"Content-Encoding": "gzip"}, "encoding_forbidden"),
    ],
)
async def test_rejects_headers_before_body_read(network, headers, code):
    body = Body([b"payload"])
    network[1].append(httpx.Response(200, headers=headers, stream=body))
    with pytest.raises(crawl_http.CrawlFetchError, match=code):
        await fetch()
    assert body.reads == 0 and body.closed


async def test_stream_limit_without_content_length_closes_early(network):
    body = Body([b"12345", b"67890", b"unread"])
    network[1].append(httpx.Response(200, stream=body))
    with pytest.raises(crawl_http.CrawlFetchError, match="too_large"):
        await fetch(max_bytes=9)
    assert body.reads == 2 and body.closed


async def test_exact_limit_is_accepted(network):
    network[1].append(httpx.Response(200, stream=Body([b"123", b"45"])))
    assert (await fetch(max_bytes=5)).content == b"12345"


async def test_total_deadline_stops_slow_stream(network):
    body = Body([b"a"] * 50, delay=0.01)
    network[1].append(httpx.Response(200, stream=body))
    with pytest.raises(crawl_http.CrawlFetchError, match="deadline_exceeded"):
        await fetch(timeout_seconds=0.03)
    assert body.closed and body.reads < 50


async def test_total_deadline_covers_dns(network, monkeypatch):
    async def stalled(host, port):
        await asyncio.sleep(10)
        return ["93.184.216.34"]

    monkeypatch.setattr(crawl_http, "_resolve", stalled)
    with pytest.raises(crawl_http.CrawlFetchError, match="deadline_exceeded"):
        await fetch(timeout_seconds=0.01)
    assert network[0] == []


@pytest.mark.parametrize(
    "header", ["Host", "Proxy-Authorization", "Connection", "Transfer-Encoding"]
)
async def test_caller_cannot_override_routing_headers(network, header):
    with pytest.raises(crawl_http.CrawlFetchError, match="header_forbidden"):
        await fetch(headers={header: "secret"})
    assert network[0] == []


@pytest.mark.parametrize("limit", [0, -1, float("inf"), float("nan")])
async def test_invalid_limits(network, limit):
    with pytest.raises(crawl_http.CrawlFetchError, match="limits_invalid"):
        await fetch(timeout_seconds=limit)


async def test_discovery_collectors_use_same_fetch_boundary(network):
    from app.application.collectors.api import ApiCollectorAdapter
    from app.application.collectors.rss import RssCollectorAdapter

    for adapter in (ApiCollectorAdapter(), RssCollectorAdapter()):
        with pytest.raises(crawl_http.CrawlFetchError, match="address_forbidden"):
            await adapter.discover(url="http://127.0.0.1/feed", config={})
    assert network[0] == []


def test_document_crawler_uses_shared_info_fetch_boundary():
    import inspect

    from app.application.services.info_crawl_service import (
        _process_admitted_crawl_job,
        process_crawl_job,
    )

    entry = inspect.getsource(process_crawl_job)
    assert "async with crawl_source_slot(" in entry
    assert "await _process_admitted_crawl_job(" in entry
    source = inspect.getsource(_process_admitted_crawl_job)
    assert "await fetch_crawl_url(" in source
    assert "AsyncClient(" not in source
    assert "await response.aread()" not in source


def test_blocked_system_dns_does_not_hold_asyncio_run_shutdown():
    # A real subprocess exit tests more than cancelling a mocked coroutine.
    code = """
import asyncio, socket, time
from app.infrastructure.external.crawl_http import fetch_crawl_url, CrawlFetchError
def stalled(*args, **kwargs):
    time.sleep(30)
    return []
socket.getaddrinfo = stalled
try:
    asyncio.run(fetch_crawl_url('https://example.org/', timeout_seconds=0.03, max_bytes=10))
except CrawlFetchError as exc:
    assert str(exc) == 'crawl_deadline_exceeded'
    print('bounded-exit')
"""
    process = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=3
    )
    assert process.returncode == 0, process.stderr
    assert process.stdout.strip() == "bounded-exit"


async def test_resolver_capacity_is_bounded(monkeypatch):
    import threading

    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(crawl_http, "_DNS_SLOTS", slots)
    with pytest.raises(crawl_http.CrawlFetchError, match="capacity_exhausted"):
        await crawl_http._resolve("example.org", 443)
    slots.release()


async def test_post_sends_form_once_with_the_same_boundary(network):
    network[1].append(httpx.Response(200, stream=Body([b"{}"])))
    response = await fetch(
        "http://example.org/query",
        method="POST",
        form={"stock": "600009,gssh0600009", "category": "年报"},
    )
    request = network[0][0]
    assert request.method == "POST"
    assert str(request.url) == "http://93.184.216.34/query"
    assert request.headers["host"] == "example.org"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert request.headers["accept-encoding"] == "identity"
    assert request.content == (
        b"stock=600009%2Cgssh0600009&category=%E5%B9%B4%E6%8A%A5"
    )
    assert response.request.method == "POST"
    assert str(response.url) == "http://example.org/query"


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_post_is_never_redirected(network, status):
    network[1].append(
        httpx.Response(status, headers={"location": "https://example.org/elsewhere"})
    )
    with pytest.raises(crawl_http.CrawlFetchError, match="crawl_redirect_forbidden"):
        await fetch(method="POST", form={"a": "b"})
    assert len(network[0]) == 1


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"method": "PUT"}, "crawl_method_forbidden"),
        ({"method": "DELETE"}, "crawl_method_forbidden"),
        ({"method": "get"}, "crawl_method_forbidden"),
        ({"form": {"a": "b"}}, "crawl_body_forbidden"),
        ({"method": "POST", "form": {"a": "x" * 20000}}, "crawl_body_too_large"),
    ],
)
async def test_method_and_body_are_bounded_before_any_connection(network, kwargs, code):
    with pytest.raises(crawl_http.CrawlFetchError, match=code):
        await fetch(**kwargs)
    assert network[0] == [] and network[2] == []


async def test_post_response_limits_still_apply(network):
    network[1].append(httpx.Response(200, stream=Body([b"x" * 21])))
    with pytest.raises(crawl_http.CrawlFetchError, match="crawl_response_too_large"):
        await fetch(method="POST", form={"a": "b"})


def encoded(body: bytes, encoding: str, chunk: int = 7) -> httpx.Response:
    data = gzip.compress(body) if encoding == "gzip" else zlib.compress(body)
    return httpx.Response(
        200,
        headers={"content-encoding": encoding, "content-type": "application/json"},
        stream=Body([data[i : i + chunk] for i in range(0, len(data), chunk)]),
    )


@pytest.mark.parametrize("encoding", ["gzip", "x-gzip", "deflate", "GZIP"])
async def test_opted_in_caller_gets_the_inflated_body(network, encoding):
    body = b'{"data": [1, 2, 3]}'
    network[1].append(
        encoded(body, "gzip" if "gzip" in encoding.lower() else "deflate")
    )
    network[1][0].headers["content-encoding"] = encoding
    response = await fetch(decode=True, max_bytes=64)
    assert response.content == body
    assert "content-encoding" not in response.headers
    assert response.headers["x-crawl-decoded-from"] == encoding.lower()
    assert response.headers["content-type"] == "application/json"
    assert network[0][0].headers["accept-encoding"] == "identity"


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
async def test_compressed_answers_are_still_refused_unless_opted_in(network, encoding):
    network[1].append(encoded(b"{}", encoding))
    with pytest.raises(
        crawl_http.CrawlFetchError, match="crawl_content_encoding_forbidden"
    ):
        await fetch()


@pytest.mark.parametrize(
    "encoding", ["br", "zstd", "compress", "gzip, br", "gzip,gzip"]
)
async def test_other_encodings_are_refused_even_when_opted_in(network, encoding):
    network[1].append(
        httpx.Response(200, headers={"content-encoding": encoding}, stream=Body([b"x"]))
    )
    with pytest.raises(
        crawl_http.CrawlFetchError, match="crawl_content_encoding_forbidden"
    ):
        await fetch(decode=True)


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
async def test_a_bomb_is_stopped_at_the_limit_without_being_inflated(network, encoding):
    bomb = b"0" * (50 * 1024 * 1024)  # 压缩后只有几万字节
    response = encoded(bomb, encoding, chunk=4096)
    body = response.stream
    network[1].append(response)
    with pytest.raises(crawl_http.CrawlFetchError, match="crawl_response_too_large"):
        await fetch(decode=True, max_bytes=100_000)
    assert body.reads <= 2 and body.closed  # 第一两块就超限，后面的不再读


async def test_inflated_size_exactly_at_the_limit_is_accepted(network):
    body = b"x" * 1000
    network[1].append(encoded(body, "gzip"))
    assert (await fetch(decode=True, max_bytes=1000)).content == body
    network[1].append(encoded(body + b"y", "gzip"))
    with pytest.raises(crawl_http.CrawlFetchError, match="crawl_response_too_large"):
        await fetch(decode=True, max_bytes=1000)


async def test_compressed_input_is_bounded_too(network):
    import os

    noise = os.urandom(4000)  # 压不小
    network[1].append(encoded(noise, "gzip", chunk=500))
    with pytest.raises(crawl_http.CrawlFetchError, match="crawl_response_too_large"):
        await fetch(decode=True, max_bytes=3000)


@pytest.mark.parametrize(
    "damage",
    [
        lambda d: d[:-5],  # 截断
        lambda d: d[:10] + b"\x00" * 10 + d[20:],  # 中段损坏
        lambda d: b"not gzip at all",
        lambda d: d + b"trailing",  # 结束后还有多余内容
        lambda d: b"",
    ],
)
async def test_damaged_compressed_body_is_refused(network, damage):
    data = damage(gzip.compress(b'{"data": "value"}' * 20))
    network[1].append(
        httpx.Response(200, headers={"content-encoding": "gzip"}, stream=Body([data]))
    )
    with pytest.raises(
        crawl_http.CrawlFetchError, match="crawl_content_encoding_invalid"
    ):
        await fetch(decode=True, max_bytes=10_000)


async def test_identity_answers_are_untouched_when_opted_in(network):
    network[1].append(httpx.Response(200, stream=Body([b"plain"])))
    response = await fetch(decode=True)
    assert response.content == b"plain"
    assert "x-crawl-decoded-from" not in response.headers


# ---------------------------------------------------------------- 证券采集的取数适配


async def test_a_request_may_ask_for_more_than_the_default_up_to_the_ceiling(
    monkeypatch,
):
    """年报比网页大得多：采集器可以为它要更大的上限，但不超过封顶值。"""
    from app.domain.securities import FetchRequest, ItemKind, SourceCode
    from app.infrastructure.securities import fetcher as module

    seen: list[tuple[int, float]] = []

    async def fake(url, *, timeout_seconds, max_bytes, **kwargs):
        seen.append((max_bytes, timeout_seconds))
        return httpx.Response(200, content=b"ok", request=httpx.Request("GET", url))

    monkeypatch.setattr(module, "fetch_crawl_url", fake)

    def request(**kwargs):
        return FetchRequest(
            source=SourceCode.CNINFO,
            kind=ItemKind.REPORT_FILE,
            url="http://static.cninfo.com.cn/finalpage/2026-03-28/1.PDF",
            name="a.pdf",
            **kwargs,
        )

    capped = module.CrawlHttpFetcher(
        timeout_seconds=20,
        max_bytes=10,
        user_agent="t",
        max_bytes_ceiling=100,
        timeout_ceiling_seconds=180,
    )
    await capped.fetch(request())
    await capped.fetch(request(max_bytes=64, timeout_seconds=90))
    await capped.fetch(request(max_bytes=1000, timeout_seconds=900))
    await capped.fetch(request(max_bytes=5, timeout_seconds=3))
    assert seen == [(10, 20), (64, 90), (100, 180), (5, 3)]

    seen.clear()
    plain = module.CrawlHttpFetcher(timeout_seconds=20, max_bytes=10, user_agent="t")
    await plain.fetch(request(max_bytes=64, timeout_seconds=90))
    assert seen == [(10, 20)]  # 没给封顶值：不许超过默认值，与以前一样

    seen.clear()
    odd = module.CrawlHttpFetcher(
        timeout_seconds=20,
        max_bytes=10,
        user_agent="t",
        max_bytes_ceiling=4,
        timeout_ceiling_seconds=1,
    )
    await odd.fetch(request())
    assert seen == [(10, 20)]  # 封顶值比默认值还小时，以默认值为准
