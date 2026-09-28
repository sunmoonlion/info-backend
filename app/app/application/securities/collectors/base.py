from __future__ import annotations

import json
from typing import Any, Protocol

from app.domain.securities import CollectError, FetchRequest, SecurityCode


class CollectContext(Protocol):
    async def fetch(self, request: FetchRequest) -> bytes:
        """取回并留存；返回内容供采集器决定下一步请求。"""
        ...

    async def fetch_optional(self, request: FetchRequest) -> bytes | None:
        """同 fetch，但这一条取不到不让批次失败：记为跳过，返回 None。

        只容忍「这一条」的问题（太大、超时、对方返回错误状态）。
        请求数超限这类批次级的问题照常抛出。
        """
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
