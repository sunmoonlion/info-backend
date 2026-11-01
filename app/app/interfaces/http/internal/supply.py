"""给应用的需求接口（SDD 0045 第 2.4 节）：提需求单（精确描述、给网址）、看进度、取消。

应用以服务身份调（关系 `supply-demand`，scope `knowfeed:supply-demand`），带 knowbase 的
确认编号；knowfeed 受理前问 knowbase 核这张确认。按关系校验时就知道调用方是 knowbench 还是
knowlab（`relation_auth`）：需求单记在那个应用名下，进度只给它看。

需求状态每变一次，knowfeed 经发件箱通知提这张单的应用（契约 knowfeed-supply-status/v1，
`domain/supply/status_event.py`）；接收端没配的应用照旧按单号查进度。受理前按归属用户核
配额，超了回 429（`quota_rate`、`quota_open_demands`、`quota_monthly`）。

证券名录（`GET /securities`）：委托第 0 步模型按名字查证券代码（decisions ⑭「查主体」），
同一个服务身份与 scope。

预切合（`POST /prematch`，契约 knowfeed-supply-prematch/v1，`domain/supply/prematch.py`）：
确认之前问这几项能不能取、许可档、预计多久；只查来源目录与队列，同一个服务身份与 scope。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app.application.errors.exceptions import AppException
from app.application.supply.demands import DemandService
from app.application.supply.directory import MAX_RESULTS, SecurityDirectory
from app.application.supply.matching import PreMatch
from app.domain.supply.models import (
    Demand,
    DemandItem,
    DemandItemIn,
    DescriptionKey,
    SupplyError,
    refused,
    summarize,
)
from app.domain.supply.prematch import PrematchRequestV1, PrematchResponseV1
from app.infrastructure.security.relation_auth import (
    ServiceCaller,
    require_supply_demand_service,
)
from app.infrastructure.storage.postgres import get_postgres
from app.interfaces.http.internal.host_guard import internal_host_only

router = APIRouter(
    prefix="/internal/v1/supply",
    tags=["内部-供给"],
    dependencies=[Depends(internal_host_only)],
)


def get_demand_service() -> DemandService:
    from app.bootstrap.supply import build_demand_service

    return build_demand_service(get_postgres().session_factory)


def get_directory() -> SecurityDirectory:
    from app.bootstrap.supply import build_directory

    return build_directory(get_postgres().session_factory)


def get_prematch() -> PreMatch:
    from app.bootstrap.supply import build_prematch

    return build_prematch(get_postgres().session_factory)


def caller_app(
    caller: ServiceCaller = Depends(require_supply_demand_service),
) -> str:
    """提需求的是哪个应用：按关系校验的结果（只认 knowbench、knowlab）。"""
    return caller.caller


class DescriptionKeyIn(BaseModel):
    """一条描述（SDD 0049）：类型可以是细类或大类；定期报告的期间是报告期，其余是
    披露时间窗；版本形态进键，事项、行业只缩小范围。"""

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=64)
    doc_type: str = Field(min_length=1, max_length=32)
    period_start: date | None = None
    period_end: date | None = None
    edition: Literal["full", "summary", "english"] = "full"
    topics: list[str] = Field(default_factory=list, max_length=12)
    industry: str | None = Field(default=None, max_length=16)


class PrefillIn(BaseModel):
    """给网址时预填的描述（选填）。"""

    model_config = ConfigDict(extra="forbid")

    subject: str | None = Field(default=None, max_length=64)
    doc_type: str | None = Field(default=None, max_length=32)
    title: str | None = Field(default=None, max_length=200)


class DemandItemModel(BaseModel):
    """精确描述给 `key`；给网址给 `url`（确认里那一条的网址），两样只给一样。"""

    model_config = ConfigDict(extra="forbid")

    confirmation_item: str = Field(min_length=1, max_length=16)
    key: DescriptionKeyIn | None = None
    url: str | None = Field(default=None, max_length=2000)
    follow_links: bool = False
    prefill: PrefillIn | None = None
    # 用户声明「公开页面、不用登录」（0045 第 2.2 节），照记
    declared_public: bool = False
    readings: list[Literal["original", "text", "pages", "table"]] = Field(
        min_length=1, max_length=4
    )


class DemandIn(BaseModel):
    """应用代用户提（或替运行中的专家提）的精确描述或给网址需求。"""

    model_config = ConfigDict(extra="forbid")

    owner: str = Field(min_length=1, max_length=255)
    proposer: Literal["user", "app", "expert"] = "app"
    chain: list[str] = Field(default_factory=list, max_length=10)
    task_ref: str | None = Field(default=None, max_length=255)
    confirmation_id: str = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=8, max_length=128)
    deadline: datetime | None = None
    note: str | None = Field(default=None, max_length=500)
    items: list[DemandItemModel] = Field(min_length=1, max_length=50)


class DemandItemRead(BaseModel):
    item_id: str
    confirmation_item: str
    key: dict[str, object] | None
    url: str | None = None
    declared_public: bool = False
    readings: list[str]
    status: str
    reason: str | None
    receipt: dict[str, object]
    updated_at: datetime


class DemandRead(BaseModel):
    demand_id: str
    owner: str
    task_ref: str | None
    confirmation_id: str
    status: str
    replayed: bool = False
    deadline: datetime | None = None
    note: str | None = None
    items: list[DemandItemRead]


def _read(
    demand: Demand, items: list[DemandItem], replayed: bool = False
) -> DemandRead:
    return DemandRead(
        demand_id=demand.id,
        owner=demand.owner,
        task_ref=demand.task_ref,
        confirmation_id=demand.confirmation_id,
        status=summarize([i.status for i in items]),
        replayed=replayed,
        deadline=demand.deadline,
        note=demand.note,
        items=[
            DemandItemRead(
                item_id=i.id,
                confirmation_item=i.confirmation_item,
                key=i.key.as_dict() if i.key is not None else None,
                url=i.url,
                declared_public=i.declared_public,
                readings=list(i.readings),
                status=i.status,
                reason=i.reason,
                receipt=i.receipt,
                updated_at=i.updated_at,
            )
            for i in items
        ],
    )


async def _call(work):
    try:
        return await work()
    except SupplyError as exc:
        raise AppException(
            code=exc.code, status_code=exc.status, msg=exc.message
        ) from None


def item_in(i: DemandItemModel) -> DemandItemIn:
    key = (
        DescriptionKey(
            i.key.subject,
            i.key.doc_type,
            i.key.period_start,
            i.key.period_end,
            edition=i.key.edition,
            topics=tuple(i.key.topics),
            industry=i.key.industry,
        )
        if i.key is not None
        else None
    )
    prefill = i.prefill.model_dump(exclude_none=True) if i.prefill else {}
    return DemandItemIn(
        key=key,
        readings=tuple(i.readings),
        confirmation_item=i.confirmation_item,
        url=i.url,
        follow_links=i.follow_links,
        prefill=prefill,
        declared_public=i.declared_public,
    )


@router.post("/demands", response_model=DemandRead, status_code=status.HTTP_201_CREATED)
async def submit(
    payload: DemandIn,
    app: str = Depends(caller_app),
    service: DemandService = Depends(get_demand_service),
):
    items = [item_in(i) for i in payload.items]

    async def _run():
        if any(i.url is not None and not i.declared_public for i in items):
            raise refused("public_declaration_required", "declare the pages public")
        if payload.proposer == "user" and payload.deadline is None:
            raise refused("deadline_required", "say when you need it by")
        return await service.submit(
            app=app,
            owner=payload.owner,
            proposer=payload.proposer,
            chain=tuple(payload.chain),
            task_ref=payload.task_ref,
            confirmation_id=payload.confirmation_id,
            idempotency_key=payload.idempotency_key,
            items=items,
            deadline=payload.deadline,
            note=payload.note,
        )

    demand, made, replayed = await _call(_run)
    return _read(demand, made, replayed)


@router.get("/demands/{demand_id}", response_model=DemandRead)
async def get_demand(
    demand_id: str,
    app: str = Depends(caller_app),
    service: DemandService = Depends(get_demand_service),
):
    demand, items = await _call(lambda: service.get(app, demand_id))
    return _read(demand, items)


@router.post("/demands/{demand_id}/cancel", response_model=DemandRead)
async def cancel(
    demand_id: str,
    app: str = Depends(caller_app),
    service: DemandService = Depends(get_demand_service),
):
    await _call(lambda: service.cancel(app, demand_id))
    demand, items = await _call(lambda: service.get(app, demand_id))
    return _read(demand, items)


class SecurityRead(BaseModel):
    subject: str
    name: str


@router.get("/securities", response_model=list[SecurityRead])
async def find_securities(
    q: str = Query(min_length=1, max_length=40),
    limit: int = Query(default=10, ge=1, le=MAX_RESULTS),
    app: str = Depends(caller_app),
    directory: SecurityDirectory = Depends(get_directory),
):
    """按名字或代码查证券（查主体）：只回代码与简称，按对得上的程度排。"""
    found = await directory.search(q, limit)
    return [SecurityRead(subject=e.subject, name=e.name) for e in found]


@router.post("/prematch", response_model=PrematchResponseV1)
async def prematch(
    payload: PrematchRequestV1,
    app: str = Depends(caller_app),
    service: PreMatch = Depends(get_prematch),
):
    """预切合：每项回能不能取（能取 / 取不到 / 网站待审 / 不满足条件）、候选渠道、许可档、
    预计多久。不问渠道、不看 knowbase，不记任何东西。"""
    answers = await _call(lambda: service.check(app, payload))
    return PrematchResponseV1(items=answers)
