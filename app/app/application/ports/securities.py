"""证券采集的端口。应用层只依赖这里的协议，具体实现在基础设施层，由入口处组装。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from app.domain.securities import (
    ArchivedItem,
    FetchRequest,
    IngestionStatus,
    RawResponse,
    SecurityCode,
)


class FetchFailed(RuntimeError):
    """取数失败。code 是稳定错误码（来自取数边界），不含地址与参数。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RawFetcher(Protocol):
    async def fetch(self, request: FetchRequest) -> RawResponse:
        """按请求取回原始响应；失败抛 FetchFailed。"""
        ...


class IngestionStore(Protocol):
    async def start(self, code: SecurityCode, *, sources: list[str]) -> str:
        """登记一个待执行的批次，返回批次标识。"""
        ...

    async def begin(self, ingestion_id: str) -> SecurityCode | None:
        """把待执行的批次置为执行中并返回它的证券代码。

        批次不存在或已结束时返回 None。批次已在执行中（上次执行被打断后重新投递）时，
        把它记为失败（interrupted）并返回 None：已留存的原文不动，不自动重跑。
        """
        ...

    async def archive(
        self,
        ingestion_id: str,
        *,
        code: SecurityCode,
        seq: int,
        request: FetchRequest,
        response: RawResponse,
    ) -> ArchivedItem:
        """留存原始响应并登记；内容已存在时只登记引用。"""
        ...

    async def finish(
        self,
        ingestion_id: str,
        *,
        status: IngestionStatus,
        summary: Mapping[str, Any],
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> None: ...
