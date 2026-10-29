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

from app.domain.supply.vocabulary import types_of

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


# 需求项结束时的原因（稳定的编码，给应用与管理端认；0045 第 2.5 节、第四节）。
# 「无法满足」分几种：这类数据还取不到、渠道里没有这一份、网站审不过或没人审、渠道停用。
REASON_NO_CHANNEL = "no_channel"  # 这类数据我们还取不到（来源目录里没有能供的渠道）
REASON_CHANNEL_HAS_NONE = "channel_has_none"  # 渠道里没有这一份
REASON_CHANNEL_DISABLED = "channel_disabled"  # 渠道停用（审核撤销）
REASON_SITE_REJECTED = "site_rejected"  # 网站审不过
REASON_SITE_EXPIRED = "site_review_expired"  # 网站待审超过时限没人审
REASON_READINGS_INCOMPLETE = "readings_incomplete"  # 原件到了，要的读法没齐
REASON_CANCELLED = "cancelled"
# 能供这类的渠道都不满足确认时的供给条件（许可档、来源限制）：用户调整用途或确认新条件
REASON_CONDITIONS_UNMET = "conditions_unmet"
REASON_NO_ONE_WAITING = "no_one_waiting"  # 等这个作业的需求项都撤了，作业取消

# 缺口清单按哪种「无法满足」分组（0045 第四节「缺口清单」）：
# uncovered 是「这类数据我们还取不到」，是补渠道的主要依据；其余几种一并列出，便于判断
GAP_KINDS: dict[str, str] = {
    REASON_NO_CHANNEL: "uncovered",
    REASON_CHANNEL_DISABLED: "uncovered",
    REASON_CHANNEL_HAS_NONE: "absent",
    REASON_SITE_REJECTED: "site",
    REASON_SITE_EXPIRED: "site",
}


def summarize(statuses: list[str] | set[str]) -> str:
    """需求单状态由各项汇总（0045 第 2.5 节）：还有没结束的就是那个状态；都就绪才
    算就绪；有就绪或部分就绪的算部分就绪；否则取各项里排在前面的那个终态。"""
    found = set(statuses)
    for open_status in ("fetching", "accepted"):
        if open_status in found:
            return open_status
    if found == {"ready"}:
        return "ready"
    if "ready" in found or "partial" in found:
        return "partial"
    return sorted(found)[0] if found else "accepted"


@dataclass(frozen=True)
class DescriptionKey:
    """一条描述（词表同 knowbase，SDD 0049）：主体 × 类型 × 期间 × 版本形态，外加缩小
    范围的事项、行业。类型可以是细类或大类；定期报告的期间是报告期（一条对一份），
    其余类型的期间是披露时间窗（一条对窗里的一批）。缺省值不写进字典。"""

    subject: str
    doc_type: str
    period_start: date | None = None
    period_end: date | None = None
    edition: str = "full"
    topics: tuple[str, ...] = ()
    industry: str | None = None

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "subject": self.subject,
            "doc_type": self.doc_type,
            "period_start": self.period_start.isoformat()
            if self.period_start
            else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
        }
        if self.edition != "full":
            out["edition"] = self.edition
        if self.topics:
            out["topics"] = list(self.topics)
        if self.industry is not None:
            out["industry"] = self.industry
        return out

    @staticmethod
    def from_dict(data: dict[str, object]) -> DescriptionKey:
        start, end = data.get("period_start"), data.get("period_end")
        topics = data.get("topics") or ()
        return DescriptionKey(
            subject=str(data["subject"]),
            doc_type=str(data["doc_type"]),
            period_start=date.fromisoformat(str(start)) if start else None,
            period_end=date.fromisoformat(str(end)) if end else None,
            edition=str(data.get("edition") or "full"),
            topics=tuple(str(t) for t in topics),  # type: ignore[union-attr]
            industry=str(data["industry"]) if data.get("industry") else None,
        )

    @property
    def text(self) -> str:
        """同一用户同一描述键的未办完请求合流用（0045 第 3.3 节）。"""
        extra = ""
        if self.edition != "full" or self.topics or self.industry:
            extra = f"|{self.edition}|{','.join(sorted(self.topics))}|{self.industry}"
        return (
            f"{self.subject}|{self.doc_type}|{self.period_start}|{self.period_end}"
            + extra
        )


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
    # 确认时的供给条件里 knowfeed 核的那几样（`schedule.Conditions.as_dict`）
    conditions: dict[str, object] = field(default_factory=dict)


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
    tried_channels: tuple[str, ...] = ()  # 试过、取不到的渠道（换下一个时跳过）

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
# 在用；暂停（连续失败自动暂停或手动暂停，作业等着、恢复后接着跑）；停用（审核撤销，
# 作业取消、在等的需求项转「无法满足」，0045 第 3.1、3.3 节）
ChannelStatus = Literal["active", "paused", "disabled"]


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
    status: ChannelStatus = "active"
    consecutive_failures: int = 0
    max_running: int = 1  # 同时最多跑几个作业（登记的并发）
    # 通用网页按网站审：一个网站（域名）一条渠道；目录型渠道也可以写它所在的网站，
    # 用户给的网址落在这个网站上就照这条渠道走（0045 第四节「给网址」）
    domain: str | None = None
    terms_url: str | None = None
    render_allowed: bool = False  # 能否用无头浏览器渲染（审核时定）
    obey_robots: bool = True  # 通用网页型默认守 robots，审核可逐渠道改
    status_reason: str | None = None
    config: dict[str, object] = field(default_factory=dict)  # 适配器配置
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def covers(self, key: DescriptionKey) -> bool:
        """渠道能供这一条：审过、没停用，大类看它下面有没有这个渠道登记的细类。

        暂停的渠道照样接：作业等着，恢复后接着跑，不让需求因为一时取不到变成缺口。
        配置里写了 `markets`（如互动易只有深市公司）的，主体的市场要在里面。"""
        markets = self.config.get("markets")
        if isinstance(markets, list) and markets:
            if key.subject.rsplit(".", 1)[-1] not in markets:
                return False
        return (
            self.status in ("active", "paused")
            and self.review == "approved"
            and any(t in self.doc_types for t in types_of(key.doc_type))
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
    # 插件认出的描述（SDD 0049）：细类、版本形态、事项；交 knowbase 时就这么写
    doc_type: str = ""
    edition: str = "full"
    topics: tuple[str, ...] = ()


# —— 作业 ——

# 取数（列、取、交付、加工）；加工（上传资料的加工，不问渠道，排在加工队列里）
JobKind = Literal["fetch", "process"]
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
    tier: int = 1  # 0045 第 3.3 节四档（`schedule.py`）
    last_error: str | None = None
    not_before: datetime | None = None  # 等重试、日上限用完时，这之前不排


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
    purged_at: datetime | None = None  # 内容已删，只留出处与摘要
