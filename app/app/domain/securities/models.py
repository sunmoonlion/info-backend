"""证券采集的领域对象（0008-info「段一」）。

一次采集批次由若干请求组成。每个请求的原始响应原样留存（F-INFO-02），解析只为决定
「下一步请求什么」，不在这一层解释业务含义。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

_CODE = re.compile(r"^\d{6}$")


class InvalidSecurityCode(ValueError):
    """代码不是 A 股证券代码。消息可以直接给操作者看。"""


class IngestionNotRunnable(RuntimeError):
    """批次不在待执行状态（不存在、已结束，或上次执行被打断）。"""


class CollectError(RuntimeError):
    """采集不能继续。code 是稳定的错误码，不含地址、参数与响应内容。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


class SourceCode(StrEnum):
    CNINFO = "cninfo"  # 巨潮资讯网：法定信息披露渠道
    EASTMONEY_F10 = "eastmoney-f10"  # 东方财富 F10：第三方网站，仅内部使用


class ItemKind(StrEnum):
    STOCK_LIST = "stock_list"
    ANNOUNCEMENT_QUERY = "announcement_query"
    REPORT_FILE = "report_file"
    COMPANY_TYPE_PAGE = "company_type_page"
    STATEMENT_PERIODS = "statement_periods"
    STATEMENT_DATA = "statement_data"


class IngestionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class SecurityCode:
    """六位 A 股代码。B 股、基金、债券不在第一切口。"""

    code: str

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not _CODE.fullmatch(self.code):
            raise InvalidSecurityCode("证券代码必须是六位数字")
        if self.market is None:
            raise InvalidSecurityCode(f"{self.code} 不是沪深京 A 股代码")

    @property
    def market(self) -> str | None:
        head = self.code[0]
        if self.code.startswith(("900", "200")):  # B 股
            return None
        if head == "6":
            return "SH"
        if head in ("0", "3"):
            return "SZ"
        if head in ("4", "8") or self.code.startswith("92"):
            return "BJ"
        return None

    @property
    def prefixed(self) -> str:
        """带市场前缀，例如 SH600009。"""
        return f"{self.market}{self.code}"

    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True)
class FetchRequest:
    source: SourceCode
    kind: ItemKind
    url: str
    name: str  # 原文的文件名，进对象键
    method: str = "GET"
    params: tuple[tuple[str, str], ...] = ()
    form: tuple[tuple[str, str], ...] | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    max_bytes: int | None = None


@dataclass(frozen=True)
class RawResponse:
    status_code: int
    content_type: str
    content: bytes
    final_url: str
    decoded_from: str | None = None  # 服务器用了压缩时记下原编码；content 是解压后的


@dataclass(frozen=True)
class ArchivedItem:
    """一个请求留存之后的凭据。reused 为真表示内容与已有原文相同，没有新增对象。"""

    seq: int
    source: SourceCode
    kind: ItemKind
    sha256: str
    size_bytes: int
    object_key: str
    reused: bool
    http_status: int
    meta: dict[str, Any]


@dataclass(frozen=True)
class IngestionSummary:
    ingestion_id: str
    security_code: str
    status: IngestionStatus
    started_at: datetime
    finished_at: datetime
    items: int
    new_artifacts: int
    reused_artifacts: int
    bytes_fetched: int
    by_source: dict[str, int]
    report_years: tuple[int, ...]
    statement_periods: dict[str, int]
    error_code: str | None = None
    error_detail: str | None = None
