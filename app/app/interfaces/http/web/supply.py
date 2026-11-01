"""web端：提需求、我的需求、订阅、按主题找（SDD 0045 第一、二节，第七节块 10；发版前补齐 F1）。

本人登录 knowfeed web端后：
- 提需求：精确描述（主体 × 类型 × 期间）或给网址（一页，或连它直接链到的文件）。本人提交
  即确认：knowfeed 凭本人令牌请 knowbase 记确认（本人令牌由会话的访问令牌换来，
  `get_web_knowbase_token`），再照应用代提的同一套受理。确认之前可以先预切合，看这类能不能
  取、网站审没审过、大概多久。
- 我的需求：本人名下的全部需求单（不论谁代提的），能看、取消、再试一次。
- 订阅：建、看每轮记录、暂停、恢复、续订、撤；建、续、撤凭本人令牌在 knowbase 留、改、撤
  长期范围同意。
- 按主题找：给话题（可指定网站），候选出来 7 天内勾，勾的再确认、再去取。

归属用户就是这个会话的本人（Casdoor `sub`）；看不到别人的需求、订阅、搜索。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app.application.errors.exceptions import AppException
from app.application.supply.demands import DemandService
from app.application.supply.directory import MAX_RESULTS, SecurityDirectory
from app.application.supply.matching import PreMatch
from app.application.supply.mine import MyDemands
from app.application.supply.searches import SearchService
from app.application.supply.subscriptions import SubscriptionService
from app.domain.security import Principal
from app.domain.supply.models import (
    MAX_LINKED,
    Demand,
    DemandItem,
    DemandItemIn,
    DescriptionKey,
    SupplyError,
    summarize,
)
from app.domain.supply.prematch import PrematchRequestV1, PrematchResponseV1
from app.domain.supply.quota import BEIJING
from app.domain.supply.subscription import (
    MAX_CANDIDATES,
    MAX_EVERY_HOURS,
    MAX_MONTHLY_CAP,
    MAX_TARGETS,
    MAX_URLS,
    Round,
    Search,
    Subscription,
)
from app.domain.supply.vocabulary import (
    COLLECTABLE_BY_ORG,
    DOC_TYPE_CLASS,
    EDITIONS,
    PERIODIC,
    TOPICS,
)
from app.infrastructure.storage.postgres import get_postgres
from app.interfaces.http.middleware.auth import (
    get_web_current_user,
    get_web_knowbase_token,
)

router = APIRouter(prefix="/web/v1/supply", tags=["web-供给"])

WebUser = Annotated[Principal, Depends(get_web_current_user)]
UserToken = Annotated[str, Depends(get_web_knowbase_token)]
Reading = Literal["original", "text", "pages", "table"]


def _sessions():
    return get_postgres().session_factory


def get_my_demands() -> MyDemands:
    from app.bootstrap.supply import build_my_demands

    return build_my_demands(_sessions())


def get_demand_service() -> DemandService:
    from app.bootstrap.supply import build_demand_service

    return build_demand_service(_sessions())


def get_subscriptions() -> SubscriptionService:
    from app.bootstrap.supply import build_subscriptions

    return build_subscriptions(_sessions())


def get_searches() -> SearchService:
    from app.bootstrap.supply import build_searches

    return build_searches(_sessions())


def get_prematch() -> PreMatch:
    from app.bootstrap.supply import build_prematch

    return build_prematch(_sessions())


def get_directory() -> SecurityDirectory:
    from app.bootstrap.supply import build_directory

    return build_directory(_sessions())


async def _call(work):
    try:
        return await work()
    except SupplyError as exc:
        raise AppException(
            code=exc.code, status_code=exc.status, msg=exc.message
        ) from None


# —— 词表与上限（表单用） ——


class OptionsRead(BaseModel):
    doc_types: dict[str, str]  # 细类 → 大类
    periodic: list[str]
    topics: list[str]
    editions: list[str]
    org_doc_types: list[str]  # 机构编号能配的细类
    max_linked: int
    max_targets: int
    max_urls: int
    max_candidates: int
    max_every_hours: int
    max_monthly_cap: int


@router.get("/options", response_model=OptionsRead)
async def options(_: WebUser) -> OptionsRead:
    return OptionsRead(
        doc_types=dict(DOC_TYPE_CLASS),
        periodic=list(PERIODIC),
        topics=list(TOPICS),
        editions=list(EDITIONS),
        org_doc_types=list(COLLECTABLE_BY_ORG),
        max_linked=MAX_LINKED,
        max_targets=MAX_TARGETS,
        max_urls=MAX_URLS,
        max_candidates=MAX_CANDIDATES,
        max_every_hours=MAX_EVERY_HOURS,
        max_monthly_cap=MAX_MONTHLY_CAP,
    )


class SecurityRead(BaseModel):
    subject: str
    name: str


@router.get("/securities", response_model=list[SecurityRead])
async def find_securities(
    _: WebUser,
    q: str = Query(min_length=1, max_length=40),
    limit: int = Query(default=10, ge=1, le=MAX_RESULTS),
    directory: SecurityDirectory = Depends(get_directory),
):
    """查主体：按名字或代码查证券（同内部接口的证券名录）。"""
    found = await directory.search(q, limit)
    return [SecurityRead(subject=e.subject, name=e.name) for e in found]


@router.post("/prematch", response_model=PrematchResponseV1)
async def prematch(
    payload: PrematchRequestV1,
    _: WebUser,
    service: PreMatch = Depends(get_prematch),
):
    """提交之前看这几项能不能取、网站审没审过、许可档、大概多久（只查来源目录）。"""
    answers = await _call(lambda: service.check("knowfeed", payload))
    return PrematchResponseV1(items=answers)


# —— 提需求、我的需求 ——


class KeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=64)
    doc_type: str = Field(min_length=1, max_length=32)
    period_start: date | None = None
    period_end: date | None = None
    edition: Literal["full", "summary", "english"] = "full"
    topics: list[str] = Field(default_factory=list, max_length=12)


class PrefillIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str | None = Field(default=None, max_length=64)
    doc_type: str | None = Field(default=None, max_length=32)
    title: str | None = Field(default=None, max_length=200)


class ItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: KeyIn | None = None
    url: str | None = Field(default=None, max_length=2000)
    follow_links: bool = False
    prefill: PrefillIn | None = None
    # 给网址：用户声明「公开页面、不用登录」，必须勾（0045 第 2.2 节），记进需求项
    declared_public: bool = False


class ConditionsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_license: Literal["self", "via_expert", "sale"] | None = None


class MyDemandIn(BaseModel):
    """本人亲手提：精确描述或给网址；要的读法；最晚什么时候要（必填）；说明（选填）。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["description", "url"]
    items: list[ItemIn] = Field(min_length=1, max_length=50)
    readings: list[Reading] = Field(min_length=1, max_length=4)
    deadline: datetime
    note: str | None = Field(default=None, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=128)
    conditions: ConditionsIn | None = None


class ItemRead(BaseModel):
    item_id: str
    confirmation_item: str
    key: dict[str, object] | None
    url: str | None
    follow_links: bool
    declared_public: bool
    readings: list[str]
    status: str
    reason: str | None
    receipt: dict[str, object]
    retry_of: str | None
    updated_at: datetime


class DemandRead(BaseModel):
    demand_id: str
    app: str
    proposer: str
    status: str
    created_at: datetime
    deadline: datetime | None
    note: str | None
    items: list[ItemRead]


class SubmitRead(BaseModel):
    demand: DemandRead | None
    replayed: bool
    # 本人已有、内容与读法都齐的确认条目：不必取，照用（0043 第 3.3 节入口分支）
    already_ready: list[str]


def _item(i: DemandItem) -> ItemRead:
    return ItemRead(
        item_id=i.id,
        confirmation_item=i.confirmation_item,
        key=i.key.as_dict() if i.key is not None else None,
        url=i.url,
        follow_links=i.follow_links,
        declared_public=i.declared_public,
        readings=list(i.readings),
        status=i.status,
        reason=i.reason,
        receipt=i.receipt,
        retry_of=i.retry_of,
        updated_at=i.updated_at,
    )


def _demand(d: Demand, items: list[DemandItem]) -> DemandRead:
    return DemandRead(
        demand_id=d.id,
        app=d.app,
        proposer=d.proposer,
        status=summarize([i.status for i in items]),
        created_at=d.created_at,
        deadline=d.deadline,
        note=d.note,
        items=[_item(i) for i in items],
    )


def _items_in(payload: MyDemandIn) -> list[DemandItemIn]:
    out = []
    for n, i in enumerate(payload.items, start=1):
        key = (
            DescriptionKey(
                i.key.subject,
                i.key.doc_type,
                i.key.period_start,
                i.key.period_end,
                edition=i.key.edition,
                topics=tuple(i.key.topics),
            )
            if i.key is not None
            else None
        )
        out.append(
            DemandItemIn(
                key,
                tuple(payload.readings),
                f"c{n}",
                url=i.url,
                follow_links=i.follow_links,
                prefill=i.prefill.model_dump(exclude_none=True) if i.prefill else {},
                declared_public=i.declared_public,
            )
        )
    return out


@router.post("/demands", response_model=SubmitRead, status_code=status.HTTP_201_CREATED)
async def submit(
    payload: MyDemandIn,
    user: WebUser,
    token: UserToken,
    service: MyDemands = Depends(get_my_demands),
):
    demand, items, replayed, ready = await _call(
        lambda: service.submit(
            token,
            user.subject,
            kind=payload.kind,
            items=_items_in(payload),
            deadline=payload.deadline,
            note=payload.note,
            idempotency_key=payload.idempotency_key,
            conditions=payload.conditions.model_dump(exclude_none=True)
            if payload.conditions
            else None,
        )
    )
    return SubmitRead(
        demand=_demand(demand, items) if demand is not None else None,
        replayed=replayed,
        already_ready=ready,
    )


@router.get("/demands", response_model=list[DemandRead])
async def my_demands(user: WebUser, service: MyDemands = Depends(get_my_demands)):
    return [_demand(d, items) for d, items in await service.list(user.subject)]


@router.get("/demands/{demand_id}", response_model=DemandRead)
async def my_demand(
    demand_id: str,
    user: WebUser,
    service: DemandService = Depends(get_demand_service),
):
    demand, items = await _call(lambda: service.mine(user.subject, demand_id))
    return _demand(demand, items)


@router.post("/demands/{demand_id}/cancel", response_model=DemandRead)
async def cancel(
    demand_id: str,
    user: WebUser,
    service: DemandService = Depends(get_demand_service),
):
    await _call(lambda: service.cancel_mine(user.subject, demand_id))
    demand, items = await _call(lambda: service.mine(user.subject, demand_id))
    return _demand(demand, items)


class RetryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deadline: datetime
    idempotency_key: str = Field(min_length=8, max_length=128)


@router.post(
    "/items/{item_id}/retry",
    response_model=SubmitRead,
    status_code=status.HTTP_201_CREATED,
)
async def retry(
    item_id: str,
    payload: RetryIn,
    user: WebUser,
    token: UserToken,
    service: MyDemands = Depends(get_my_demands),
):
    """再试一次：失败、超时、无法满足的项，本人再确认一次，生成挂在原项下的新项。"""
    demand, items, replayed, ready = await _call(
        lambda: service.retry(
            token,
            user.subject,
            item_id,
            deadline=payload.deadline,
            idempotency_key=payload.idempotency_key,
        )
    )
    return SubmitRead(
        demand=_demand(demand, items) if demand is not None else None,
        replayed=replayed,
        already_ready=ready,
    )


# —— 订阅 ——


class SubscriptionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    by: Literal["description", "url", "topic"]
    subjects: list[str] = Field(default_factory=list, max_length=50)
    doc_types: list[str] = Field(default_factory=list, max_length=11)
    urls: list[str] = Field(default_factory=list, max_length=MAX_URLS)
    topic: str | None = Field(default=None, max_length=200)
    readings: list[Reading] = Field(min_length=1, max_length=4)
    starts_on: date | None = None
    ends_on: date
    every_hours: int = Field(default=24, ge=1, le=MAX_EVERY_HOURS)
    monthly_cap: int = Field(default=100, ge=1, le=MAX_MONTHLY_CAP)
    purpose: str = Field(min_length=1, max_length=200)
    skip_backlog: bool = False
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)


class SubscriptionRead(BaseModel):
    subscription_id: str
    revision: int
    by: str
    subjects: list[str]
    doc_types: list[str]
    urls: list[str]
    topic: str | None
    readings: list[str]
    starts_on: date
    ends_on: date
    every_hours: int
    monthly_cap: int
    purpose: str
    status: str
    status_reason: str | None
    skip_backlog: bool
    renew_due: bool  # 离截止日 14 天以内：提醒续订
    last_checked_at: datetime | None
    next_check_at: datetime | None
    created_at: datetime


class RoundRead(BaseModel):
    started_at: datetime
    finished_at: datetime | None
    found: int
    taken: int
    over_cap: int
    demand_id: str | None
    note: str | None


class SubscriptionDetailRead(BaseModel):
    subscription: SubscriptionRead
    rounds: list[RoundRead]


def _subscription(s: Subscription) -> SubscriptionRead:
    return SubscriptionRead(
        subscription_id=s.id,
        revision=s.revision,
        by=s.by,
        subjects=list(s.subjects),
        doc_types=list(s.doc_types),
        urls=list(s.urls),
        topic=s.topic,
        readings=list(s.readings),
        starts_on=s.starts_on,
        ends_on=s.ends_on,
        every_hours=s.every_hours,
        monthly_cap=s.monthly_cap,
        purpose=s.purpose,
        status=s.status,
        status_reason=s.status_reason,
        skip_backlog=s.skip_backlog,
        renew_due=s.renew_due(datetime.now(BEIJING).date()),
        last_checked_at=s.last_checked_at,
        next_check_at=s.next_check_at,
        created_at=s.created_at,
    )


def _round(r: Round) -> RoundRead:
    return RoundRead(
        started_at=r.started_at,
        finished_at=r.finished_at,
        found=r.found,
        taken=r.taken,
        over_cap=r.over_cap,
        demand_id=r.demand_id,
        note=r.note,
    )


@router.get("/subscriptions", response_model=list[SubscriptionRead])
async def subscriptions(
    user: WebUser, service: SubscriptionService = Depends(get_subscriptions)
):
    return [_subscription(s) for s in await service.mine(user.subject)]


@router.post(
    "/subscriptions",
    response_model=SubscriptionRead,
    status_code=status.HTTP_201_CREATED,
)
async def subscribe(
    payload: SubscriptionIn,
    user: WebUser,
    token: UserToken,
    service: SubscriptionService = Depends(get_subscriptions),
):
    sub = await _call(
        lambda: service.create(
            token,
            user.subject,
            by=payload.by,
            subjects=tuple(payload.subjects),
            doc_types=tuple(payload.doc_types),
            urls=tuple(payload.urls),
            topic=payload.topic,
            readings=tuple(payload.readings),
            starts_on=payload.starts_on,
            ends_on=payload.ends_on,
            every_hours=payload.every_hours,
            monthly_cap=payload.monthly_cap,
            purpose=payload.purpose,
            skip_backlog=payload.skip_backlog,
            idempotency_key=payload.idempotency_key,
        )
    )
    return _subscription(sub)


@router.get("/subscriptions/{subscription_id}", response_model=SubscriptionDetailRead)
async def subscription(
    subscription_id: str,
    user: WebUser,
    service: SubscriptionService = Depends(get_subscriptions),
):
    sub, rounds = await _call(lambda: service.get(user.subject, subscription_id))
    return SubscriptionDetailRead(
        subscription=_subscription(sub), rounds=[_round(r) for r in rounds]
    )


@router.post("/subscriptions/{subscription_id}/pause", response_model=SubscriptionRead)
async def pause(
    subscription_id: str,
    user: WebUser,
    service: SubscriptionService = Depends(get_subscriptions),
):
    return _subscription(
        await _call(lambda: service.pause(user.subject, subscription_id))
    )


@router.post("/subscriptions/{subscription_id}/resume", response_model=SubscriptionRead)
async def resume(
    subscription_id: str,
    user: WebUser,
    service: SubscriptionService = Depends(get_subscriptions),
):
    return _subscription(
        await _call(lambda: service.resume(user.subject, subscription_id))
    )


class BacklogIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skip: bool


@router.put("/subscriptions/{subscription_id}/backlog", response_model=SubscriptionRead)
async def backlog(
    subscription_id: str,
    payload: BacklogIn,
    user: WebUser,
    service: SubscriptionService = Depends(get_subscriptions),
):
    """当月超额没取的，下月补不补。"""
    return _subscription(
        await _call(
            lambda: service.set_skip_backlog(
                user.subject, subscription_id, payload.skip
            )
        )
    )


class RenewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ends_on: date


@router.post("/subscriptions/{subscription_id}/renew", response_model=SubscriptionRead)
async def renew(
    subscription_id: str,
    payload: RenewIn,
    user: WebUser,
    token: UserToken,
    service: SubscriptionService = Depends(get_subscriptions),
):
    return _subscription(
        await _call(
            lambda: service.renew(token, user.subject, subscription_id, payload.ends_on)
        )
    )


@router.post("/subscriptions/{subscription_id}/cancel", response_model=SubscriptionRead)
async def unsubscribe(
    subscription_id: str,
    user: WebUser,
    token: UserToken,
    service: SubscriptionService = Depends(get_subscriptions),
):
    return _subscription(
        await _call(lambda: service.cancel(token, user.subject, subscription_id))
    )


# —— 按主题找 ——


class SearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=1, max_length=200)
    sites: list[str] = Field(default_factory=list, max_length=MAX_URLS)
    wanted: int = Field(default=20, ge=1, le=MAX_CANDIDATES)


class CandidateRead(BaseModel):
    ref: str
    title: str
    url: str
    snippet: str
    date: str | None
    channel_id: str
    site: str  # approved / pending / unregistered / rejected / invalid
    license: str | None


class SearchRead(BaseModel):
    search_id: str
    topic: str
    sites: list[str]
    wanted: int
    status: str
    note: str | None
    subscription_id: str | None
    created_at: datetime
    expires_at: datetime | None
    candidates: list[CandidateRead]


def _search(s: Search) -> SearchRead:
    # 按渠道先后、渠道给的原始顺序列（不重排、不评级）
    ordered = sorted(
        s.candidates,
        key=lambda c: (int(c.get("channel_priority") or 0), int(c.get("order") or 0)),  # type: ignore[call-overload]
    )
    return SearchRead(
        search_id=s.id,
        topic=s.topic,
        sites=list(s.sites),
        wanted=s.wanted,
        status=s.status,
        note=s.note,
        subscription_id=s.subscription_id,
        created_at=s.created_at,
        expires_at=s.expires_at,
        candidates=[
            CandidateRead(
                ref=str(c.get("ref")),
                title=str(c.get("title") or ""),
                url=str(c.get("url") or ""),
                snippet=str(c.get("snippet") or ""),
                date=str(c["date"]) if c.get("date") else None,
                channel_id=str(c.get("channel_id") or ""),
                site=str(c.get("site") or "unregistered"),
                license=str(c["license"]) if c.get("license") else None,
            )
            for c in ordered
        ],
    )


@router.post(
    "/searches", response_model=SearchRead, status_code=status.HTTP_201_CREATED
)
async def start_search(
    payload: SearchIn,
    user: WebUser,
    service: SearchService = Depends(get_searches),
):
    search = await _call(
        lambda: service.start(
            user.subject, payload.topic, tuple(payload.sites), payload.wanted
        )
    )
    return _search(search)


@router.get("/searches", response_model=list[SearchRead])
async def my_searches(user: WebUser, service: SearchService = Depends(get_searches)):
    return [_search(s) for s in await service.mine(user.subject)]


@router.get("/searches/{search_id}", response_model=SearchRead)
async def my_search(
    search_id: str, user: WebUser, service: SearchService = Depends(get_searches)
):
    return _search(await _call(lambda: service.get(user.subject, search_id)))


class PickIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refs: list[str] = Field(min_length=1, max_length=MAX_CANDIDATES)
    readings: list[Reading] = Field(min_length=1, max_length=4)
    follow_links: bool = False
    deadline: datetime
    idempotency_key: str = Field(min_length=8, max_length=128)


@router.post(
    "/searches/{search_id}/pick",
    response_model=DemandRead,
    status_code=status.HTTP_201_CREATED,
)
async def pick(
    search_id: str,
    payload: PickIn,
    user: WebUser,
    token: UserToken,
    service: SearchService = Depends(get_searches),
    subscriptions: SubscriptionService = Depends(get_subscriptions),
):
    """勾候选：每条变成一个给网址的需求项，再确认才去取。"""
    demand, items, _ = await _call(
        lambda: service.pick(
            token,
            user.subject,
            search_id,
            payload.refs,
            readings=tuple(payload.readings),
            idempotency_key=payload.idempotency_key,
            follow_links=payload.follow_links,
            deadline=payload.deadline,
            subscriptions=subscriptions,
        )
    )
    return _demand(demand, items)
