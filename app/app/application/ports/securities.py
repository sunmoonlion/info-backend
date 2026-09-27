"""证券采集的端口。应用层只依赖这里的协议，具体实现在基础设施层，由入口处组装。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from app.domain.securities import (
    ArchivedItem,
    FetchRequest,
    IngestionStatus,
    RawResponse,
    SecurityCode,
)
from app.domain.securities.dataset import BuiltDataset, ReportPage
from app.domain.securities.registration import DatasetRecord


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


@dataclass(frozen=True)
class BatchItem:
    seq: int
    source: str
    kind: str
    sha256: str
    http_status: int
    meta: Mapping[str, Any]
    locator: Any  # 留存实现自己用来找回原文的凭据，应用层不解读


@dataclass(frozen=True)
class LoadedBatch:
    ingestion_id: str
    code: SecurityCode
    status: str
    items: tuple[BatchItem, ...]


class BatchReader(Protocol):
    async def load(self, ingestion_id: str) -> LoadedBatch | None: ...

    async def latest_succeeded(self, code: SecurityCode) -> str | None: ...

    async def read(self, item: BatchItem) -> bytes:
        """读回原文并核对校验值；对不上要报错，不能返回内容。"""
        ...


class ReportReader(Protocol):
    def extract_pages(self, pdf: bytes, *, max_pages: int) -> list[ReportPage]:
        """抽出报告前若干页的文字与表格。读不了就抛 DatasetBuildError。"""
        ...


class DatasetStore(Protocol):
    async def save(self, dataset: BuiltDataset, *, ingestion_id: str) -> str:
        """留存数据集文件、清单与质量报告，并登记。同一版本重复保存是幂等的。"""
        ...


class DatasetRecords(Protocol):
    async def get(self, record_id: str) -> DatasetRecord | None: ...

    async def latest_published(self, code: SecurityCode) -> DatasetRecord | None:
        """该代码最近建成且通过质量检查的数据集版本。"""
        ...

    async def mark_registered(self, record_id: str) -> None:
        """记下向下游登记成功的时间，并清掉上次的失败原因。"""
        ...

    async def mark_registration_failed(self, record_id: str, code: str) -> None:
        """记下最近一次登记失败的错误码；不改动上次成功的时间。"""
        ...


class DatasetRegistrar(Protocol):
    @property
    def configured(self) -> bool:
        """下游的地址与服务身份是否都配了。"""
        ...

    async def register(self, dataset: DatasetRecord) -> None:
        """向下游登记这个版本并使它成为现行版本；不成功抛 RegistrationError。

        下游对同一版本的重复登记是幂等的。
        """
        ...
