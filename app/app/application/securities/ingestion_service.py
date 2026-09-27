"""按证券代码采集一个批次（F-INFO-01、F-INFO-02）。

顺序执行各采集器。每个请求：取回 → 留存 → 登记，然后才把内容交给采集器解析。
所以即使采集器读不懂响应，原文也已经在对象存储里，能事后查。
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import datetime

from app.application.ports.securities import FetchFailed, IngestionStore, RawFetcher
from app.application.securities.collectors.base import SecurityCollector
from app.domain.securities import (
    ArchivedItem,
    CollectError,
    FetchRequest,
    IngestionNotRunnable,
    IngestionStatus,
    IngestionSummary,
    ItemKind,
    RawResponse,
    SecurityCode,
)

logger = logging.getLogger(__name__)
_MAX_REQUESTS = 400
_MAX_ATTEMPTS = 3
# 偶发的、换个时间再试可能就好的错误。地址不合法、响应过大这类不重试。
_TRANSIENT_FETCH_ERRORS = frozenset(
    {
        "crawl_deadline_exceeded",
        "crawl_transport_failed",
        "crawl_content_encoding_invalid",
        "crawl_dns_failed",
        "crawl_dns_capacity_exhausted",
    }
)
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})


class _Context:
    def __init__(
        self,
        *,
        ingestion_id: str,
        code: SecurityCode,
        fetcher: RawFetcher,
        store: IngestionStore,
        pause: Callable[[], Awaitable[None]],
        backoff: Callable[[int], Awaitable[None]],
    ) -> None:
        self._ingestion_id = ingestion_id
        self._code = code
        self._fetcher = fetcher
        self._store = store
        self._pause = pause
        self._backoff = backoff
        self.items: list[ArchivedItem] = []

    async def fetch(self, request: FetchRequest) -> bytes:
        if len(self.items) >= _MAX_REQUESTS:
            raise CollectError("too_many_requests")
        if self.items:
            await self._pause()  # 对来源网站保持克制
        response, attempts = await self._fetch_with_retry(request)
        if attempts > 1:
            request = replace(request, meta={**request.meta, "attempts": attempts})
        item = await self._store.archive(
            self._ingestion_id,
            code=self._code,
            seq=len(self.items) + 1,
            request=request,
            response=response,
        )
        self.items.append(item)
        if response.status_code >= 400:
            raise CollectError("http_status", str(response.status_code))
        return response.content

    async def _fetch_with_retry(self, request: FetchRequest) -> tuple[RawResponse, int]:
        """最多试三次。只留存最后一次的响应，试了几次记在条目里。"""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            last = attempt == _MAX_ATTEMPTS
            try:
                response = await self._fetcher.fetch(request)
            except FetchFailed as exc:
                if last or exc.code not in _TRANSIENT_FETCH_ERRORS:
                    raise CollectError("fetch_failed", exc.code) from None
            else:
                if last or response.status_code not in _TRANSIENT_STATUS:
                    return response, attempt
            await self._backoff(attempt)
        raise AssertionError("unreachable")  # pragma: no cover


class SecurityIngestionService:
    def __init__(
        self,
        *,
        fetcher: RawFetcher,
        store: IngestionStore,
        collectors: Sequence[SecurityCollector],
        clock: Callable[[], datetime],
        pause: Callable[[], Awaitable[None]],
        backoff: Callable[[int], Awaitable[None]],
    ) -> None:
        if not collectors:
            raise ValueError("at least one collector is required")
        self._fetcher = fetcher
        self._store = store
        self._collectors = list(collectors)
        self._clock = clock
        self._pause = pause
        self._backoff = backoff

    @property
    def sources(self) -> list[str]:
        return [c.source for c in self._collectors]

    async def request(self, raw_code: str) -> str:
        """登记一个待执行的批次。不合法的代码在登记之前就拒绝。"""
        return await self._store.start(SecurityCode(raw_code), sources=self.sources)

    async def ingest(self, raw_code: str) -> IngestionSummary:
        """登记并立即执行（命令行用）。"""
        return await self.run(await self.request(raw_code))

    async def run(self, ingestion_id: str) -> IngestionSummary:
        code = await self._store.begin(ingestion_id)
        if code is None:
            raise IngestionNotRunnable(ingestion_id)
        started = self._clock()
        ctx = _Context(
            ingestion_id=ingestion_id,
            code=code,
            fetcher=self._fetcher,
            store=self._store,
            pause=self._pause,
            backoff=self._backoff,
        )
        error: CollectError | None = None
        for collector in self._collectors:
            try:
                await collector.collect(code, ctx)
            except CollectError as exc:
                error = exc
                logger.warning(
                    "security ingestion stopped code=%s source=%s error=%s",
                    code,
                    collector.source,
                    exc.code,
                )
                break
        status = IngestionStatus.FAILED if error else IngestionStatus.SUCCEEDED
        summary = _summarize(
            ingestion_id=ingestion_id,
            code=code,
            status=status,
            started=started,
            finished=self._clock(),
            items=ctx.items,
            error=error,
        )
        await self._store.finish(
            ingestion_id,
            status=status,
            summary={
                "items": summary.items,
                "new_artifacts": summary.new_artifacts,
                "reused_artifacts": summary.reused_artifacts,
                "bytes_fetched": summary.bytes_fetched,
                "by_source": summary.by_source,
                "report_years": list(summary.report_years),
                "statement_periods": summary.statement_periods,
            },
            error_code=summary.error_code,
            error_detail=summary.error_detail,
        )
        return summary


def _summarize(
    *,
    ingestion_id: str,
    code: SecurityCode,
    status: IngestionStatus,
    started: datetime,
    finished: datetime,
    items: list[ArchivedItem],
    error: CollectError | None,
) -> IngestionSummary:
    periods: dict[str, set[str]] = {}
    for item in items:
        if item.kind is ItemKind.STATEMENT_DATA and item.http_status < 400:
            periods.setdefault(str(item.meta.get("statement")), set()).update(
                str(p) for p in item.meta.get("periods", [])
            )
    years = sorted(
        {
            int(item.meta["fiscal_year"])
            for item in items
            if item.kind is ItemKind.REPORT_FILE and item.http_status < 400
        }
    )
    return IngestionSummary(
        ingestion_id=ingestion_id,
        security_code=code.code,
        status=status,
        started_at=started,
        finished_at=finished,
        items=len(items),
        new_artifacts=sum(1 for i in items if not i.reused),
        reused_artifacts=sum(1 for i in items if i.reused),
        bytes_fetched=sum(i.size_bytes for i in items),
        by_source=dict(Counter(i.source.value for i in items)),
        report_years=tuple(years),
        statement_periods={k: len(v) for k, v in sorted(periods.items())},
        error_code=error.code if error else None,
        error_detail=(error.detail or None) if error else None,
    )
