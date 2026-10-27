"""供给的领域对象（SDD 0045 第二、三节）。

需求只用统一词表说「要什么」，不知道渠道；供给用同一套词表说「能取到什么」，不知道
是谁要的；两边在切合时相遇（第四节）。这一版先做竖线：精确描述 × 目录型渠道（巨潮）×
下载、按页文字、财报抽表 × 交 knowbase 拿回执。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

# —— 需求 ——

ItemStatus = Literal[
    "accepted",  # 已受理
    "fetching",  # 取数中
    "ready",  # 已就绪
    "partial",  # 部分就绪
    "failed",  # 失败
    "unsatisfiable",  # 无法满足
    "cancelled",  # 已取消
    "timeout",  # 超时
]
TERMINAL: frozenset[str] = frozenset(
    {"ready", "partial", "failed", "unsatisfiable", "cancelled", "timeout"}
)
# 只往前走（0045 第 2.5 节）：迟到的事件不能把已取消、超时改成已就绪
_FORWARD: dict[str, frozenset[str]] = {
    "accepted": frozenset(
        {"fetching", "ready", "unsatisfiable", "cancelled", "timeout", "failed"}
    ),
    "fetching": frozenset(
        {"ready", "partial", "failed", "unsatisfiable", "cancelled", "timeout"}
    ),
}
Reading = Literal["original", "text", "pages", "table"]
READINGS: tuple[Reading, ...] = ("original", "text", "pages", "table")
Proposer = Literal["user", "app", "expert", "subscription"]

_SUBJECT = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
MAX_ITEMS = 50


class SupplyError(Exception):
    """拒绝的原因。status 是 HTTP 状态，code 是稳定的错误码。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def refused(code: str, message: str) -> SupplyError:
    return SupplyError(422, code, message)


def can_move(current: str, target: str) -> bool:
    return target in _FORWARD.get(current, frozenset())


@dataclass(frozen=True)
class DescriptionKey:
    """一条精确的描述：主体 × 类型 × 报告期（词表同 knowbase）。"""

    subject: str
    doc_type: str
    period_start: date | None = None
    period_end: date | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "subject": self.subject,
            "doc_type": self.doc_type,
            "period_start": self.period_start.isoformat()
            if self.period_start
            else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
        }

    @staticmethod
    def from_dict(data: dict[str, object]) -> DescriptionKey:
        start, end = data.get("period_start"), data.get("period_end")
        return DescriptionKey(
            subject=str(data["subject"]),
            doc_type=str(data["doc_type"]),
            period_start=date.fromisoformat(str(start)) if start else None,
            period_end=date.fromisoformat(str(end)) if end else None,
        )

    @property
    def text(self) -> str:
        """同一用户同一描述键的未办完请求合流用（0045 第 3.3 节）。"""
        return f"{self.subject}|{self.doc_type}|{self.period_start}|{self.period_end}"


@dataclass(frozen=True)
class DemandItemIn:
    key: DescriptionKey
    readings: tuple[str, ...]
    confirmation_item: str  # 确认里的条目编号（c1、c2……）


@dataclass(frozen=True)
class Demand:
    id: str
    owner: str  # 归属用户：数据给谁、谁确认、配额算谁的（平台账号也是用户）
    proposer: str
    chain: tuple[str, ...]  # 代办链
    app: str  # 提交它的应用，通知发给它
    task_ref: str | None
    confirmation_id: str
    idempotency_key: str
    digest: str
    created_at: datetime


@dataclass(frozen=True)
class DemandItem:
    id: str
    demand_id: str
    key: DescriptionKey
    readings: tuple[str, ...]
    confirmation_item: str
    status: str
    created_at: datetime
    updated_at: datetime
    channel_id: str | None = None
    reason: str | None = None
    receipt: dict[str, object] = field(default_factory=dict)

    @property
    def basis_ref(self) -> str:
        """这一项在 knowbase 记引用用的需求依据编号：确认 × 条目 × 需求项。"""
        return f"demand:{self.demand_id}:{self.confirmation_item}"


def check_demand(
    owner: str,
    proposer: str,
    idempotency_key: str,
    items: list[DemandItemIn],
) -> None:
    if proposer not in ("user", "app", "expert", "subscription"):
        raise refused("proposer_invalid", "proposer is invalid")
    if not owner or len(owner) > 255:
        raise refused("owner_invalid", "owner is invalid")
    if not _IDEMPOTENCY.fullmatch(idempotency_key):
        raise refused("idempotency_key_invalid", "idempotency key is invalid")
    if not 1 <= len(items) <= MAX_ITEMS:
        raise refused("items_invalid", f"give 1 to {MAX_ITEMS} items")
    for item in items:
        if not _SUBJECT.fullmatch(item.key.subject):
            # 竖线先只做 A 股证券代码；机构编号的政策、统计随别的渠道补
            raise refused("subject_unsupported", "only A-share security codes for now")
        if not item.readings or any(r not in READINGS for r in item.readings):
            raise refused("readings_invalid", "readings are original/text/pages/table")


# —— 供给：渠道 ——

ChannelKind = Literal["catalog", "feed", "web", "search"]


@dataclass(frozen=True)
class Channel:
    """来源目录里的一条渠道：能提供什么、怎么取、条款与许可、规矩（0045 第 3.1 节）。"""

    id: str
    name: str
    kind: ChannelKind
    plugin: str
    doc_types: tuple[str, ...]
    readings: tuple[str, ...]
    license: str  # 取来的数据属于哪一档（knowbase 的许可档）
    review: Literal["approved", "pending", "rejected"]
    rate_per_minute: int
    daily_cap: int
    priority: int
    status: Literal["active", "paused"] = "active"
    consecutive_failures: int = 0

    def covers(self, key: DescriptionKey) -> bool:
        return (
            self.status == "active"
            and self.review == "approved"
            and key.doc_type in self.doc_types
        )


@dataclass(frozen=True)
class Entry:
    """渠道里符合描述的一个条目（「列」的结果）。"""

    channel_id: str
    channel_doc_id: str
    title: str
    url: str
    disclosed_on: date | None
    revised: bool = False
    announcement_id: str | None = None  # 渠道里这一次公告的编号（更正版另一个）


# —— 作业 ——

JobKind = Literal["fetch", "deliver"]
JobStatus = Literal["queued", "running", "done", "failed", "waiting"]
MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class Job:
    """跨用户共享的作业：同一渠道同一描述只取一次，挂着等它的需求项。"""

    id: str
    kind: JobKind
    channel_id: str
    dedup_key: str
    payload: dict[str, object]
    status: str
    attempts: int
    created_at: datetime
    tier: int = 1  # 0045 第 3.3 节四档：① 有任务在等数据的
    last_error: str | None = None


@dataclass(frozen=True)
class Original:
    """一个字节不改先存下的原件；交 knowbase 拿到回执后再留 30 天（0045 第 3.4 节）。"""

    id: str
    sha256: str
    channel_id: str
    channel_doc_id: str
    source_url: str
    media_type: str
    size_bytes: int
    object_key: str
    fetched_at: datetime
    delivered_at: datetime | None = None
    purge_after: datetime | None = None
    knowbase_material_id: str | None = None
    knowbase_version_no: int | None = None
