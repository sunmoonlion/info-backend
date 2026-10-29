"""调度的记录（PostgreSQL，SDD 0045 第 3.3 节）：把排着的作业按渠道、四档、按用户轮流排给
worker；每问一次渠道记一次，按它限速；上传资料的加工作业进加工队列。

- 排：一个事务里拿调度锁（同时只有一个调度在排，拿不到的这一轮就不排），每条队列按
  「同时最多跑几个」补满。排出去的作业改成「在跑」、记排出去的时间，经发件箱投给 worker
  （worker 挂了，发件箱会再投；作业跑的时候已在跑的照样接着跑）。
- 等它的需求项都撤了的取数作业不再排，直接取消（0045 第 3.3 节「所有请求都撤了作业才
  取消」）。
- 限速：一个事务里拿限速锁，数近一分钟问了几次（渠道、全局）、今天取回几份，没超就记一次。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.supply.models import REASON_NO_ONE_WAITING, TERMINAL
from app.domain.supply.schedule import (
    PROCESS_QUEUE,
    TIER_BACKGROUND,
    Candidate,
    Usage,
    day_start,
    pick,
    wait_for,
)
from app.infrastructure.messaging.durable_tasks import enqueue_task
from app.infrastructure.models.supply import (
    SupplyChannel,
    SupplyChannelRequest,
    SupplyDemand,
    SupplyDemandItem,
    SupplyJob,
    SupplyTurn,
)
from app.infrastructure.repositories.supply import JOB_TOPIC, OPEN_JOB

DISPATCH_LOCK = "knowfeed.supply.dispatch"
RATE_LOCK = "knowfeed.supply.rate"
LOOK_AHEAD = 200  # 每条队列每轮最多看前面几个作业


class SqlScheduleRepository:
    def __init__(self, sessions: Callable[[], async_sessionmaker[AsyncSession]]):
        self._sessions = sessions

    async def dispatch(self, now: datetime, *, process_slots: int) -> list[str]:
        """排一轮；回排出去的作业编号。"""
        picked: list[str] = []
        async with self._sessions()() as session, session.begin():
            locked = await session.scalar(
                text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"),
                {"key": DISPATCH_LOCK},
            )
            if not locked:
                return []
            channels = (
                await session.execute(
                    select(SupplyChannel)
                    .where(
                        SupplyChannel.status == "active",
                        SupplyChannel.review == "approved",
                    )
                    .order_by(SupplyChannel.priority, SupplyChannel.id)
                )
            ).scalars()
            running = dict(
                (
                    await session.execute(
                        select(SupplyJob.channel_id, func.count())
                        .where(SupplyJob.status == "running")
                        .group_by(SupplyJob.channel_id)
                    )
                ).all()
            )
            fetched = await _fetched_today(session, now)
            queues: list[tuple[str, int]] = []
            for channel in channels:
                if fetched.get(channel.id, 0) >= channel.daily_cap:
                    continue  # 今天的份数用完了：排出去也只会放回来，明天再排
                queues.append(
                    (channel.id, channel.max_running - running.get(channel.id, 0))
                )
            queues.append(
                (PROCESS_QUEUE, process_slots - running.get(PROCESS_QUEUE, 0))
            )
            for queue, slots in queues:
                if slots > 0:
                    picked += await _fill(session, queue, slots, now)
        return picked

    async def usage(self, channel_id: str, now: datetime) -> Usage:
        async with self._sessions()() as session:
            return await _usage(session, channel_id, now)

    async def take(
        self,
        channel_id: str,
        *,
        fetch: bool,
        rate_per_minute: int,
        daily_cap: int,
        global_rate: int,
        now: datetime,
    ) -> datetime | None:
        """问渠道之前要一个名额：有就记一次、回 None；没有就回最早什么时候能问。"""
        async with self._sessions()() as session, session.begin():
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": RATE_LOCK},
            )
            usage = await _usage(session, channel_id, now)
            later = wait_for(
                usage,
                rate_per_minute=rate_per_minute,
                daily_cap=daily_cap,
                global_rate=global_rate,
                fetch=fetch,
                now=now,
            )
            if later is not None:
                return later
            session.add(
                SupplyChannelRequest(
                    channel_id=channel_id, kind="fetch" if fetch else "request", at=now
                )
            )
            return None

    async def queue_ahead(self, tier: int) -> dict[str, int]:
        """各渠道排在这一档及更靠前的、没办完的作业有几个（预计时间用）。"""
        async with self._sessions()() as session:
            rows = await session.execute(
                select(SupplyJob.channel_id, func.count())
                .where(SupplyJob.status.in_(OPEN_JOB), SupplyJob.tier <= tier)
                .group_by(SupplyJob.channel_id)
            )
            return {cid: int(n) for cid, n in rows.all()}

    async def fetched_today(self, now: datetime) -> dict[str, int]:
        async with self._sessions()() as session:
            return await _fetched_today(session, now)

    async def add_process_jobs(
        self, pending: list[tuple[str, str, int]], now: datetime
    ) -> int:
        """knowbase 待加工清单里的每一版建一个加工作业（第 ④ 档）；同一版已有没办完的
        就不再建。回新建了几个。"""
        made = 0
        async with self._sessions()() as session, session.begin():
            for owner, material_id, version_no in pending:
                dedup = f"process:{material_id}:{version_no}"
                inserted = await session.scalar(
                    insert(SupplyJob)
                    .values(
                        id=uuid.uuid4(),
                        kind="process",
                        channel_id=PROCESS_QUEUE,
                        dedup_key=dedup,
                        payload={
                            "owner": owner,
                            "material_id": material_id,
                            "version_no": version_no,
                        },
                        status="queued",
                        tier=TIER_BACKGROUND,
                        attempts=0,
                        created_at=now,
                    )
                    .on_conflict_do_nothing(
                        index_elements=["dedup_key"],
                        # 与部分唯一索引 uq_supply_job_open 的条件逐字一致才认得出
                        index_where=text("status IN ('queued', 'running', 'waiting')"),
                    )
                    .returning(SupplyJob.id)
                )
                made += inserted is not None
        return made

    async def prune_requests(self, before: datetime) -> int:
        async with self._sessions()() as session, session.begin():
            result = await session.execute(
                delete(SupplyChannelRequest).where(SupplyChannelRequest.at < before)
            )
            return int(result.rowcount or 0)


async def _usage(session: AsyncSession, channel_id: str, now: datetime) -> Usage:
    minute = now - timedelta(minutes=1)
    in_minute = SupplyChannelRequest.at > minute
    channel_minute, channel_oldest = (
        await session.execute(
            select(func.count(), func.min(SupplyChannelRequest.at)).where(
                SupplyChannelRequest.channel_id == channel_id, in_minute
            )
        )
    ).one()
    global_minute, global_oldest = (
        await session.execute(
            select(func.count(), func.min(SupplyChannelRequest.at)).where(in_minute)
        )
    ).one()
    fetched = await session.scalar(
        select(func.count()).where(
            SupplyChannelRequest.channel_id == channel_id,
            SupplyChannelRequest.kind == "fetch",
            SupplyChannelRequest.at >= day_start(now),
        )
    )
    return Usage(
        channel_minute=int(channel_minute),
        global_minute=int(global_minute),
        fetched_today=int(fetched or 0),
        channel_oldest=channel_oldest,
        global_oldest=global_oldest,
    )


async def _fetched_today(session: AsyncSession, now: datetime) -> dict[str, int]:
    rows = await session.execute(
        select(SupplyChannelRequest.channel_id, func.count())
        .where(
            SupplyChannelRequest.kind == "fetch",
            SupplyChannelRequest.at >= day_start(now),
        )
        .group_by(SupplyChannelRequest.channel_id)
    )
    return {cid: int(n) for cid, n in rows.all()}


async def _fill(
    session: AsyncSession, queue: str, slots: int, now: datetime
) -> list[str]:
    """一条队列补 `slots` 个：先靠前的档，同档按用户轮流（`schedule.pick`）。"""
    jobs = list(
        (
            await session.execute(
                select(SupplyJob)
                .where(
                    SupplyJob.channel_id == queue,
                    SupplyJob.status.in_(("queued", "waiting")),
                    or_(SupplyJob.not_before.is_(None), SupplyJob.not_before <= now),
                )
                .order_by(SupplyJob.tier, SupplyJob.created_at)
                .limit(LOOK_AHEAD)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    if not jobs:
        return []
    owners: dict[uuid.UUID, set[str]] = {j.id: set() for j in jobs}
    for job in jobs:
        if job.kind == "process":
            owners[job.id].add(str(job.payload.get("owner") or ""))
    fetch_ids = [j.id for j in jobs if j.kind != "process"]
    if fetch_ids:
        rows = await session.execute(
            select(SupplyDemandItem.job_id, SupplyDemand.owner_subject)
            .join(SupplyDemand, SupplyDemand.id == SupplyDemandItem.demand_id)
            .where(
                SupplyDemandItem.job_id.in_(fetch_ids),
                SupplyDemandItem.status.not_in(tuple(TERMINAL)),
            )
        )
        for job_id, owner in rows.all():
            owners[job_id].add(owner)
    orphans = [j for j in jobs if j.kind != "process" and not owners[j.id]]
    for job in orphans:
        job.status = "failed"
        job.last_error = REASON_NO_ONE_WAITING
        job.finished_at = now
    everyone = sorted({o for found in owners.values() for o in found})
    served = dict(
        (
            await session.execute(
                select(SupplyTurn.owner_subject, SupplyTurn.served_at).where(
                    SupplyTurn.queue == queue,
                    SupplyTurn.owner_subject.in_(everyone),
                )
            )
        ).all()
    )
    chosen = pick(
        [
            Candidate(str(j.id), j.tier, j.created_at, tuple(sorted(owners[j.id])))
            for j in jobs
            if owners[j.id]
        ],
        slots,
        served,
        now,
    )
    by_id = {str(j.id): j for j in jobs}
    for job_id, owner in chosen:
        job = by_id[job_id]
        job.status = "running"
        job.dispatched_at = now
        job.not_before = None
        await enqueue_task(
            session,
            topic=JOB_TOPIC,
            key=job_id,
            payload={"job_id": job_id},
            deduplication_key=f"supply.job:{job_id}:dispatch:{uuid.uuid4()}",
        )
        await session.execute(
            insert(SupplyTurn)
            .values(queue=queue, owner_subject=owner, served_at=now)
            .on_conflict_do_update(
                index_elements=["queue", "owner_subject"], set_={"served_at": now}
            )
        )
    await session.flush()
    return [job_id for job_id, _ in chosen]
