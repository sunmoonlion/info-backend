"""供给的适配器：渠道插件用的 Http（沿用取数边界）、原件的对象存储、按页文字。

- Http：只连公网、限大小、限重定向、解压有上限（`crawl_http.py`），插件不自带网络客户端；
  对方 5xx、超时、取数失败记成可重试，4xx 记成不重试。
- 限速（`LimitedHttp`）：插件按渠道建，它的 Http 每问一次渠道先要一个名额（SDD 0045
  第 3.1、3.3 节，`application/supply/scheduler.py` 的 `ChannelLimiter`），取文件算一份。
- 按页文字：pdfplumber 一页一页取，取完即释放；页数、大小有上限。
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from urllib.parse import unquote

from app.application.ports.securities import FetchFailed
from app.application.ports.supply import Page
from app.application.supply.cninfo import Http
from app.application.supply.scheduler import ChannelLimiter
from app.domain.securities import FetchRequest, ItemKind, SourceCode
from app.domain.supply.catalog import on_site, site_of
from app.domain.supply.models import Channel, SupplyError
from app.infrastructure.securities.fetcher import CrawlHttpFetcher
from app.infrastructure.storage.object_storage import ObjectStorage

logging.getLogger("pdfminer").setLevel(logging.ERROR)

MAX_PDF_BYTES = 200 * 1024 * 1024
MAX_PAGES = 1500


class CrawlHttp:
    def __init__(self, fetcher: CrawlHttpFetcher) -> None:
        self._fetcher = fetcher

    async def post_form(self, url: str, form: tuple[tuple[str, str], ...]) -> bytes:
        return await self._get(
            FetchRequest(
                source=SourceCode.CNINFO,
                kind=ItemKind.ANNOUNCEMENT_QUERY,
                url=url,
                name="query.json",
                method="POST",
                form=form,
            )
        )

    async def get(self, url: str) -> bytes:
        return await self._get(
            FetchRequest(
                source=SourceCode.CNINFO, kind=ItemKind.STOCK_LIST, url=url, name="list"
            )
        )

    async def download(
        self, url: str, *, max_bytes: int, timeout_seconds: float
    ) -> tuple[bytes, str]:
        request = FetchRequest(
            source=SourceCode.CNINFO,
            kind=ItemKind.REPORT_FILE,
            url=url,
            name="report.pdf",
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
        )
        try:
            response = await self._fetcher.fetch(request)
        except FetchFailed as exc:
            raise SupplyError(503, f"fetch_failed:{exc.code}", "fetch failed") from None
        _check(response.status_code)
        return response.content, response.content_type

    async def page(
        self,
        url: str,
        *,
        max_bytes: int,
        timeout_seconds: float,
        fetch: bool,
        site: str | None = None,
    ) -> Page:
        """通用网页、订阅源、站内搜索：对方回什么状态都给出来，由适配器判定。

        `site` 给了就只许在这个网站里跳转：跳到别的网站的那一跳当场停，不发出去（二审第
        16 条）。网址本身的毛病（解析不了、地址被禁、协议端口不对、跳出网站）是「这一条
        不行」（422），不算渠道的错，不计连续失败（二审第 3 条）；连不上、超时才算。"""
        del fetch  # 限速在 LimitedHttp 那一层算
        request = FetchRequest(
            source=SourceCode.CNINFO,  # 取数边界只用它记来源，与渠道无关
            kind=ItemKind.REPORT_FILE,
            url=url,
            name="page",
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
        )
        allow = (lambda target: _on_site(target, site)) if site else None
        try:
            response = await self._fetcher.fetch(request, allow_redirect=allow)
        except FetchFailed as exc:
            if exc.code == "crawl_redirect_off_site":
                raise SupplyError(422, "off_site", "redirected off the site") from None
            if exc.code.startswith(_URL_PROBLEMS):
                raise SupplyError(422, "url_rejected", exc.code) from None
            raise SupplyError(503, f"fetch_failed:{exc.code}", "fetch failed") from None
        return Page(
            status=response.status_code,
            media_type=response.content_type,
            content=response.content,
            final_url=response.final_url,
            filename=_disposition_filename(response.content_disposition),
        )

    async def _get(self, request: FetchRequest) -> bytes:
        try:
            response = await self._fetcher.fetch(request)
        except FetchFailed as exc:
            raise SupplyError(503, f"fetch_failed:{exc.code}", "fetch failed") from None
        _check(response.status_code)
        return response.content


class LimitedHttp:
    """一条渠道的插件用的 Http：每问一次先经限速；取文件（`download`）算日上限的一份。"""

    def __init__(self, http: Http, limiter: ChannelLimiter, channel: Channel) -> None:
        self._http = http
        self._limiter = limiter
        self._channel = channel

    async def post_form(self, url: str, form: tuple[tuple[str, str], ...]) -> bytes:
        await self._limiter.acquire(self._channel, fetch=False)
        return await self._http.post_form(url, form)

    async def get(self, url: str) -> bytes:
        await self._limiter.acquire(self._channel, fetch=False)
        return await self._http.get(url)

    async def download(
        self, url: str, *, max_bytes: int, timeout_seconds: float
    ) -> tuple[bytes, str]:
        await self._limiter.acquire(self._channel, fetch=True)
        return await self._http.download(
            url, max_bytes=max_bytes, timeout_seconds=timeout_seconds
        )

    async def page(
        self,
        url: str,
        *,
        max_bytes: int,
        timeout_seconds: float,
        fetch: bool,
        site: str | None = None,
    ) -> Page:
        await self._limiter.acquire(self._channel, fetch=fetch)
        return await self._http.page(  # type: ignore[attr-defined]
            url,
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
            fetch=fetch,
            site=site,
        )


# 网址本身的确定性毛病（取数边界的错误码）：这一条不行，不是渠道的错；
# DNS 临时解析失败、容量耗尽、不可达等算 fetch_failed 走重试（三审第 6 条）
_URL_PROBLEMS = (
    "crawl_url_",
    "crawl_dns_invalid",
    "crawl_address_forbidden",
    "crawl_tls_downgrade_forbidden",
    "crawl_tls_",
)
_FILENAME_STAR = re.compile(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;\s]+)")
_FILENAME_PLAIN = re.compile(r'filename\s*=\s*"?([^";]+)"?')


def _disposition_filename(header: str | None) -> str | None:
    if not header:
        return None
    star = _FILENAME_STAR.search(header)
    if star:
        name = unquote(star.group(1)).strip()
        if name:
            return name
    plain = _FILENAME_PLAIN.search(header)
    if plain:
        name = unquote(plain.group(1)).strip()
        if name:
            return name
    return None


def _on_site(url: str, domain: str) -> bool:
    try:
        return on_site(site_of(url), domain)
    except SupplyError:
        return False


def _check(status: int) -> None:
    if status == 429:
        raise SupplyError(429, "channel_rate_limited", "the channel asked to slow down")
    if status >= 500:
        raise SupplyError(503, "channel_unavailable", "the channel is unavailable")
    if status >= 400:
        raise SupplyError(422, "channel_refused", f"the channel answered {status}")


class StorageObjects:
    """原件放 knowfeed 自己的桶（交付后留 30 天，到期删内容）。"""

    def __init__(self, storage: ObjectStorage) -> None:
        self._storage = storage

    async def put(self, key: str, content: bytes, media_type: str) -> None:
        await asyncio.to_thread(
            self._storage.put_bytes,
            object_key=key,
            data=content,
            content_type=media_type,
        )

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(
            self._storage.get_bytes, object_key=key, max_bytes=MAX_PDF_BYTES
        )

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._storage.delete_all_versions, object_key=key)


class PdfPlumberPages:
    def text_pages(self, pdf: bytes) -> list[tuple[int, str]]:
        import pdfplumber  # noqa: PLC0415 - 只有加工时才需要

        if not pdf.startswith(b"%PDF-") or len(pdf) > MAX_PDF_BYTES:
            raise ValueError("not a readable PDF")
        out: list[tuple[int, str]] = []
        with pdfplumber.open(io.BytesIO(pdf)) as document:
            for index, page in enumerate(document.pages[:MAX_PAGES]):
                out.append((index + 1, (page.extract_text() or "").strip()))
                page.flush_cache()
        return out
