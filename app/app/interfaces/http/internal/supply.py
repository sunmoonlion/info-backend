"""给应用的需求接口（SDD 0045 第 2.4 节）：提精确描述的需求单、看进度、取消。

应用以服务身份调（关系 `supply-demand`，scope `knowfeed:supply-demand`），带 knowbase 的
确认编号；knowfeed 受理前问 knowbase 核这张确认。按关系校验时就知道调用方是 knowbench 还是
knowlab（`relation_auth`）：需求单记在那个应用名下，进度只给它看。

需求状态的推送（经发件箱通知提需求的应用）随 knowbench 接上时做（0045 块 4）；现在应用
按单号查进度。

证券名录（`GET /securities`）：委托第 0 步模型按名字查证券代码（decisions ⑭「查主体」），
同一个服务身份与 scope。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field

from app.application.errors.exceptions import AppException
from app.application.supply.demands import DemandService
from app.application.supply.directory import MAX_RESULTS, SecurityDirectory
from app.domain.supply.models import (
    Demand,
    DemandItem,
    DemandItemIn,
    DescriptionKey,
    SupplyError,
)
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


class DemandItemModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation_item: str = Field(min_length=1, max_length=16)
    key: DescriptionKeyIn
    readings: list[Literal["original", "text", "pages", "table"]] = Field(
        min_length=1, max_length=4
    )


class DemandIn(BaseModel):
    """应用代用户提（或替运行中的专家提）的精确描述需求。"""

    model_config = ConfigDict(extra="forbid")

    owner: str = Field(min_length=1, max_length=255)
    proposer: Literal["user", "app", "expert"] = "app"
    chain: list[str] = Field(default_factory=list, max_length=10)
    task_ref: str | None = Field(default=None, max_length=255)
    confirmation_id: str = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=8, max_length=128)
    items: list[DemandItemModel] = Field(min_length=1, max_length=50)


class DemandItemRead(BaseModel):
    item_id: str
    confirmation_item: str
    key: dict[str, object]
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
    items: list[DemandItemRead]


def _summary(items: list[DemandItem]) -> str:
    """需求单状态由各项汇总（0045 第 2.5 节）。"""
    statuses = {i.status for i in items}
    for open_status in ("fetching", "accepted"):
        if open_status in statuses:
            return open_status
    if statuses == {"ready"}:
        return "ready"
    if "ready" in statuses or "partial" in statuses:
        return "partial"
    return sorted(statuses)[0] if statuses else "accepted"


def _read(
    demand: Demand, items: list[DemandItem], replayed: bool = False
) -> DemandRead:
    return DemandRead(
        demand_id=demand.id,
        owner=demand.owner,
        task_ref=demand.task_ref,
        confirmation_id=demand.confirmation_id,
        status=_summary(items),
        replayed=replayed,
        items=[
            DemandItemRead(
                item_id=i.id,
                confirmation_item=i.confirmation_item,
                key=i.key.as_dict(),
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


@router.post("/demands", response_model=DemandRead, status_code=status.HTTP_201_CREATED)
async def submit(
    payload: DemandIn,
    app: str = Depends(caller_app),
    service: DemandService = Depends(get_demand_service),
):
    items = [
        DemandItemIn(
            key=DescriptionKey(
                i.key.subject,
                i.key.doc_type,
                i.key.period_start,
                i.key.period_end,
                edition=i.key.edition,
                topics=tuple(i.key.topics),
                industry=i.key.industry,
            ),
            readings=tuple(i.readings),
            confirmation_item=i.confirmation_item,
        )
        for i in payload.items
    ]
    demand, made, replayed = await _call(
        lambda: service.submit(
            app=app,
            owner=payload.owner,
            proposer=payload.proposer,
            chain=tuple(payload.chain),
            task_ref=payload.task_ref,
            confirmation_id=payload.confirmation_id,
            idempotency_key=payload.idempotency_key,
            items=items,
        )
    )
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
