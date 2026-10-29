"""来源目录的规矩（SDD 0045 第 3.1 节、第七节块 2）：渠道的登记、审核与试采、在用与
停用、先后、连续失败自动暂停、按网站审与待审网站。

- 加渠道是登记一条记录，不改代码；审核就是第一道关，每个渠道审一次，不逐条审。
- 放行（审过）之前要对一条样例描述试采一次「列」和「取」，看得到结果才能放行。
- 渠道连续失败 N 次自动暂停并告警，不让需求堆成失败。
- 通用网页按网站（域名）审：用户给的网址落在没登记的网站上，自动建一条「待审」渠道；
  待审有时限，N 天没人审，在等它的需求项转「无法满足」。

几个数 0045 只写了「N」，这里取的拟值列在下面，交回报告里请 Opus 定。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit

from app.domain.supply.models import SupplyError, refused

# 拟值（0045 只写了 N）：连续失败几次自动暂停；待审网站几天没人审就转「无法满足」；
# 待审网站多少个、最久等了多久就在管理端告警
AUTO_PAUSE_AFTER = 5
SITE_REVIEW_WITHIN = timedelta(days=7)
SITE_ALERT_COUNT = 10
SITE_ALERT_WAIT = timedelta(days=2)

KINDS = ("catalog", "feed", "web", "search")
LICENSES = ("self", "via_expert", "sale")  # knowbase 的许可档
_CHANNEL_ID = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
_HOST = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
MAX_RATE_PER_MINUTE = 600
MAX_DAILY_CAP = 100_000
MAX_RUNNING = 10  # 同时最多跑几个作业的上限（出站是家庭宽带，不宜多开）

StatusAction = Literal["pause", "resume", "disable", "enable"]
# 在用、暂停、停用之间怎么走；停用后再启用要审过
_STATUS_MOVES: dict[str, dict[str, str]] = {
    "pause": {"active": "paused"},
    "resume": {"paused": "active"},
    "disable": {"active": "disabled", "paused": "disabled"},
    "enable": {"disabled": "active"},
}


def site_of(url: str) -> str:
    """网址落在哪个网站：小写主机名，去掉端口与结尾的点。不是 http、https 的不认。"""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        raise refused("url_invalid", "the URL is invalid") from None
    host = (parts.hostname or "").rstrip(".").lower()
    if parts.scheme not in ("http", "https") or not _HOST.fullmatch(host):
        raise refused("url_invalid", "only public http(s) URLs with a host name")
    return host


def on_site(host: str, domain: str) -> bool:
    """主机名属于这个网站：一样，或是它下面的子域名。"""
    return host == domain or host.endswith("." + domain)


def check_domain(domain: str) -> str:
    value = domain.strip().rstrip(".").lower()
    if not _HOST.fullmatch(value):
        raise refused("domain_invalid", "give a host name such as www.example.gov.cn")
    return value


def move_status(current: str, action: str, review: str) -> str:
    """在用 / 暂停 / 停用之间走一步；走不通的拒绝。启用、恢复都要已审过。"""
    target = _STATUS_MOVES.get(action, {}).get(current)
    if target is None:
        raise SupplyError(
            409, "status_move_invalid", f"cannot {action} a {current} one"
        )
    if target == "active" and review != "approved":
        raise SupplyError(409, "not_approved", "approve the channel first")
    return target


@dataclass(frozen=True)
class Registration:
    """登记一条渠道写什么（0045 第 3.1 节）：名称、大类；能提供什么；怎么取；条款与许可；
    规矩。审核结论与状态不在这里，由审核、启停改。"""

    name: str
    kind: str
    plugin: str
    doc_types: tuple[str, ...]
    readings: tuple[str, ...]
    license: str
    rate_per_minute: int
    daily_cap: int
    domain: str | None = None
    terms_url: str | None = None
    render_allowed: bool = False
    obey_robots: bool = True
    config: dict[str, object] = field(default_factory=dict)
    max_running: int = 1  # 同时最多跑几个作业（登记的并发，0045 第 3.3 节）


def check_registration(reg: Registration, known_types: frozenset[str]) -> Registration:
    if not reg.name.strip() or len(reg.name) > 80:
        raise refused("name_invalid", "give a name of at most 80 characters")
    if reg.kind not in KINDS:
        raise refused("kind_invalid", "kind is catalog/feed/web/search")
    if reg.license not in LICENSES:
        raise refused("license_invalid", "license is self/via_expert/sale")
    unknown = [t for t in reg.doc_types if t not in known_types]
    if unknown:
        raise refused("doc_type_unknown", f"not in the vocabulary: {unknown[0]}")
    if reg.kind != "search" and not reg.readings:
        raise refused("readings_invalid", "say which readings the channel gives")
    if any(r not in ("original", "text", "pages", "table") for r in reg.readings):
        raise refused("readings_invalid", "readings are original/text/pages/table")
    if not 1 <= reg.rate_per_minute <= MAX_RATE_PER_MINUTE:
        raise refused("rate_invalid", f"rate is 1 to {MAX_RATE_PER_MINUTE} a minute")
    if not 1 <= reg.daily_cap <= MAX_DAILY_CAP:
        raise refused("daily_cap_invalid", f"daily cap is 1 to {MAX_DAILY_CAP}")
    if not 1 <= reg.max_running <= MAX_RUNNING:
        raise refused("max_running_invalid", f"max running is 1 to {MAX_RUNNING}")
    domain = check_domain(reg.domain) if reg.domain else None
    if reg.kind == "web" and domain is None:
        # 通用网页按网站审：一条渠道就是一个网站
        raise refused("domain_required", "a web channel is one site: give its domain")
    if reg.terms_url:
        site_of(reg.terms_url)
    return Registration(
        name=reg.name.strip(),
        kind=reg.kind,
        plugin=reg.plugin,
        doc_types=tuple(dict.fromkeys(reg.doc_types)),
        readings=tuple(dict.fromkeys(reg.readings)),
        license=reg.license,
        rate_per_minute=reg.rate_per_minute,
        daily_cap=reg.daily_cap,
        domain=domain,
        terms_url=reg.terms_url or None,
        render_allowed=reg.render_allowed,
        obey_robots=reg.obey_robots,
        config=dict(reg.config),
        max_running=reg.max_running,
    )


def check_channel_id(channel_id: str) -> str:
    if not _CHANNEL_ID.fullmatch(channel_id):
        raise refused("channel_id_invalid", "lower-case letters, digits and dashes")
    return channel_id


def site_channel_id(domain: str) -> str:
    """待审网站自动建的渠道编号：site- 加域名（点换成连字符），过长的截断加摘要。"""
    import hashlib  # noqa: PLC0415 - 只在这里用

    slug = "site-" + domain.replace(".", "-")
    if len(slug) <= 40:
        return slug
    digest = hashlib.sha256(domain.encode()).hexdigest()[:8]
    return slug[:31] + "-" + digest


TrialStatus = Literal["queued", "running", "ok", "failed"]


@dataclass(frozen=True)
class Trial:
    """一次试采：对样例描述跑一次「列」和「取」，结果给审核的人看。"""

    id: str
    channel_id: str
    key: dict[str, object]
    status: str
    requested_by: str
    created_at: datetime
    finished_at: datetime | None = None
    listed: tuple[dict[str, object], ...] = ()
    fetched: dict[str, object] | None = None
    error: str | None = None


@dataclass(frozen=True)
class ChannelEvent:
    """审核记录与渠道上的每一次改动：谁、何时、做了什么、凭什么。"""

    id: str
    channel_id: str
    action: str
    actor: str
    reason: str | None
    detail: dict[str, object]
    at: datetime


@dataclass(frozen=True)
class SiteSummary:
    """待审网站的告警：有多少个、最久的等了多久（0045 第 3.1 节）。"""

    pending: int
    oldest_since: datetime | None
    now: datetime

    @property
    def alerts(self) -> list[str]:
        out = []
        if self.pending >= SITE_ALERT_COUNT:
            out.append("too_many_pending")
        if self.oldest_since and self.now - self.oldest_since >= SITE_ALERT_WAIT:
            out.append("waiting_too_long")
        return out
