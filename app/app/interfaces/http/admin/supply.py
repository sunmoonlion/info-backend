"""管理端：来源目录、待审网站、缺口清单、队列监控（SDD 0045 第一节、第 3.1 节、第四节，第七节
块 2）。要 knowfeed 管理端的权限（admin 或 operator，认法同其他管理端路由）。

- 来源目录：渠道列表、登记、改登记、审核（放行 / 审不过，放行前要试采成功）、在用 / 暂停
  / 停用、排先后、试采（worker 去跑）、每条渠道的审核记录与试采记录；
- 待审网站：通用网页按网站审，自动建的待审渠道与在等它的需求项，带告警；
- 缺口清单：需求项「无法满足」按「哪种 × 类型」汇总，看不出是谁要的；
- 队列监控：各渠道的作业与尝试、最近失败的原因，手动把等着的作业马上再排。

每一次改动记谁做的（管理端会话的主体）、凭什么。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app.application.errors.exceptions import AppException
from app.application.supply.catalog import PLUGINS, ChannelCatalog
from app.application.supply.monitor import SupplyMonitor
from app.domain.security import Principal
from app.domain.supply.catalog import (
    AUTO_PAUSE_AFTER,
    SITE_ALERT_COUNT,
    SITE_ALERT_WAIT,
    SITE_REVIEW_WITHIN,
    ChannelEvent,
    Registration,
    Trial,
)
from app.domain.supply.models import Channel, DescriptionKey, SupplyError
from app.infrastructure.storage.postgres import get_postgres
from app.interfaces.http.middleware.auth import require_knowfeed_admin

router = APIRouter(
    prefix="/admin/v1/supply",
    tags=["管理-供给"],
    dependencies=[Depends(require_knowfeed_admin)],
)

Admin = Annotated[Principal, Depends(require_knowfeed_admin)]


def get_catalog() -> ChannelCatalog:
    from app.bootstrap.supply import build_catalog

    return build_catalog(get_postgres().session_factory)


def get_monitor() -> SupplyMonitor:
    from app.bootstrap.supply import build_monitor

    return build_monitor(get_postgres().session_factory)


def actor_of(principal: Principal) -> str:
    return principal.subject


async def _call(work):
    try:
        return await work()
    except SupplyError as exc:
        raise AppException(
            code=exc.code, status_code=exc.status, msg=exc.message
        ) from None


# —— 读的样子 ——


class ChannelRead(BaseModel):
    channel_id: str
    name: str
    kind: str
    plugin: str
    doc_types: list[str]
    readings: list[str]
    license: str
    review: str
    status: str
    status_reason: str | None
    priority: int
    rate_per_minute: int
    daily_cap: int
    consecutive_failures: int
    domain: str | None
    terms_url: str | None
    render_allowed: bool
    obey_robots: bool
    config: dict[str, object]
    created_at: datetime | None
    updated_at: datetime | None


class EventRead(BaseModel):
    action: str
    actor: str
    reason: str | None
    detail: dict[str, object]
    at: datetime


class TrialRead(BaseModel):
    trial_id: str
    key: dict[str, object]
    status: str
    requested_by: str
    created_at: datetime
    finished_at: datetime | None
    listed: list[dict[str, object]]
    fetched: dict[str, object] | None
    error: str | None


class PluginRead(BaseModel):
    name: str
    kind: str


class ChannelListRead(BaseModel):
    items: list[ChannelRead]
    plugins: list[PluginRead]
    auto_pause_after: int


class ChannelDetailRead(BaseModel):
    channel: ChannelRead
    events: list[EventRead]
    trials: list[TrialRead]


def _channel(c: Channel) -> ChannelRead:
    return ChannelRead(
        channel_id=c.id,
        name=c.name,
        kind=c.kind,
        plugin=c.plugin,
        doc_types=list(c.doc_types),
        readings=list(c.readings),
        license=c.license,
        review=c.review,
        status=c.status,
        status_reason=c.status_reason,
        priority=c.priority,
        rate_per_minute=c.rate_per_minute,
        daily_cap=c.daily_cap,
        consecutive_failures=c.consecutive_failures,
        domain=c.domain,
        terms_url=c.terms_url,
        render_allowed=c.render_allowed,
        obey_robots=c.obey_robots,
        config=c.config,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


def _event(e: ChannelEvent) -> EventRead:
    return EventRead(
        action=e.action, actor=e.actor, reason=e.reason, detail=e.detail, at=e.at
    )


def _trial(t: Trial) -> TrialRead:
    return TrialRead(
        trial_id=t.id,
        key=t.key,
        status=t.status,
        requested_by=t.requested_by,
        created_at=t.created_at,
        finished_at=t.finished_at,
        listed=list(t.listed),
        fetched=t.fetched,
        error=t.error,
    )


# —— 写的样子 ——


class RegistrationIn(BaseModel):
    """登记写什么（0045 第 3.1 节）：名称、大类、插件；能提供什么；条款与许可；规矩。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    kind: Literal["catalog", "feed", "web", "search"]
    plugin: str = Field(min_length=1, max_length=40)
    doc_types: list[str] = Field(default_factory=list, max_length=60)
    readings: list[Literal["original", "text", "pages", "table"]] = Field(
        default_factory=list, max_length=4
    )
    license: Literal["self", "via_expert", "sale"]
    rate_per_minute: int = Field(ge=1)
    daily_cap: int = Field(ge=1)
    domain: str | None = Field(default=None, max_length=253)
    terms_url: str | None = Field(default=None, max_length=2000)
    render_allowed: bool = False
    obey_robots: bool = True
    config: dict[str, object] = Field(default_factory=dict)

    def registration(self) -> Registration:
        return Registration(
            name=self.name,
            kind=self.kind,
            plugin=self.plugin,
            doc_types=tuple(self.doc_types),
            readings=tuple(self.readings),
            license=self.license,
            rate_per_minute=self.rate_per_minute,
            daily_cap=self.daily_cap,
            domain=self.domain or None,
            terms_url=self.terms_url or None,
            render_allowed=self.render_allowed,
            obey_robots=self.obey_robots,
            config=dict(self.config),
        )


class ChannelCreate(RegistrationIn):
    channel_id: str = Field(min_length=2, max_length=40)


class ChannelUpdate(RegistrationIn):
    reason: str = Field(min_length=1, max_length=500)


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=1, max_length=500, description="凭什么")
    license: Literal["self", "via_expert", "sale"] | None = None


class StatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["pause", "resume", "disable", "enable"]
    reason: str | None = Field(default=None, max_length=200)


class OrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_ids: list[str] = Field(min_length=1, max_length=500)


class TrialIn(BaseModel):
    """样例描述（词表同 knowbase）：主体、类型、期间。"""

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=64)
    doc_type: str = Field(min_length=1, max_length=32)
    period_start: date | None = None
    period_end: date | None = None


# —— 来源目录 ——


@router.get("/channels", response_model=ChannelListRead)
async def list_channels(catalog: ChannelCatalog = Depends(get_catalog)):
    channels = await catalog.channels()
    return ChannelListRead(
        items=[_channel(c) for c in channels],
        plugins=[PluginRead(name=n, kind=k) for n, k in sorted(PLUGINS.items())],
        auto_pause_after=AUTO_PAUSE_AFTER,
    )


@router.post(
    "/channels", response_model=ChannelRead, status_code=status.HTTP_201_CREATED
)
async def register_channel(
    payload: ChannelCreate,
    principal: Admin,
    catalog: ChannelCatalog = Depends(get_catalog),
):
    made = await _call(
        lambda: catalog.register(
            payload.channel_id, payload.registration(), actor=actor_of(principal)
        )
    )
    return _channel(made)


@router.post("/channels/order", response_model=ChannelListRead)
async def reorder_channels(
    payload: OrderIn,
    principal: Admin,
    catalog: ChannelCatalog = Depends(get_catalog),
):
    channels = await _call(
        lambda: catalog.reorder(payload.channel_ids, actor=actor_of(principal))
    )
    return ChannelListRead(
        items=[_channel(c) for c in channels],
        plugins=[PluginRead(name=n, kind=k) for n, k in sorted(PLUGINS.items())],
        auto_pause_after=AUTO_PAUSE_AFTER,
    )


@router.get("/channels/{channel_id}", response_model=ChannelDetailRead)
async def channel_detail(
    channel_id: str, catalog: ChannelCatalog = Depends(get_catalog)
):
    channel, events, trials = await _call(lambda: catalog.detail(channel_id))
    return ChannelDetailRead(
        channel=_channel(channel),
        events=[_event(e) for e in events],
        trials=[_trial(t) for t in trials],
    )


@router.put("/channels/{channel_id}", response_model=ChannelRead)
async def update_channel(
    channel_id: str,
    payload: ChannelUpdate,
    principal: Admin,
    catalog: ChannelCatalog = Depends(get_catalog),
):
    changed = await _call(
        lambda: catalog.update(
            channel_id,
            payload.registration(),
            actor=actor_of(principal),
            reason=payload.reason,
        )
    )
    return _channel(changed)


@router.post("/channels/{channel_id}/review", response_model=ChannelRead)
async def review_channel(
    channel_id: str,
    payload: ReviewIn,
    principal: Admin,
    catalog: ChannelCatalog = Depends(get_catalog),
):
    changed = await _call(
        lambda: catalog.review(
            channel_id,
            payload.decision,
            actor=actor_of(principal),
            reason=payload.reason,
            license=payload.license,
        )
    )
    return _channel(changed)


@router.post("/channels/{channel_id}/status", response_model=ChannelRead)
async def set_channel_status(
    channel_id: str,
    payload: StatusIn,
    principal: Admin,
    catalog: ChannelCatalog = Depends(get_catalog),
):
    changed = await _call(
        lambda: catalog.set_status(
            channel_id, payload.action, actor=actor_of(principal), reason=payload.reason
        )
    )
    return _channel(changed)


@router.post(
    "/channels/{channel_id}/trials",
    response_model=TrialRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_trial(
    channel_id: str,
    payload: TrialIn,
    principal: Admin,
    catalog: ChannelCatalog = Depends(get_catalog),
):
    key = DescriptionKey(
        payload.subject, payload.doc_type, payload.period_start, payload.period_end
    )
    trial = await _call(
        lambda: catalog.request_trial(channel_id, key, actor=actor_of(principal))
    )
    return _trial(trial)


# —— 待审网站 ——


class SiteRead(BaseModel):
    channel_id: str
    domain: str | None
    since: datetime | None
    waiting_items: int
    users: int
    oldest_item_at: datetime | None


class SitesRead(BaseModel):
    pending: int
    oldest_since: datetime | None
    alerts: list[str]
    review_within_days: int
    alert_count: int
    alert_wait_hours: int
    items: list[SiteRead]


@router.get("/sites", response_model=SitesRead)
async def pending_sites(catalog: ChannelCatalog = Depends(get_catalog)):
    summary, rows = await catalog.sites()
    return SitesRead(
        pending=summary.pending,
        oldest_since=summary.oldest_since,
        alerts=summary.alerts,
        review_within_days=SITE_REVIEW_WITHIN.days,
        alert_count=SITE_ALERT_COUNT,
        alert_wait_hours=int(SITE_ALERT_WAIT.total_seconds() // 3600),
        items=[SiteRead(**row) for row in rows],  # type: ignore[arg-type]
    )


# —— 缺口清单 ——


class GapRead(BaseModel):
    kind: str
    doc_type: str | None
    items: int
    users: int
    subjects: int
    sample_subjects: list[str]
    first_at: datetime
    last_at: datetime


class GapsRead(BaseModel):
    items: list[GapRead]


@router.get("/gaps", response_model=GapsRead)
async def gaps(
    kind: Annotated[Literal["uncovered", "absent", "site"] | None, Query()] = None,
    monitor: SupplyMonitor = Depends(get_monitor),
):
    rows = await _call(lambda: monitor.gaps(kind))
    return GapsRead(items=[GapRead(**row) for row in rows])  # type: ignore[arg-type]


# —— 队列监控 ——


class ChannelQueueRead(BaseModel):
    channel_id: str
    name: str
    status: str
    status_reason: str | None
    consecutive_failures: int
    queued: int
    running: int
    waiting: int
    failed_recent: int
    done_recent: int
    oldest_open_at: datetime | None


class TierRead(BaseModel):
    tier: int
    open: int


class FailureRead(BaseModel):
    channel_id: str
    reason: str
    count: int


class QueueRead(BaseModel):
    since: datetime
    channels: list[ChannelQueueRead]
    tiers: list[TierRead]
    failures: list[FailureRead]


class JobRead(BaseModel):
    job_id: str
    kind: str
    channel_id: str
    key: dict[str, object]
    status: str
    tier: int
    attempts: int
    last_error: str | None
    created_at: datetime
    finished_at: datetime | None
    waiting_items: int


class AttemptRead(BaseModel):
    started_at: datetime
    finished_at: datetime | None
    outcome: str | None
    reason: str | None


class JobDetailRead(JobRead):
    channel_status: str
    attempt_list: list[AttemptRead]


class JobsRead(BaseModel):
    items: list[JobRead]


@router.get("/queue", response_model=QueueRead)
async def queue(monitor: SupplyMonitor = Depends(get_monitor)):
    return QueueRead(**await monitor.queue())  # type: ignore[arg-type]


@router.get("/jobs", response_model=JobsRead)
async def jobs(
    channel_id: Annotated[str | None, Query(max_length=40)] = None,
    job_status: Annotated[
        Literal["queued", "running", "waiting", "done", "failed"] | None,
        Query(alias="status"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    monitor: SupplyMonitor = Depends(get_monitor),
):
    rows = await monitor.jobs(channel_id=channel_id, status=job_status, limit=limit)
    return JobsRead(items=[JobRead(**row) for row in rows])  # type: ignore[arg-type]


@router.get("/jobs/{job_id}", response_model=JobDetailRead)
async def job_detail(job_id: str, monitor: SupplyMonitor = Depends(get_monitor)):
    return JobDetailRead(**await _call(lambda: monitor.job(job_id)))  # type: ignore[arg-type]


@router.post("/jobs/{job_id}/requeue", response_model=JobDetailRead)
async def requeue_job(job_id: str, monitor: SupplyMonitor = Depends(get_monitor)):
    return JobDetailRead(**await _call(lambda: monitor.requeue(job_id)))  # type: ignore[arg-type]
