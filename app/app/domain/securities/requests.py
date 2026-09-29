"""采集申请的领域对象与规则（0008-info-intake）。不依赖框架、数据库与网络。

一个申请对应一家公司的一次采集请求，可以有多个申请人。批准之前的状态存在申请上；
批准之后的进度从采集批次与数据集推出来，到了终点才记回申请（`outcome`），之后不再变。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from app.domain.cross_app import Origin

MAX_REASON_CHARS = 500
DEFAULT_MAX_OPEN_REQUESTS = 5
SYSTEM_WATCHLIST = "system:watchlist"


class RequestError(Exception):
    """申请办不成。code 是稳定错误码；消息可以直接给用户看。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class RequestKind(StrEnum):
    INITIAL = "initial"  # 这家公司还没有数据
    REFRESH = "refresh"  # 已有数据，申请更新


class RequestStatus(StrEnum):
    """存在申请上的状态：只到「批没批」为止。"""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class Progress(StrEnum):
    """给用户看的进度。批准之后的几个是推出来的。"""

    PENDING = "pending"  # 已提交，等批准
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    QUEUED = "queued"  # 已批准，排队中
    COLLECTING = "collecting"
    BUILDING = "building"
    REGISTERING = "registering"
    AVAILABLE = "available"
    QUALITY_FAILED = "quality_failed"  # 采到了，没通过质量检查，不发布
    FAILED = "failed"  # 采集或建库出错
    REGISTRATION_FAILED = "registration_failed"  # 建好了，没能交给查询服务


FINAL: frozenset[Progress] = frozenset(
    {
        Progress.REJECTED,
        Progress.WITHDRAWN,
        Progress.AVAILABLE,
        Progress.QUALITY_FAILED,
        Progress.FAILED,
        Progress.REGISTRATION_FAILED,
    }
)


def clean_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    text = reason.strip()
    if not text:
        return None
    if len(text) > MAX_REASON_CHARS:
        raise RequestError("reason_too_long", f"申请理由最多 {MAX_REASON_CHARS} 个字")
    if any(ord(c) < 32 and c not in "\n\t" for c in text):
        raise RequestError("reason_invalid", "申请理由里有不能显示的字符")
    return text


@dataclass(frozen=True)
class IngestionState:
    """采集批次走到哪了。"""

    status: str  # pending / running / succeeded / failed
    error_code: str | None = None
    build_error: str | None = None  # 建库被拒绝的原因（原文不足以建库）


@dataclass(frozen=True)
class DatasetState:
    """这个批次建出的数据集。"""

    status: str  # published / quality_failed
    dataset_id: str
    data_version: str
    start_date: str
    end_date: str
    registered: bool
    registration_error: str | None = None


def progress_after_approval(
    ingestion: IngestionState | None,
    dataset: DatasetState | None,
    *,
    registration_enabled: bool,
) -> Progress:
    """批准之后的进度。所有入口都用这一个函数，不各自判断。"""
    if ingestion is None or ingestion.status == "pending":
        return Progress.QUEUED
    if ingestion.status == "running":
        return Progress.COLLECTING
    if ingestion.status == "failed":
        return Progress.FAILED
    if dataset is None:
        return Progress.FAILED if ingestion.build_error else Progress.BUILDING
    if dataset.status != "published":
        return Progress.QUALITY_FAILED
    if not registration_enabled or dataset.registered:
        return Progress.AVAILABLE
    if dataset.registration_error:
        return Progress.REGISTRATION_FAILED
    return Progress.REGISTERING


@dataclass(frozen=True)
class Requester:
    actor_id: str
    reason: str | None
    origin: Origin | None
    requested_at: datetime
    withdrawn_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.withdrawn_at is None


@dataclass(frozen=True)
class SecurityRequest:
    request_id: str
    security_code: str
    kind: RequestKind
    status: RequestStatus
    open: bool
    created_at: datetime
    requesters: tuple[Requester, ...] = field(default_factory=tuple)
    ingestion_id: str | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None
    outcome: Progress | None = None
    closed_at: datetime | None = None

    def requester(self, actor_id: str) -> Requester | None:
        for one in self.requesters:
            if one.actor_id == actor_id:
                return one
        return None

    @property
    def active_requesters(self) -> int:
        return sum(1 for one in self.requesters if one.active)

    def can_withdraw(self, actor_id: str) -> bool:
        """批准之前才能撤回：批准之后采集已经排上了。"""
        mine = self.requester(actor_id)
        return (
            self.open
            and self.status is RequestStatus.PENDING
            and mine is not None
            and mine.active
        )
