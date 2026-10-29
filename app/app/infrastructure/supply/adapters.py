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

from app.application.ports.securities import FetchFailed
from app.application.supply.cninfo import Http
from app.application.supply.scheduler import ChannelLimiter
from app.domain.securities import FetchRequest, ItemKind, SourceCode
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
