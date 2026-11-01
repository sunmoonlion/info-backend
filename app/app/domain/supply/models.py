"""供给的领域对象（SDD 0045 第二、三节）。

需求只用统一词表说「要什么」，不知道渠道；供给用同一套词表说「能取到什么」，不知道
是谁要的；两边在切合时相遇（第四节）。需求项两种写法：精确描述（主体 × 类型 × 期间），
给网址（一页，或加上它直接链到的文件）；订阅、按主题找勾的候选最后也落成这两种。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from app.domain.supply.vocabulary import COLLECTABLE_BY_ORG, subject_kind, types_of

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
# 只往前走（0045 第 2.5 节）：迟到的事件不能把已取消、超时改成已就绪；超时的项准用户撤（三审第 15 条）
_FORWARD: dict[str, frozenset[str]] = {
    "accepted": frozenset(
        {"fetching", "ready", "unsatisfiable", "cancelled", "timeout", "failed"}
    ),
    "fetching": frozenset(
        {"ready", "partial", "failed", "unsatisfiable", "cancelled", "timeout"}
    ),
    "timeout": frozenset({"cancelled"}),
}
Reading = Literal["original", "text", "pages", "table"]
READINGS: tuple[Reading, ...] = ("original", "text", "pages", "table")
Proposer = Literal["user", "app", "expert", "subscription"]

_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
MAX_ITEMS = 50
MAX_URL = 2000
# 给网址加上它直接链到的文件：同一网站、只往下一层，最多这么多份（0045 第 2.2 节）
MAX_LINKED = 20


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
# 给网址（0045 第 2.5 节、第 3.2 节）：网站还在审核，审过就取（需求项停在「已受理」）
REASON_SITE_PENDING = "site_pending"
REASON_ROBOTS = "robots_disallowed"  # 网站的 robots 不让取（渠道登记守 robots 时）
REASON_OFF_SITE = "off_site"  # 网址不在这条渠道登记的网站上
# 渠道要渲染（审核定的），平台的渲染服务没配：不渲染，照实回原因（decisions 10-29 ④）
REASON_RENDER_UNAVAILABLE = "render_unavailable"
REASON_SUBSCRIPTION_ENDED = "subscription_ended"  # 订阅到期、撤了，它生成的在途项取消
REASON_DEADLINE = "deadline_passed"  # 过了「最晚什么时候要」还没办完：超时
REASON_SUBSCRIPTION_EXPIRED = "subscription_expired"  # 订阅到期，它生成的在途项取消
# 为用户采集的原件要走 knowbase「为某用户采集」那条路；它还没上（404、405）就不交付，
# 绝不改走公开原件层（二审第 1 条）
REASON_AWAITING_KNOWBASE = "awaiting_knowbase"
# 法定披露的原件要有主体（knowbase 公开原件层的描述）；渠道配置没写主体的这一项单独失败
REASON_SUBJECT_REQUIRED = "subject_required"
# 网址落在目录型渠道（巨潮这类）的网站上：目录型只按描述取，请按描述要（二审第 4 条）
REASON_CATALOG_SITE = "catalog_site"
REASON_URL_REJECTED = "url_rejected"  # 网址本身不行（解析不了、地址被禁），不算渠道的错
REASON_UNEXPECTED_RESPONSE = (
    "unexpected_response"  # knowbase 回包漏了该受益人的资料（三审第 11 条）
)
ZONES = ("disclosure", "collected")

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
    """一项需求：精确描述给 `key`；给网址给 `url`（`follow_links` 是连它直接链到的文件一起
    取，`prefill` 是用户预填的描述，选填：主体、类型）。两样只给一样。"""

    key: DescriptionKey | None
    readings: tuple[str, ...]
    confirmation_item: str  # 确认里的条目编号（c1、c2……）
    url: str | None = None
    follow_links: bool = False
    prefill: dict[str, object] = field(default_factory=dict)
    declared_public: bool = False  # 用户声明「公开页面、不用登录」（0045 第 2.2 节）


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
    deadline: datetime | None = None  # 最晚什么时候要（亲手提的必填，0045 第 2.2 节）
    note: str | None = None  # 说明（选填）


@dataclass(frozen=True)
class DemandItem:
    id: str
    demand_id: str
    key: DescriptionKey | None
    readings: tuple[str, ...]
    confirmation_item: str
    status: str
    created_at: datetime
    updated_at: datetime
    channel_id: str | None = None
    reason: str | None = None
    receipt: dict[str, object] = field(default_factory=dict)
    tried_channels: tuple[str, ...] = ()  # 试过、取不到的渠道（换下一个时跳过）
    url: str | None = None  # 给网址的那一项
    follow_links: bool = False
    prefill: dict[str, object] = field(default_factory=dict)
    retry_of: str | None = None  # 「再试一次」挂在哪一项下（0045 第 2.5 节）
    declared_public: bool = False

    @property
    def target_text(self) -> str:
        """合流、去重用：描述键，或网址。"""
        if self.key is not None:
            return self.key.text
        return f"url:{self.url}"

    @property
    def basis_ref(self) -> str:
        """这一项在 knowbase 记引用用的需求依据编号：确认 × 条目 × 需求项。"""
        return f"demand:{self.demand_id}:{self.confirmation_item}"


def basis_of(demand: Demand, item: DemandItem) -> dict[str, object]:
    """受益人各自的合规依据（三审第 3 条）：普通确认单带 confirmation_id + item_id，
    订阅生成的单带 subscription_id + revision。"""
    if demand.confirmation_id.startswith("subscription:"):
        raw = demand.confirmation_id.removeprefix("subscription:")
        sub_id, _, rev = raw.partition("@")
        return {
            "subscription_id": sub_id,
            "revision": int(rev) if rev.isdigit() else 1,
        }
    return {
        "confirmation_id": demand.confirmation_id,
        "item_id": item.confirmation_item,
    }


class Reference(tuple):
    """公开原件的引用记录：保留 (subject, ref) 二元组形状，同时挂上该受益人自己的依据。"""

    basis: dict[str, object]

    def __new__(
        cls, subject: str, ref: str, basis: dict[str, object] | None = None
    ) -> Reference:
        instance = super().__new__(cls, (subject, ref))
        instance.basis = dict(basis or {})
        return instance


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
        if (item.key is None) == (item.url is None):
            raise refused("item_invalid", "give either a description or a URL")
        if item.key is not None:
            check_subject(item.key.subject, item.key.doc_type)
        elif not item.url or len(item.url) > MAX_URL:
            raise refused("url_invalid", f"a URL of at most {MAX_URL} characters")
        if not item.readings or any(r not in READINGS for r in item.readings):
            raise refused("readings_invalid", "readings are original/text/pages/table")


def check_subject(subject: str, doc_type: str) -> None:
    """主体按 knowbase 词表认（`subject_kinds`）：证券代码，或机构编号。机构编号只配政策
    法规、统计数据这类（词表 `collectable.org`），上市公司的公告不挂在机构名下。"""
    kind = subject_kind(subject)
    if kind is None:
        raise refused("subject_unsupported", "subject is not a known code")
    if kind.startswith("org_") and not set(types_of(doc_type)) & set(
        COLLECTABLE_BY_ORG
    ):
        raise refused(
            "subject_type_mismatch", "an org subject goes with policy or statistics"
        )


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
    # 审核定的「区」（0045 第 3.5 节、0043 第 2.1、2.4 节）：`disclosure` 是法定披露（交易所、
    # 监管、政府公报一类），进公开原件层；`collected` 是为用户采集，每位受益人各得一份自己
    # 名下的资料。网站审过不等于页面是法定披露，所以默认是为用户采集
    zone: str = "collected"
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
            and (self.review == "approved" or self.status_reason == "re_review")
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
    subject: str | None = None  # 订阅源、网页的主体（渠道登记写的，或用户预填的）

    def as_dict(self) -> dict[str, object]:
        """进作业的载荷、订阅的发现记录（订阅每轮已知条目，作业不必再「列」）。"""
        return {
            "channel_id": self.channel_id,
            "channel_doc_id": self.channel_doc_id,
            "title": self.title,
            "url": self.url,
            "disclosed_on": self.disclosed_on.isoformat()
            if self.disclosed_on
            else None,
            "revised": self.revised,
            "announcement_id": self.announcement_id,
            "doc_type": self.doc_type,
            "edition": self.edition,
            "topics": list(self.topics),
            "subject": self.subject,
        }

    @staticmethod
    def from_dict(data: dict[str, object]) -> Entry:
        disclosed = data.get("disclosed_on")
        topics = data.get("topics") or ()
        return Entry(
            channel_id=str(data["channel_id"]),
            channel_doc_id=str(data["channel_doc_id"]),
            title=str(data.get("title") or ""),
            url=str(data.get("url") or ""),
            disclosed_on=date.fromisoformat(str(disclosed)) if disclosed else None,
            revised=bool(data.get("revised")),
            announcement_id=str(data["announcement_id"])
            if data.get("announcement_id")
            else None,
            doc_type=str(data.get("doc_type") or ""),
            edition=str(data.get("edition") or "full"),
            topics=tuple(str(t) for t in topics),  # type: ignore[union-attr]
            subject=str(data["subject"]) if data.get("subject") else None,
        )


# —— 作业 ——

# 取数（列、取、交付、加工）；加工（上传资料的加工，不问渠道，排在加工队列里）；
# 看新的（订阅的一轮查看，跨订阅共享发现游标）；搜（按主题找、按主题订阅的一轮）
JobKind = Literal["fetch", "process", "watch", "search"]
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
    """一个字节不改先存下的原件；只管内容与留存（0045 第 3.4 节，三审第 1 条）。"""

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
    purged_at: datetime | None = None  # 内容已删，只留出处与摘要


@dataclass(frozen=True)
class OriginalDelivery:
    """原文交付记录（三审第 1 条）：按（渠道，文档编号，区，归属）单记，不跨区共用。"""

    id: str
    original_id: str
    channel_id: str
    channel_doc_id: str
    zone: str
    owner_subject: str
    knowbase_material_id: str
    knowbase_version_no: int
    readings: tuple[str, ...]
    delivered_at: datetime
