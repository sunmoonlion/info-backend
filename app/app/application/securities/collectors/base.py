from __future__ import annotations

import json
from typing import Any, Protocol

from app.domain.securities import CollectError, FetchRequest, SecurityCode


class CollectContext(Protocol):
    async def fetch(self, request: FetchRequest) -> bytes:
        """取回并留存；返回内容供采集器决定下一步请求。"""
        ...


class SecurityCollector(Protocol):
    source: str

    async def collect(self, code: SecurityCode, ctx: CollectContext) -> None: ...


def parse_json(content: bytes, *, what: str) -> Any:
    """响应不是预期的 JSON 时明确报错。原文在此之前已经留存。"""
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise CollectError("unexpected_response", what) from None


def require(condition: bool, what: str) -> None:
    if not condition:
        raise CollectError("unexpected_response", what)
