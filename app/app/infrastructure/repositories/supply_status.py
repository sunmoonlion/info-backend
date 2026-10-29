"""需求状态通知进发件箱（SDD 0045 第 2.5 节，契约 knowfeed-supply-status/v1）。

改需求项状态的那个事务里调：每张变了的单排一条通知，带当时的汇总状态与每一项。和状态
同一个事务提交，状态改了通知就一定排上；worker 取出来交给提单的应用（`delivery_handlers`）。
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.supply.models import summarize
from app.domain.supply.status_event import (
    DemandStatusEventV1,
    DemandStatusItemV1,
)
from app.infrastructure.messaging.durable_tasks import enqueue_task
from app.infrastructure.models.supply import SupplyDemand, SupplyDemandItem

STATUS_TOPIC = "knowfeed.supply.demand.status.v1"
NOTIFIED_APPS = ("knowbench", "knowlab")


async def enqueue_status(
    session: AsyncSession, demand_ids: Iterable[uuid.UUID], at: datetime
) -> int:
    """给这几张单各排一条状态通知；回排了几条。"""
    sent = 0
    for demand_id in sorted(set(demand_ids), key=str):
        demand = await session.get(SupplyDemand, demand_id)
        if demand is None or demand.app not in NOTIFIED_APPS:
            continue
        rows = (
            await session.execute(
                select(SupplyDemandItem)
                .where(SupplyDemandItem.demand_id == demand_id)
                .order_by(SupplyDemandItem.confirmation_item)
            )
        ).scalars()
        items = list(rows)
        if not items:
            continue
        event = DemandStatusEventV1(
            event_id=str(uuid.uuid4()),
            demand_id=str(demand.id),
            app=demand.app,  # type: ignore[arg-type]
            owner=demand.owner_subject,
            task_ref=demand.task_ref,
            status=summarize([i.status for i in items]),  # type: ignore[arg-type]
            items=[
                DemandStatusItemV1(
                    item_id=str(i.id),
                    confirmation_item=i.confirmation_item,
                    status=i.status,  # type: ignore[arg-type]
                    reason=i.reason,
                    updated_at=i.updated_at,
                )
                for i in items
            ],
            occurred_at=at,
        )
        await enqueue_task(
            session,
            topic=STATUS_TOPIC,
            key=str(demand.id),
            payload=event.model_dump(mode="json"),
            deduplication_key=f"supply.status:{event.event_id}",
        )
        sent += 1
    return sent
