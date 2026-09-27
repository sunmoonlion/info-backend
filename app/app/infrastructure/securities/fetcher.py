"""证券采集的取数适配：沿用 info 的取数边界（只连公网、限大小、限重定向）。"""

from __future__ import annotations

from app.application.ports.securities import FetchFailed
from app.domain.securities import FetchRequest, RawResponse
from app.infrastructure.external.crawl_http import CrawlFetchError, fetch_crawl_url


class CrawlHttpFetcher:
    def __init__(
        self, *, timeout_seconds: float, max_bytes: int, user_agent: str
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._max_bytes = max_bytes
        self._user_agent = user_agent

    async def fetch(self, request: FetchRequest) -> RawResponse:
        try:
            response = await fetch_crawl_url(
                request.url,
                timeout_seconds=self._timeout_seconds,
                max_bytes=min(request.max_bytes or self._max_bytes, self._max_bytes),
                headers={"User-Agent": self._user_agent},
                params=dict(request.params) if request.params else None,
                method=request.method,
                form=dict(request.form) if request.form is not None else None,
                # 来源网站有时无视「不要压缩」的请求头（2026-09-27 实测）；
                # 解压有上限，超限即止
                decode=True,
            )
        except CrawlFetchError as exc:
            raise FetchFailed(str(exc)) from None
        return RawResponse(
            status_code=response.status_code,
            content_type=response.headers.get(
                "content-type", "application/octet-stream"
            ),
            content=response.content,
            final_url=str(response.url),
            decoded_from=response.headers.get("x-crawl-decoded-from"),
        )
