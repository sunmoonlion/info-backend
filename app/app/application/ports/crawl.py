"""采集器要外界做的事：按地址取回一份有上限的响应。

实现是加固过的取数边界（`app/infrastructure/external/crawl_http.py`）：
只许公网地址、跳转共用一个期限、响应大小封顶。应用层不自己发请求。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol


class FetchedResponse(Protocol):
    @property
    def text(self) -> str: ...

    def json(self) -> Any: ...

    def raise_for_status(self) -> Any:
        """状态码不是成功就抛错。"""
        ...


class CrawlFetch(Protocol):
    async def __call__(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> FetchedResponse: ...
