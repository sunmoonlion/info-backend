"""订阅与按主题找的规矩（SDD 0045 第 2.2、2.5 节，第四节，第七节块 9）。

- 订阅三种：按描述订（主体 × 类型，从订阅那天起往后新发的）、按网址订（栏目页、RSS 源）、
  按主题订（定期重新找，每轮候选要用户勾）。必须有截止日，最长一年，可续；续订 = 范围同意
  出新修订。
- 每条订阅各有进度（共用的只是渠道的发现游标）：新加入的只收起始日之后的；当月超额的记
  「本月超额未取」，下月初在下月配额里优先补取，用户可选跳过；暂停后从自己的进度恢复。
- 查的频率取用户希望的与渠道允许的最快里慢的那个（第 3.3 节）。
- 提前 14 天提醒续订；到期不续，它生成的在途需求项标「已取消（订阅到期）」。
- 按主题订阅、按主题找的每轮候选 7 天内不勾作废。

几个上限 0045 没写，这里取拟值，交回报告里请 Opus 定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

from app.domain.supply.catalog import site_of
from app.domain.supply.models import READINGS, check_subject, refused
from app.domain.supply.vocabulary import DOC_TYPE_CLASS, types_of

SubscriptionBy = Literal["description", "url", "topic"]
SubscriptionStatus = Literal["active", "paused", "expired", "cancelled", "disabled"]
LIVE: frozenset[str] = frozenset({"active", "paused"})

MAX_SPAN = timedelta(days=366)  # 最长一年（闰年那一天也算进去）
RENEW_NOTICE = timedelta(days=14)
CANDIDATES_VALID = timedelta(days=7)
# 拟值：一条订阅最多几个对象（主体 × 类型、网址）、多久查一次的范围、每月上限的范围
MAX_TARGETS = 50
MAX_URLS = 20
MIN_EVERY_HOURS = 1
MAX_EVERY_HOURS = 24 * 30
MAX_MONTHLY_CAP = 1000
# 渠道登记里没写 `min_check_hours` 的，最快每小时查一次
DEFAULT_CHANNEL_MIN_HOURS = 1
# 当月超额未取的最多记这么多条（再多的就不记了，下月照常从新的开始）
MAX_BACKLOG = 500
# 按主题找：一次最多要几个候选
MAX_CANDIDATES = 50


@dataclass(frozen=True)
class Subscription:
    id: str
    owner: str
    by: str
    readings: tuple[str, ...]
    every_hours: int
    starts_on: date
    ends_on: date
    monthly_cap: int
    status: str
    revision: int
    purpose: str
    created_at: datetime
    updated_at: datetime
    subjects: tuple[str, ...] = ()
    doc_types: tuple[str, ...] = ()
    urls: tuple[str, ...] = ()
    topic: str | None = None
    status_reason: str | None = None
    skip_backlog: bool = False  # 当月超额的下月不补
    next_check_at: datetime | None = None
    last_checked_at: datetime | None = None
    round_id: str | None = None  # 正在跑的这一轮（看新的作业还没跑完）
    channels: dict[str, str] = field(default_factory=dict)  # 对象 → 执行的渠道

    @property
    def consent(self) -> str:
        """knowbase 里的长期范围同意：`subscription:<订阅>@<修订>`（0043 第 2.2 节）。"""
        return f"subscription:{self.id}@{self.revision}"

    def targets(self) -> list[str]:
        """各订阅对象：按描述订是「主体|类型」，按网址订是网址，按主题订是话题。"""
        if self.by == "description":
            return [f"{s}|{t}" for s in self.subjects for t in self.doc_types]
        if self.by == "url":
            return list(self.urls)
        return [self.topic or ""]

    def renew_due(self, today: date) -> bool:
        return self.status in LIVE and self.ends_on - today <= RENEW_NOTICE

    def scope(self) -> dict[str, object]:
        """在 knowbase 留的长期范围同意写什么（knowbase `SubscriptionScopeIn`）。"""
        return {
            "by": self.by,
            "starts_on": self.starts_on.isoformat(),
            "ends_on": self.ends_on.isoformat(),
            "purpose": self.purpose,
            "subjects": list(self.subjects),
            "doc_types": list(self.doc_types),
            "urls": list(self.urls),
            "topic": self.topic,
        }


def every(subscription_hours: int, channel_config: dict[str, object]) -> timedelta:
    """查的频率：用户希望的与渠道允许的最快里慢的那个。"""
    floor = channel_config.get("min_check_hours", DEFAULT_CHANNEL_MIN_HOURS)
    if not isinstance(floor, int) or floor < MIN_EVERY_HOURS:
        floor = DEFAULT_CHANNEL_MIN_HOURS
    return timedelta(hours=max(subscription_hours, floor))


def month_key(now: datetime, zone) -> str:
    local = now.astimezone(zone)
    return f"{local.year:04d}-{local.month:02d}"


def check_subscription(
    *,
    by: str,
    subjects: tuple[str, ...],
    doc_types: tuple[str, ...],
    urls: tuple[str, ...],
    topic: str | None,
    readings: tuple[str, ...],
    starts_on: date,
    ends_on: date,
    every_hours: int,
    monthly_cap: int,
    purpose: str,
    today: date,
) -> None:
    if by not in ("description", "url", "topic"):
        raise refused("by_invalid", "subscribe by description, url or topic")
    if starts_on < today:
        raise refused("starts_on_invalid", "a subscription starts today or later")
    if ends_on <= starts_on or ends_on - starts_on > MAX_SPAN:
        raise refused("ends_on_invalid", "give an end date within one year")
    if not MIN_EVERY_HOURS <= every_hours <= MAX_EVERY_HOURS:
        raise refused(
            "every_invalid", f"check every {MIN_EVERY_HOURS} to {MAX_EVERY_HOURS} hours"
        )
    if not 1 <= monthly_cap <= MAX_MONTHLY_CAP:
        raise refused("monthly_cap_invalid", f"monthly cap is 1 to {MAX_MONTHLY_CAP}")
    if not purpose.strip() or len(purpose) > 200:
        raise refused("purpose_invalid", "say what it is for, at most 200 characters")
    if not readings or any(r not in READINGS for r in readings):
        raise refused("readings_invalid", "readings are original/text/pages/table")
    if by == "description":
        if not subjects or not doc_types:
            raise refused("targets_invalid", "give subjects and types")
        if len(subjects) * len(doc_types) > MAX_TARGETS:
            raise refused(
                "targets_invalid", f"at most {MAX_TARGETS} subject-type pairs"
            )
        for doc_type in doc_types:
            if doc_type not in DOC_TYPE_CLASS and not types_of(doc_type):
                raise refused("doc_type_unknown", f"not in the vocabulary: {doc_type}")
            for subject in subjects:
                check_subject(subject, doc_type)
    elif by == "url":
        if not 1 <= len(urls) <= MAX_URLS:
            raise refused("targets_invalid", f"give 1 to {MAX_URLS} URLs")
        for url in urls:
            site_of(url)
    elif not topic or not topic.strip() or len(topic) > 200:
        raise refused("topic_invalid", "give a topic of at most 200 characters")


@dataclass(frozen=True)
class Round:
    """订阅的一轮：查的时间、发现几份、取了几份、因上限没取几份（0045 第 2.5 节）。"""

    id: str
    subscription_id: str
    started_at: datetime
    finished_at: datetime | None = None
    found: int = 0
    taken: int = 0
    over_cap: int = 0
    demand_id: str | None = None
    note: str | None = None


SearchStatus = Literal["searching", "ready", "ended"]


@dataclass(frozen=True)
class Search:
    """按主题找（0045 第 2.2 节、第四节）：正在找 → 候选已出（7 天）→ 已结束。

    第一版只在用户指定的网站与审核过的站内搜索渠道里搜（所有者 10-26 定，通用搜索接口
    等律师意见）。候选按搜索给的原始顺序，不重排、不评级；用户勾的再确认才去取。"""

    id: str
    owner: str
    topic: str
    sites: tuple[str, ...]
    wanted: int
    status: str
    created_at: datetime
    candidates: tuple[dict[str, object], ...] = ()
    expires_at: datetime | None = None
    subscription_id: str | None = None  # 按主题订阅的一轮
    note: str | None = None


def check_search(topic: str, sites: tuple[str, ...], wanted: int) -> tuple[str, ...]:
    if not topic.strip() or len(topic) > 200:
        raise refused("topic_invalid", "give a topic of at most 200 characters")
    if not 1 <= wanted <= MAX_CANDIDATES:
        raise refused("wanted_invalid", f"ask for 1 to {MAX_CANDIDATES} candidates")
    if len(sites) > MAX_URLS:
        raise refused("sites_invalid", f"at most {MAX_URLS} sites")
    cleaned = []
    for site in sites:
        value = site.strip().lower()
        cleaned.append(site_of(value if "://" in value else f"https://{value}/"))
    return tuple(dict.fromkeys(cleaned))
