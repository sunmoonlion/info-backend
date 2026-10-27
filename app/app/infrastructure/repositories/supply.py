"""供给的记录（PostgreSQL，SDD 0045 第五节）。"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.supply.models import (
    TERMINAL,
    Channel,
    Demand,
    DemandItem,
    DescriptionKey,
    Job,
    Original,
    can_move,
)
from app.infrastructure.messaging.durable_tasks import enqueue_task
from app.infrastructure.models.supply import (
    SupplyAttempt,
    SupplyChannel,
    SupplyDemand,
    SupplyDemandItem,
    SupplyJob,
    SupplyOriginal,
)

JOB_TOPIC = "knowfeed.supply.job.v1"
OPEN_JOB = ("queued", "running", "waiting")


def _channel(row: SupplyChannel) -> Channel:
    return Channel(
        id=row.id,
        name=row.name,
        kind=row.kind,  # type: ignore[arg-type]
        plugin=row.plugin,
        doc_types=tuple(row.doc_types),
        readings=tuple(row.readings),
        license=row.license,
        review=row.review,  # type: ignore[arg-type]
        rate_per_minute=row.rate_per_minute,
        daily_cap=row.daily_cap,
        priority=row.priority,
        status=row.status,  # type: ignore[arg-type]
        consecutive_failures=row.consecutive_failures,
    )


def _demand(row: SupplyDemand) -> Demand:
    return Demand(
        id=str(row.id),
        owner=row.owner_subject,
        proposer=row.proposer,
        chain=tuple(row.chain),
        app=row.app,
        task_ref=row.task_ref,
        confirmation_id=row.confirmation_id,
        idempotency_key=row.idempotency_key,
        digest=row.digest,
        created_at=row.created_at,
    )


def _item(row: SupplyDemandItem) -> DemandItem:
    return DemandItem(
        id=str(row.id),
        demand_id=str(row.demand_id),
        key=DescriptionKey.from_dict(row.key),
        readings=tuple(row.readings),
        confirmation_item=row.confirmation_item,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
        channel_id=row.channel_id,
        reason=row.reason,
        receipt=dict(row.receipt or {}),
    )


def _job(row: SupplyJob) -> Job:
    return Job(
        id=str(row.id),
        kind=row.kind,  # type: ignore[arg-type]
        channel_id=row.channel_id,
        dedup_key=row.dedup_key,
        payload=dict(row.payload),
        status=row.status,
        attempts=row.attempts,
        created_at=row.created_at,
        tier=row.tier,
        last_error=row.last_error,
    )


def _original(row: SupplyOriginal) -> Original:
    return Original(
        id=str(row.id),
        sha256=row.sha256,
        channel_id=row.channel_id,
        channel_doc_id=row.channel_doc_id,
        source_url=row.source_url,
        media_type=row.media_type,
        size_bytes=row.size_bytes,
        object_key=row.object_key,
        fetched_at=row.fetched_at,
        delivered_at=row.delivered_at,
        purge_after=row.purge_after,
        knowbase_material_id=row.knowbase_material_id,
        knowbase_version_no=row.knowbase_version_no,
    )


class SqlSupplyRepository:
    def __init__(self, sessions: Callable[[], async_sessionmaker[AsyncSession]]):
        self._sessions = sessions

    async def channels(self) -> list[Channel]:
        async with self._sessions()() as session:
            rows = await session.execute(
                select(SupplyChannel).order_by(SupplyChannel.priority, SupplyChannel.id)
            )
            return [_channel(r) for r in rows.scalars()]

    async def find_demand(self, app: str, idempotency_key: str) -> Demand | None:
        async with self._sessions()() as session:
            row = await session.scalar(
                select(SupplyDemand).where(
                    SupplyDemand.app == app,
                    SupplyDemand.idempotency_key == idempotency_key,
                )
            )
            return _demand(row) if row is not None else None

    async def get_demand(self, demand_id: str) -> Demand | None:
        try:
            key = uuid.UUID(demand_id)
        except (ValueError, TypeError):
            return None
        async with self._sessions()() as session:
            row = await session.get(SupplyDemand, key)
            return _demand(row) if row is not None else None

    async def items_of(self, demand_id: str) -> list[DemandItem]:
        async with self._sessions()() as session:
            rows = await session.execute(
                select(SupplyDemandItem)
                .where(SupplyDemandItem.demand_id == uuid.UUID(demand_id))
                .order_by(SupplyDemandItem.confirmation_item)
            )
            return [_item(r) for r in rows.scalars()]

    async def submit(
        self,
        demand: Demand,
        items: list[DemandItem],
        jobs: list[Job],
        links: dict[str, str],
    ) -> list[Job]:
        async with self._sessions()() as session, session.begin():
            session.add(
                SupplyDemand(
                    id=uuid.UUID(demand.id),
                    owner_subject=demand.owner,
                    proposer=demand.proposer,
                    chain=list(demand.chain),
                    app=demand.app,
                    task_ref=demand.task_ref,
                    confirmation_id=demand.confirmation_id,
                    idempotency_key=demand.idempotency_key,
                    digest=demand.digest,
                    created_at=demand.created_at,
                )
            )
            # 同一渠道同一描述的未办完作业跨用户共享（0045 第 3.3 节）
            by_key: dict[str, uuid.UUID] = {}
            created: list[Job] = []
            for job in jobs:
                existing = await session.scalar(
                    select(SupplyJob)
                    .where(
                        SupplyJob.dedup_key == job.dedup_key,
                        SupplyJob.status.in_(OPEN_JOB),
                    )
                    .with_for_update()
                )
                if existing is not None:
                    by_key[job.dedup_key] = existing.id
                    continue
                session.add(
                    SupplyJob(
                        id=uuid.UUID(job.id),
                        kind=job.kind,
                        channel_id=job.channel_id,
                        dedup_key=job.dedup_key,
                        payload=dict(job.payload),
                        status="queued",
                        tier=job.tier,
                        attempts=0,
                        created_at=job.created_at,
                    )
                )
                by_key[job.dedup_key] = uuid.UUID(job.id)
                created.append(job)
            await session.flush()
            for item in items:
                dedup = links.get(item.id)
                session.add(
                    SupplyDemandItem(
                        id=uuid.UUID(item.id),
                        demand_id=uuid.UUID(item.demand_id),
                        key=item.key.as_dict(),
                        key_text=item.key.text,
                        readings=list(item.readings),
                        confirmation_item=item.confirmation_item,
                        status=item.status,
                        channel_id=item.channel_id,
                        job_id=by_key.get(dedup) if dedup else None,
                        reason=item.reason,
                        receipt=dict(item.receipt),
                        created_at=item.created_at,
                        updated_at=item.updated_at,
                    )
                )
            for job in created:
                await enqueue_task(
                    session,
                    topic=JOB_TOPIC,
                    key=job.id,
                    payload={"job_id": job.id},
                    deduplication_key=f"supply.job:{job.id}:0",
                )
        return created

    async def get_job(self, job_id: str) -> Job | None:
        async with self._sessions()() as session:
            row = await session.get(SupplyJob, uuid.UUID(job_id))
            return _job(row) if row is not None else None

    async def start_attempt(self, job_id: str, at: datetime) -> str | None:
        async with self._sessions()() as session, session.begin():
            row = await session.get(SupplyJob, uuid.UUID(job_id), with_for_update=True)
            if row is None or row.status not in ("queued", "waiting", "running"):
                return None
            row.status = "running"
            row.attempts += 1
            attempt = SupplyAttempt(id=uuid.uuid4(), job_id=row.id, started_at=at)
            session.add(attempt)
            await session.flush()
            return str(attempt.id)

    async def finish_attempt(
        self,
        job_id: str,
        attempt_id: str,
        at: datetime,
        *,
        outcome: str,
        reason: str | None,
        job_status: str,
    ) -> None:
        async with self._sessions()() as session, session.begin():
            await session.execute(
                update(SupplyAttempt)
                .where(SupplyAttempt.id == uuid.UUID(attempt_id))
                .values(finished_at=at, outcome=outcome, reason=(reason or "")[:200])
            )
            values: dict[str, object] = {
                "status": job_status,
                "last_error": (reason or None) and reason[:200],
            }
            if job_status in ("done", "failed"):
                values["finished_at"] = at
            await session.execute(
                update(SupplyJob)
                .where(SupplyJob.id == uuid.UUID(job_id))
                .values(**values)
            )

    async def retry_later(self, job_id: str, not_before: datetime) -> None:
        """临时出错：作业回到等待，过一会儿再排一次（同一作业、新的去重键）。"""
        async with self._sessions()() as session, session.begin():
            row = await session.get(SupplyJob, uuid.UUID(job_id), with_for_update=True)
            if row is None:
                return
            row.status = "waiting"
            await enqueue_task(
                session,
                topic=JOB_TOPIC,
                key=job_id,
                payload={"job_id": job_id},
                deduplication_key=f"supply.job:{job_id}:{row.attempts}",
                not_before=not_before,
            )

    async def waiting_items(self, job_id: str) -> list[tuple[DemandItem, Demand]]:
        async with self._sessions()() as session:
            rows = await session.execute(
                select(SupplyDemandItem, SupplyDemand)
                .join(SupplyDemand, SupplyDemand.id == SupplyDemandItem.demand_id)
                .where(
                    SupplyDemandItem.job_id == uuid.UUID(job_id),
                    SupplyDemandItem.status.not_in(tuple(TERMINAL)),
                )
                .order_by(SupplyDemandItem.created_at)
            )
            return [(_item(i), _demand(d)) for i, d in rows.all()]

    async def settle_items(
        self,
        item_ids: list[str],
        status: str,
        at: datetime,
        *,
        reason: str | None = None,
        receipt: dict[str, object] | None = None,
    ) -> int:
        if not item_ids:
            return 0
        changed = 0
        async with self._sessions()() as session, session.begin():
            rows = await session.execute(
                select(SupplyDemandItem)
                .where(SupplyDemandItem.id.in_([uuid.UUID(i) for i in item_ids]))
                .with_for_update()
            )
            for row in rows.scalars():
                if not can_move(row.status, status):
                    continue  # 迟到的事件不能把已取消、超时改成已就绪
                row.status = status
                row.updated_at = at
                if reason is not None:
                    row.reason = reason[:200]
                if receipt is not None:
                    row.receipt = dict(receipt)
                changed += 1
        return changed

    async def cancel_items(self, demand_id: str, at: datetime) -> int:
        items = await self.items_of(demand_id)
        return await self.settle_items(
            [i.id for i in items], "cancelled", at, reason="cancelled by the app"
        )

    async def find_original(self, sha256: str) -> Original | None:
        async with self._sessions()() as session:
            row = await session.scalar(
                select(SupplyOriginal).where(SupplyOriginal.sha256 == sha256)
            )
            return _original(row) if row is not None else None

    async def save_original(
        self, original: Original, meta: dict[str, object]
    ) -> Original:
        async with self._sessions()() as session, session.begin():
            existing = await session.scalar(
                select(SupplyOriginal).where(SupplyOriginal.sha256 == original.sha256)
            )
            if existing is not None:
                return _original(existing)
            row = SupplyOriginal(
                id=uuid.UUID(original.id),
                sha256=original.sha256,
                channel_id=original.channel_id,
                channel_doc_id=original.channel_doc_id,
                source_url=original.source_url,
                media_type=original.media_type,
                size_bytes=original.size_bytes,
                object_key=original.object_key,
                meta=dict(meta),
                fetched_at=original.fetched_at,
            )
            session.add(row)
            await session.flush()
            return _original(row)

    async def mark_delivered(
        self,
        original_id: str,
        at: datetime,
        purge_after: datetime,
        material_id: str,
        version_no: int,
    ) -> None:
        async with self._sessions()() as session, session.begin():
            await session.execute(
                update(SupplyOriginal)
                .where(SupplyOriginal.id == uuid.UUID(original_id))
                .values(
                    delivered_at=at,
                    purge_after=purge_after,
                    knowbase_material_id=material_id,
                    knowbase_version_no=version_no,
                )
            )
