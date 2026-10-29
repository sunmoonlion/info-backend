"""来源目录、缺口清单、队列监控、原件留存的记录（PostgreSQL，SDD 0045 第 3.1、3.3、3.4 节、
第四节）。"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.supply.catalog import ChannelEvent, Trial, on_site
from app.domain.supply.models import (
    GAP_KINDS,
    REASON_SITE_EXPIRED,
    TERMINAL,
    Channel,
    SupplyError,
)
from app.infrastructure.messaging.durable_tasks import enqueue_task
from app.infrastructure.models.supply import (
    SupplyAttempt,
    SupplyChannel,
    SupplyChannelEvent,
    SupplyChannelTrial,
    SupplyDemand,
    SupplyDemandItem,
    SupplyJob,
    SupplyOriginal,
)
from app.infrastructure.repositories.supply import (
    JOB_TOPIC,
    OPEN_JOB,
    _channel,
    settle_rows,
)

TRIAL_TOPIC = "knowfeed.supply.channel.trial.v1"
REGISTRATION_ACTIONS = ("register", "update", "site_found")


def _event_row(event: ChannelEvent) -> SupplyChannelEvent:
    return SupplyChannelEvent(
        id=uuid.UUID(event.id),
        channel_id=event.channel_id,
        action=event.action,
        actor=event.actor[:255],
        reason=event.reason,
        detail=dict(event.detail),
        at=event.at,
    )


def _event(row: SupplyChannelEvent) -> ChannelEvent:
    return ChannelEvent(
        id=str(row.id),
        channel_id=row.channel_id,
        action=row.action,
        actor=row.actor,
        reason=row.reason,
        detail=dict(row.detail or {}),
        at=row.at,
    )


def _trial(row: SupplyChannelTrial) -> Trial:
    return Trial(
        id=str(row.id),
        channel_id=row.channel_id,
        key=dict(row.key),
        status=row.status,
        requested_by=row.requested_by,
        created_at=row.created_at,
        finished_at=row.finished_at,
        listed=tuple(row.listed or ()),
        fetched=dict(row.fetched) if row.fetched else None,
        error=row.error,
    )


def _row_values(channel: Channel) -> dict[str, object]:
    return {
        "name": channel.name,
        "kind": channel.kind,
        "plugin": channel.plugin,
        "doc_types": list(channel.doc_types),
        "readings": list(channel.readings),
        "license": channel.license,
        "review": channel.review,
        "rate_per_minute": channel.rate_per_minute,
        "daily_cap": channel.daily_cap,
        "priority": channel.priority,
        "status": channel.status,
        "consecutive_failures": channel.consecutive_failures,
        "domain": channel.domain,
        "terms_url": channel.terms_url,
        "render_allowed": channel.render_allowed,
        "obey_robots": channel.obey_robots,
        "status_reason": channel.status_reason,
        "config": dict(channel.config),
        "updated_at": channel.updated_at,
    }


class SqlCatalogRepository:
    def __init__(self, sessions: Callable[[], async_sessionmaker[AsyncSession]]):
        self._sessions = sessions

    async def channels(self) -> list[Channel]:
        async with self._sessions()() as session:
            rows = await session.execute(
                select(SupplyChannel).order_by(SupplyChannel.priority, SupplyChannel.id)
            )
            return [_channel(r) for r in rows.scalars()]

    async def get_channel(self, channel_id: str) -> Channel | None:
        async with self._sessions()() as session:
            row = await session.get(SupplyChannel, channel_id)
            return _channel(row) if row is not None else None

    async def add_channel(self, channel: Channel, event: ChannelEvent) -> None:
        try:
            async with self._sessions()() as session, session.begin():
                session.add(
                    SupplyChannel(
                        id=channel.id,
                        created_at=channel.created_at,
                        **_row_values(channel),
                    )
                )
                await session.flush()
                session.add(_event_row(event))
        except IntegrityError:
            raise SupplyError(
                409, "channel_exists", "this channel id or site is registered"
            ) from None

    async def save_channel(
        self,
        channel: Channel,
        event: ChannelEvent,
        *,
        withdraw: str | None = None,
        requeue: bool = False,
    ) -> None:
        try:
            async with self._sessions()() as session, session.begin():
                await session.execute(
                    update(SupplyChannel)
                    .where(SupplyChannel.id == channel.id)
                    .values(**_row_values(channel))
                )
                session.add(_event_row(event))
                if withdraw:
                    await _withdraw(session, channel.id, withdraw, event.at)
                if requeue:
                    await _requeue_open(session, channel.id, f"resume:{event.id}")
        except IntegrityError:
            raise SupplyError(
                409, "channel_exists", "this site is registered"
            ) from None

    async def reorder(
        self, priorities: dict[str, int], events: list[ChannelEvent], at: datetime
    ) -> None:
        async with self._sessions()() as session, session.begin():
            for channel_id, priority in priorities.items():
                await session.execute(
                    update(SupplyChannel)
                    .where(
                        SupplyChannel.id == channel_id,
                        SupplyChannel.priority != priority,
                    )
                    .values(priority=priority, updated_at=at)
                )
            for event in events:
                session.add(_event_row(event))

    async def events(self, channel_id: str, *, limit: int) -> list[ChannelEvent]:
        async with self._sessions()() as session:
            rows = await session.execute(
                select(SupplyChannelEvent)
                .where(SupplyChannelEvent.channel_id == channel_id)
                .order_by(SupplyChannelEvent.at.desc(), SupplyChannelEvent.id)
                .limit(limit)
            )
            return [_event(r) for r in rows.scalars()]

    async def registration_changed_at(self, channel_id: str) -> datetime | None:
        async with self._sessions()() as session:
            return await session.scalar(
                select(func.max(SupplyChannelEvent.at)).where(
                    SupplyChannelEvent.channel_id == channel_id,
                    SupplyChannelEvent.action.in_(REGISTRATION_ACTIONS),
                )
            )

    async def trials(self, channel_id: str, *, limit: int) -> list[Trial]:
        async with self._sessions()() as session:
            rows = await session.execute(
                select(SupplyChannelTrial)
                .where(SupplyChannelTrial.channel_id == channel_id)
                .order_by(SupplyChannelTrial.created_at.desc())
                .limit(limit)
            )
            return [_trial(r) for r in rows.scalars()]

    async def latest_trial(self, channel_id: str) -> Trial | None:
        found = await self.trials(channel_id, limit=1)
        return found[0] if found else None

    async def add_trial(self, trial: Trial) -> None:
        async with self._sessions()() as session, session.begin():
            session.add(
                SupplyChannelTrial(
                    id=uuid.UUID(trial.id),
                    channel_id=trial.channel_id,
                    key=dict(trial.key),
                    status="queued",
                    listed=[],
                    requested_by=trial.requested_by[:255],
                    created_at=trial.created_at,
                )
            )
            await enqueue_task(
                session,
                topic=TRIAL_TOPIC,
                key=trial.id,
                payload={"trial_id": trial.id},
                deduplication_key=f"supply.trial:{trial.id}",
            )

    async def start_trial(self, trial_id: str, at: datetime) -> Trial | None:
        async with self._sessions()() as session, session.begin():
            row = await session.get(
                SupplyChannelTrial, uuid.UUID(trial_id), with_for_update=True
            )
            if row is None or row.status not in ("queued", "running"):
                return None
            row.status = "running"
            return _trial(row)

    async def finish_trial(
        self,
        trial_id: str,
        at: datetime,
        *,
        status: str,
        listed: list[dict[str, object]] | None = None,
        fetched: dict[str, object] | None = None,
        error: str | None = None,
    ) -> None:
        async with self._sessions()() as session, session.begin():
            await session.execute(
                update(SupplyChannelTrial)
                .where(SupplyChannelTrial.id == uuid.UUID(trial_id))
                .values(
                    status=status,
                    listed=list(listed or []),
                    fetched=fetched,
                    error=error,
                    finished_at=at,
                )
            )

    async def channel_for_host(self, host: str) -> Channel | None:
        """网址落在哪条渠道：主机名是登记的网站或它的子域名；有几条对得上取网站最长的。"""
        async with self._sessions()() as session:
            rows = await session.execute(
                select(SupplyChannel).where(SupplyChannel.domain.is_not(None))
            )
            matches = [r for r in rows.scalars() if on_site(host, r.domain or "")]
        if not matches:
            return None
        return _channel(max(matches, key=lambda r: len(r.domain or "")))

    async def add_site(self, channel: Channel, event: ChannelEvent) -> Channel:
        async with self._sessions()() as session, session.begin():
            inserted = await session.scalar(
                insert(SupplyChannel)
                .values(
                    id=channel.id, created_at=channel.created_at, **_row_values(channel)
                )
                .on_conflict_do_nothing()
                .returning(SupplyChannel.id)
            )
            if inserted is not None:
                session.add(_event_row(event))
        found = await self.channel_for_host(channel.domain or "")
        if found is None:  # 编号撞了别的网站（截断后的编号相同）：照实报
            raise SupplyError(409, "channel_exists", "this channel id is taken")
        return found

    async def pending_sites(self) -> list[dict[str, object]]:
        waiting = (
            select(
                SupplyDemandItem.channel_id.label("channel_id"),
                func.count().label("waiting"),
                func.count(func.distinct(SupplyDemand.owner_subject)).label("users"),
                func.min(SupplyDemandItem.created_at).label("oldest_item_at"),
            )
            .join(SupplyDemand, SupplyDemand.id == SupplyDemandItem.demand_id)
            .where(SupplyDemandItem.status.not_in(tuple(TERMINAL)))
            .group_by(SupplyDemandItem.channel_id)
            .subquery()
        )
        async with self._sessions()() as session:
            rows = await session.execute(
                select(
                    SupplyChannel.id,
                    SupplyChannel.domain,
                    SupplyChannel.created_at,
                    func.coalesce(waiting.c.waiting, 0),
                    func.coalesce(waiting.c.users, 0),
                    waiting.c.oldest_item_at,
                )
                .outerjoin(waiting, waiting.c.channel_id == SupplyChannel.id)
                .where(SupplyChannel.kind == "web", SupplyChannel.review == "pending")
                .order_by(SupplyChannel.created_at)
            )
            return [
                {
                    "channel_id": cid,
                    "domain": domain,
                    "since": since,
                    "waiting_items": int(items),
                    "users": int(users),
                    "oldest_item_at": oldest,
                }
                for cid, domain, since, items, users, oldest in rows.all()
            ]

    async def expire_sites(self, before: datetime, at: datetime) -> int:
        pending = (
            select(SupplyChannel.id)
            .where(SupplyChannel.kind == "web", SupplyChannel.review == "pending")
            .scalar_subquery()
        )
        async with self._sessions()() as session, session.begin():
            return await settle_rows(
                session,
                select(SupplyDemandItem).where(
                    SupplyDemandItem.channel_id.in_(pending),
                    SupplyDemandItem.status.not_in(tuple(TERMINAL)),
                    SupplyDemandItem.created_at < before,
                ),
                "unsatisfiable",
                at,
                reason=REASON_SITE_EXPIRED,
            )

    # —— 缺口清单与队列监控 ——

    async def gaps(self, kind: str | None) -> list[dict[str, object]]:
        kind_of = case(
            *[(SupplyDemandItem.reason == r, k) for r, k in GAP_KINDS.items()],
            else_=None,
        ).label("kind")
        doc_type = SupplyDemandItem.key["doc_type"].astext.label("doc_type")
        subject = SupplyDemandItem.key["subject"].astext
        reasons = [r for r, k in GAP_KINDS.items() if kind is None or k == kind]
        async with self._sessions()() as session:
            rows = await session.execute(
                select(
                    kind_of,
                    doc_type,
                    func.count(),
                    func.count(func.distinct(SupplyDemand.owner_subject)),
                    func.count(func.distinct(subject)),
                    func.array_agg(func.distinct(subject)),
                    func.min(SupplyDemandItem.updated_at),
                    func.max(SupplyDemandItem.updated_at),
                )
                .join(SupplyDemand, SupplyDemand.id == SupplyDemandItem.demand_id)
                .where(
                    SupplyDemandItem.status == "unsatisfiable",
                    SupplyDemandItem.reason.in_(reasons),
                )
                .group_by(kind_of, doc_type)
                .order_by(func.count().desc(), doc_type)
            )
            return [
                {
                    "kind": k,
                    "doc_type": dt,
                    "items": int(n),
                    "users": int(users),
                    "subjects": int(subjects),
                    "sample_subjects": sorted(s for s in (sample or []) if s)[:5],
                    "first_at": first,
                    "last_at": last,
                }
                for k, dt, n, users, subjects, sample, first, last in rows.all()
            ]

    async def queue(self, since: datetime) -> dict[str, object]:
        open_q = SupplyJob.status.in_(OPEN_JOB)
        recent = SupplyJob.finished_at >= since
        async with self._sessions()() as session:
            per_channel = await session.execute(
                select(
                    SupplyJob.channel_id,
                    func.count().filter(SupplyJob.status == "queued"),
                    func.count().filter(SupplyJob.status == "running"),
                    func.count().filter(SupplyJob.status == "waiting"),
                    func.count().filter(SupplyJob.status == "failed", recent),
                    func.count().filter(SupplyJob.status == "done", recent),
                    func.min(SupplyJob.created_at).filter(open_q),
                ).group_by(SupplyJob.channel_id)
            )
            counts = {row[0]: row[1:] for row in per_channel.all()}
            channels = (
                await session.execute(
                    select(SupplyChannel).order_by(
                        SupplyChannel.priority, SupplyChannel.id
                    )
                )
            ).scalars()
            tiers = await session.execute(
                select(SupplyJob.tier, func.count())
                .where(open_q)
                .group_by(SupplyJob.tier)
                .order_by(SupplyJob.tier)
            )
            failures = await session.execute(
                select(SupplyJob.channel_id, SupplyAttempt.reason, func.count())
                .join(SupplyJob, SupplyJob.id == SupplyAttempt.job_id)
                .where(
                    SupplyAttempt.finished_at >= since,
                    SupplyAttempt.outcome.in_(("retry", "failed")),
                )
                .group_by(SupplyJob.channel_id, SupplyAttempt.reason)
                .order_by(func.count().desc())
                .limit(20)
            )
            rows = []
            for c in channels:
                queued, running, waiting, failed, done, oldest = counts.get(
                    c.id, (0, 0, 0, 0, 0, None)
                )
                rows.append(
                    {
                        "channel_id": c.id,
                        "name": c.name,
                        "status": c.status,
                        "status_reason": c.status_reason,
                        "consecutive_failures": c.consecutive_failures,
                        "queued": int(queued),
                        "running": int(running),
                        "waiting": int(waiting),
                        "failed_recent": int(failed),
                        "done_recent": int(done),
                        "oldest_open_at": oldest,
                    }
                )
            return {
                "since": since,
                "channels": rows,
                "tiers": [{"tier": t, "open": int(n)} for t, n in tiers.all()],
                "failures": [
                    {"channel_id": cid, "reason": reason or "", "count": int(n)}
                    for cid, reason, n in failures.all()
                ],
            }

    async def jobs(
        self, *, channel_id: str | None, status: str | None, limit: int
    ) -> list[dict[str, object]]:
        waiting = (
            select(SupplyDemandItem.job_id, func.count().label("waiting"))
            .where(SupplyDemandItem.status.not_in(tuple(TERMINAL)))
            .group_by(SupplyDemandItem.job_id)
            .subquery()
        )
        query = (
            select(SupplyJob, func.coalesce(waiting.c.waiting, 0))
            .outerjoin(waiting, waiting.c.job_id == SupplyJob.id)
            .order_by(SupplyJob.created_at.desc())
            .limit(limit)
        )
        if channel_id:
            query = query.where(SupplyJob.channel_id == channel_id)
        if status:
            query = query.where(SupplyJob.status == status)
        async with self._sessions()() as session:
            rows = await session.execute(query)
            return [_job_row(job, int(items)) for job, items in rows.all()]

    async def job(self, job_id: str) -> dict[str, object] | None:
        try:
            key = uuid.UUID(job_id)
        except ValueError:
            return None
        async with self._sessions()() as session:
            job = await session.get(SupplyJob, key)
            if job is None:
                return None
            channel = await session.get(SupplyChannel, job.channel_id)
            items = await session.scalar(
                select(func.count()).where(
                    SupplyDemandItem.job_id == key,
                    SupplyDemandItem.status.not_in(tuple(TERMINAL)),
                )
            )
            attempts = await session.execute(
                select(SupplyAttempt)
                .where(SupplyAttempt.job_id == key)
                .order_by(SupplyAttempt.started_at)
            )
            return {
                **_job_row(job, int(items or 0)),
                "channel_status": channel.status if channel else "disabled",
                "attempt_list": [
                    {
                        "started_at": a.started_at,
                        "finished_at": a.finished_at,
                        "outcome": a.outcome,
                        "reason": a.reason,
                    }
                    for a in attempts.scalars()
                ],
            }

    async def requeue(self, job_id: str) -> None:
        async with self._sessions()() as session, session.begin():
            row = await session.get(SupplyJob, uuid.UUID(job_id), with_for_update=True)
            if row is None or row.status not in ("queued", "waiting"):
                return
            row.status = "queued"
            await enqueue_task(
                session,
                topic=JOB_TOPIC,
                key=job_id,
                payload={"job_id": job_id},
                deduplication_key=f"supply.job:{job_id}:requeue:{uuid.uuid4()}",
            )

    # —— 原件留存 ——

    async def due_originals(self, now: datetime, limit: int) -> list[tuple[str, str]]:
        async with self._sessions()() as session:
            rows = await session.execute(
                select(SupplyOriginal.id, SupplyOriginal.object_key)
                .where(
                    SupplyOriginal.purge_after <= now,
                    SupplyOriginal.purged_at.is_(None),
                )
                .order_by(SupplyOriginal.purge_after)
                .limit(limit)
            )
            return [(str(i), key) for i, key in rows.all()]

    async def mark_purged(self, original_id: str, at: datetime) -> bool:
        """只在仍到期、没删过时记；这期间又交付了一次（留存从头算）的不记。"""
        async with self._sessions()() as session, session.begin():
            result = await session.execute(
                update(SupplyOriginal)
                .where(
                    SupplyOriginal.id == uuid.UUID(original_id),
                    SupplyOriginal.purged_at.is_(None),
                    SupplyOriginal.purge_after <= at,
                )
                .values(purged_at=at)
            )
            return bool(result.rowcount)


def _job_row(job: SupplyJob, waiting_items: int) -> dict[str, object]:
    return {
        "job_id": str(job.id),
        "kind": job.kind,
        "channel_id": job.channel_id,
        "key": dict(job.payload.get("key") or {}),
        "status": job.status,
        "tier": job.tier,
        "attempts": job.attempts,
        "last_error": job.last_error,
        "created_at": job.created_at,
        "finished_at": job.finished_at,
        "waiting_items": waiting_items,
    }


async def _withdraw(
    session: AsyncSession, channel_id: str, reason: str, at: datetime
) -> None:
    """渠道停用或审不过：没办完的作业取消，在等它的需求项转「无法满足」附原因。"""
    await session.execute(
        update(SupplyJob)
        .where(SupplyJob.channel_id == channel_id, SupplyJob.status.in_(OPEN_JOB))
        .values(status="failed", last_error=reason, finished_at=at)
    )
    await settle_rows(
        session,
        select(SupplyDemandItem).where(
            SupplyDemandItem.channel_id == channel_id,
            SupplyDemandItem.status.not_in(tuple(TERMINAL)),
        ),
        "unsatisfiable",
        at,
        reason=reason,
    )


async def _requeue_open(session: AsyncSession, channel_id: str, tag: str) -> None:
    """渠道恢复：等着的作业马上再排（等重试的那条原排程到时会被跳过或接着跑）。"""
    rows = await session.execute(
        select(SupplyJob).where(
            SupplyJob.channel_id == channel_id,
            SupplyJob.status.in_(("queued", "waiting")),
        )
    )
    for job in rows.scalars():
        job.status = "queued"
        await enqueue_task(
            session,
            topic=JOB_TOPIC,
            key=str(job.id),
            payload={"job_id": str(job.id)},
            deduplication_key=f"supply.job:{job.id}:{tag}",
        )
