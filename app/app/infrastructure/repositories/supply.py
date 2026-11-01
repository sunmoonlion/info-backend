"""供给的记录（PostgreSQL，SDD 0045 第五节）：需求、作业与尝试、原件。

需求项的状态每变一次，在同一个事务里排一条状态通知（`supply_status.py`）；受理时在同一个
事务里按归属用户数配额。作业记下就算排进队列，由调度（`supply_schedule.py`）按渠道、
四档、按用户轮流排出去；这里不直接投给 worker。"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.supply.catalog import url_job
from app.domain.supply.models import (
    REASON_CHANNEL_DISABLED,
    REASON_CONDITIONS_UNMET,
    REASON_DEADLINE,
    REASON_SITE_PENDING,
    REASON_SITE_REJECTED,
    TERMINAL,
    Channel,
    Demand,
    DemandItem,
    DescriptionKey,
    Job,
    Original,
    OriginalDelivery,
    SupplyError,
    can_move,
)
from app.domain.supply.quota import Quota, Usage, check_quota, hour_ago, month_start
from app.domain.supply.schedule import Conditions, demand_tier
from app.infrastructure.models.supply import (
    SupplyAttempt,
    SupplyChannel,
    SupplyChannelEvent,
    SupplyDemand,
    SupplyDemandItem,
    SupplyJob,
    SupplyOriginal,
    SupplyOriginalDelivery,
    SupplySearch,
    SupplyWebSubmission,
)
from app.infrastructure.repositories.supply_status import enqueue_status

JOB_TOPIC = "knowfeed.supply.job.v1"
OPEN_JOB = ("queued", "running", "waiting")
SUBMISSION_TTL = timedelta(minutes=5)


def still_waiting():
    """还在等作业的需求项：没结束的，加上超时了、数据还没到的（0045 第 2.5 节「超时后数据
    到了照样进用户的栏」，二审第 5 条）。"""
    return or_(
        SupplyDemandItem.status.not_in(tuple(TERMINAL)),
        and_(
            SupplyDemandItem.status == "timeout",
            SupplyDemandItem.receipt == text("'{}'::jsonb"),
        ),
    )


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
        max_running=row.max_running,
        domain=row.domain,
        terms_url=row.terms_url,
        render_allowed=row.render_allowed,
        obey_robots=row.obey_robots,
        status_reason=row.status_reason,
        config=dict(row.config or {}),
        zone=row.zone,
        created_at=row.created_at,
        updated_at=row.updated_at,
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
        conditions=dict(row.conditions or {}),
        deadline=row.deadline,
        note=row.note,
    )


def _item(row: SupplyDemandItem) -> DemandItem:
    return DemandItem(
        id=str(row.id),
        demand_id=str(row.demand_id),
        key=DescriptionKey.from_dict(row.key) if row.url is None else None,
        readings=tuple(row.readings),
        confirmation_item=row.confirmation_item,
        status=row.status,
        created_at=row.created_at,
        updated_at=row.updated_at,
        channel_id=row.channel_id,
        reason=row.reason,
        receipt=dict(row.receipt or {}),
        tried_channels=tuple(row.tried_channels or ()),
        url=row.url,
        follow_links=row.follow_links,
        prefill=dict(row.prefill or {}),
        retry_of=str(row.retry_of) if row.retry_of else None,
        declared_public=row.declared_public,
    )


def item_row(item: DemandItem, job_id: uuid.UUID | None) -> SupplyDemandItem:
    return SupplyDemandItem(
        id=uuid.UUID(item.id),
        demand_id=uuid.UUID(item.demand_id),
        key=item.key.as_dict() if item.key is not None else {},
        key_text=item.target_text[:200],
        readings=list(item.readings),
        confirmation_item=item.confirmation_item,
        status=item.status,
        channel_id=item.channel_id,
        job_id=job_id,
        reason=item.reason,
        receipt=dict(item.receipt),
        tried_channels=list(item.tried_channels),
        url=item.url,
        follow_links=item.follow_links,
        prefill=dict(item.prefill),
        retry_of=uuid.UUID(item.retry_of) if item.retry_of else None,
        declared_public=item.declared_public,
        created_at=item.created_at,
        updated_at=item.updated_at,
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
        not_before=row.not_before,
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
        purged_at=row.purged_at,
    )


def _delivery(row: SupplyOriginalDelivery) -> OriginalDelivery:
    return OriginalDelivery(
        id=str(row.id),
        original_id=str(row.original_id),
        channel_id=row.channel_id,
        channel_doc_id=row.channel_doc_id,
        zone=row.zone,
        owner_subject=row.owner_subject,
        knowbase_material_id=row.knowbase_material_id,
        knowbase_version_no=row.knowbase_version_no,
        readings=tuple(row.readings or ()),
        delivered_at=row.delivered_at,
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

    async def get_channel(self, channel_id: str) -> Channel | None:
        async with self._sessions()() as session:
            row = await session.get(SupplyChannel, channel_id)
            return _channel(row) if row is not None else None

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

    async def claim_submission(
        self, owner: str, key: str, digest: str, at: datetime
    ) -> dict[str, object] | None:
        """web端一次提交先占位（二审第 12 条、三审第 8 条）：占到了回 None；这个（用户，钥匙）
        早有了就回那一条（摘要、需求单编号、结果，结果为空是还在办）。若前一次进程崩溃留下半截记录
        且已超过 SUBMISSION_TTL，允许同键接续。"""
        async with self._sessions()() as session, session.begin():
            inserted = await session.scalar(
                insert(SupplyWebSubmission)
                .values(
                    owner_subject=owner,
                    idempotency_key=key,
                    digest=digest,
                    created_at=at,
                )
                .on_conflict_do_nothing()
                .returning(SupplyWebSubmission.digest)
            )
            if inserted is not None:
                return None
            row = await session.get(
                SupplyWebSubmission, (owner, key), with_for_update=True
            )
            assert row is not None
            in_flight = row.result is None
            has_pending = isinstance(row.result, dict) and "_pending" in row.result
            if (in_flight and row.created_at <= at - SUBMISSION_TTL) or has_pending:
                if row.digest == digest:
                    row.created_at = at
                    if row.result is None:
                        return None
                    return {
                        "digest": row.digest,
                        "demand_id": None,
                        "result": None,
                        "resumed": dict(row.result),
                    }
                if row.result is None:
                    row.digest = digest
                    row.created_at = at
                    return None
            return {
                "digest": row.digest,
                "demand_id": str(row.demand_id) if row.demand_id else None,
                "result": None if in_flight else row.result,
            }

    async def save_submission_progress(
        self, owner: str, key: str, progress: dict[str, object]
    ) -> None:
        """记下已向 knowbase 留下的确认单编号：若进程在落库前崩溃，超时后同键重试直接接着用。"""
        async with self._sessions()() as session, session.begin():
            await session.execute(
                update(SupplyWebSubmission)
                .where(
                    SupplyWebSubmission.owner_subject == owner,
                    SupplyWebSubmission.idempotency_key == key,
                )
                .values(result={"_pending": True, **progress})
            )

    async def finish_submission(
        self, owner: str, key: str, demand_id: str | None, result: dict[str, object]
    ) -> None:
        async with self._sessions()() as session, session.begin():
            await session.execute(
                update(SupplyWebSubmission)
                .where(
                    SupplyWebSubmission.owner_subject == owner,
                    SupplyWebSubmission.idempotency_key == key,
                )
                .values(
                    demand_id=uuid.UUID(demand_id) if demand_id else None, result=result
                )
            )

    async def drop_submission(self, owner: str, key: str) -> None:
        """没办成（knowbase 拒了、核不过）：放开这个钥匙，本人改了再提可以用回它。"""
        async with self._sessions()() as session, session.begin():
            row = await session.get(SupplyWebSubmission, (owner, key))
            if row is not None and (row.result is None or "_pending" in row.result):
                await session.delete(row)

    async def item(self, item_id: str) -> tuple[DemandItem, Demand] | None:
        try:
            key = uuid.UUID(item_id)
        except (ValueError, TypeError):
            return None
        async with self._sessions()() as session:
            row = (
                await session.execute(
                    select(SupplyDemandItem, SupplyDemand)
                    .join(SupplyDemand, SupplyDemand.id == SupplyDemandItem.demand_id)
                    .where(SupplyDemandItem.id == key)
                )
            ).first()
            return (_item(row[0]), _demand(row[1])) if row is not None else None

    async def demands_of(
        self, owner: str, limit: int
    ) -> list[tuple[Demand, list[DemandItem]]]:
        """本人名下的需求单（新的在前），连同各项。"""
        async with self._sessions()() as session:
            demands = [
                _demand(r)
                for r in (
                    await session.execute(
                        select(SupplyDemand)
                        .where(SupplyDemand.owner_subject == owner)
                        .order_by(SupplyDemand.created_at.desc())
                        .limit(limit)
                    )
                ).scalars()
            ]
            if not demands:
                return []
            rows = await session.execute(
                select(SupplyDemandItem)
                .where(
                    SupplyDemandItem.demand_id.in_([uuid.UUID(d.id) for d in demands])
                )
                .order_by(SupplyDemandItem.confirmation_item)
            )
            by_demand: dict[str, list[DemandItem]] = {}
            for row in rows.scalars():
                by_demand.setdefault(str(row.demand_id), []).append(_item(row))
            return [(d, by_demand.get(d.id, [])) for d in demands]

    async def expire_deadlines(self, now: datetime) -> int:
        """过了「最晚什么时候要」还没办完的需求项转「超时」（0045 第 2.5 节）；数据以后
        到了照样进用户的栏，不自动重启任务。回转了几项。"""
        late = select(SupplyDemand.id).where(
            SupplyDemand.deadline.is_not(None), SupplyDemand.deadline < now
        )
        async with self._sessions()() as session, session.begin():
            return await settle_rows(
                session,
                select(SupplyDemandItem).where(
                    SupplyDemandItem.demand_id.in_(late.scalar_subquery()),
                    SupplyDemandItem.status.not_in(tuple(TERMINAL)),
                ),
                "timeout",
                now,
                reason=REASON_DEADLINE,
            )

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
        *,
        quota: Quota | None,
    ) -> list[Job]:
        """`quota` 为 None 的不核配额（订阅每轮生成的：订阅建时核过条数，每月上限另算）。
        同一应用同一幂等钥匙并发受理撞了唯一索引：回 409 `demand_exists`，由调用方重读
        已有的那张（独立审第 15 条），不报 500。"""
        try:
            async with self._sessions()() as session, session.begin():
                return await submit_in(session, demand, items, jobs, links, quota)
        except IntegrityError:
            raise SupplyError(
                409, "demand_exists", "a demand with this key exists"
            ) from None

    async def move_items(
        self,
        moves: list[tuple[str, Job, tuple[str, ...], str]],
        at: datetime,
    ) -> None:
        """取不到换下一个渠道（0045 第四节）：每项挂到下一个渠道的作业上（没有就建，有就
        共享，档次取靠前的），记下试过的渠道与上一条为什么没取到；需求项照旧「取数中」。"""
        if not moves:
            return
        async with self._sessions()() as session, session.begin():
            for item_id, job, tried, reason in moves:
                found, _ = await _job_for(session, job)
                await session.execute(
                    update(SupplyDemandItem)
                    .where(
                        SupplyDemandItem.id == uuid.UUID(item_id),
                        SupplyDemandItem.status.not_in(tuple(TERMINAL)),
                    )
                    .values(
                        channel_id=job.channel_id,
                        job_id=found,
                        tried_channels=list(tried),
                        reason=reason[:200],
                        updated_at=at,
                    )
                )

    async def hold_job(self, job_id: str, *, token: str | None = None) -> bool:
        async with self._sessions()() as session, session.begin():
            row = await session.get(SupplyJob, uuid.UUID(job_id), with_for_update=True)
            if row is None:
                return False
            if token is not None:
                if row.status != "running" or str(row.dispatch_token) != token:
                    return False
            elif row.status not in OPEN_JOB:
                return False
            row.status = "waiting"
            row.dispatched_at = None
            row.dispatch_token = None
            row.last_error = "channel_paused"
            return True

    async def channel_failed(
        self, channel_id: str, at: datetime, *, pause_after: int
    ) -> bool:
        async with self._sessions()() as session, session.begin():
            row = await session.get(SupplyChannel, channel_id, with_for_update=True)
            if row is None:
                return False
            row.consecutive_failures += 1
            if row.status != "active" or row.consecutive_failures < pause_after:
                return False
            row.status = "paused"
            if row.review != "pending" or row.status_reason != "re_review":
                row.status_reason = f"auto_paused: {row.consecutive_failures} failures"
            row.updated_at = at
            session.add(
                SupplyChannelEvent(
                    id=uuid.uuid4(),
                    channel_id=channel_id,
                    action="auto_pause",
                    actor="system",
                    reason=f"auto_paused: {row.consecutive_failures} failures",
                    detail={"consecutive_failures": row.consecutive_failures},
                    at=at,
                )
            )
            await settle_channel_searches(session, channel_id, "channel_paused", at)
            return True

    async def channel_succeeded(self, channel_id: str) -> None:
        async with self._sessions()() as session, session.begin():
            await session.execute(
                update(SupplyChannel)
                .where(
                    SupplyChannel.id == channel_id,
                    SupplyChannel.consecutive_failures != 0,
                )
                .values(consecutive_failures=0)
            )

    async def get_job(self, job_id: str) -> Job | None:
        async with self._sessions()() as session:
            row = await session.get(SupplyJob, uuid.UUID(job_id))
            return _job(row) if row is not None else None

    async def start_attempt(
        self, job_id: str, at: datetime, *, token: str | None = None
    ) -> str | None:
        """作业开跑：排队中的改成跑着、记一次尝试。拿着的派发代次不是这一代的、或还有没
        结束的尝试（另一个 worker 在跑）的，不放行，回 None（二审第 6 条、三审第 7 条）。"""
        async with self._sessions()() as session, session.begin():
            row = await session.get(SupplyJob, uuid.UUID(job_id), with_for_update=True)
            if row is None:
                return None
            if token is not None:
                if row.status != "running" or str(row.dispatch_token) != token:
                    return None
            elif row.status not in ("queued", "waiting", "running"):
                return None
            running = await session.scalar(
                select(func.count())
                .select_from(SupplyAttempt)
                .where(
                    SupplyAttempt.job_id == row.id, SupplyAttempt.finished_at.is_(None)
                )
            )
            if running:
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
        not_before: datetime | None = None,
        refund: bool = False,
        token: str | None = None,
    ) -> None:
        async with self._sessions()() as session, session.begin():
            row = await session.get(SupplyJob, uuid.UUID(job_id), with_for_update=True)
            if row is None:
                return
            if token is not None:
                if row.status != "running" or str(row.dispatch_token) != token:
                    return
            await session.execute(
                update(SupplyAttempt)
                .where(SupplyAttempt.id == uuid.UUID(attempt_id))
                .values(finished_at=at, outcome=outcome, reason=(reason or "")[:200])
            )
            status = job_status
            if status in ("done", "failed") and row.kind == "fetch":
                late = await session.scalar(
                    select(func.count())
                    .select_from(SupplyDemandItem)
                    .where(
                        SupplyDemandItem.job_id == row.id,
                        SupplyDemandItem.status.not_in(tuple(TERMINAL)),
                    )
                )
                if late:
                    # 跑的中途挂上来的需求项：这一次没给它们记引用、没结算，再跑一次
                    status = "queued"
                    not_before = None
            row.status = status
            row.last_error = (reason or None) and reason[:200]
            row.not_before = not_before
            row.dispatched_at = None
            row.dispatch_token = None
            if status in ("done", "failed"):
                row.finished_at = at
            if refund:
                row.attempts = max(row.attempts - 1, 0)

    async def waiting_items(self, job_id: str) -> list[tuple[DemandItem, Demand]]:
        async with self._sessions()() as session:
            rows = await session.execute(
                select(SupplyDemandItem, SupplyDemand)
                .join(SupplyDemand, SupplyDemand.id == SupplyDemandItem.demand_id)
                .where(SupplyDemandItem.job_id == uuid.UUID(job_id), still_waiting())
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
        async with self._sessions()() as session, session.begin():
            return await settle_rows(
                session,
                select(SupplyDemandItem).where(
                    SupplyDemandItem.id.in_([uuid.UUID(i) for i in item_ids])
                ),
                status,
                at,
                reason=reason,
                receipt=receipt,
            )

    async def cancel_items(self, demand_id: str, at: datetime, reason: str) -> int:
        items = await self.items_of(demand_id)
        return await self.settle_items(
            [i.id for i in items], "cancelled", at, reason=reason
        )

    async def revive_original(self, original_id: str, purge_after: datetime) -> None:
        """内容放回去了：删除记录清掉，到期时间先按这次取到时算（交付成了再从交付时
        算）；交付一直没成的也不会永远留着（独立审第 13 条）。"""
        async with self._sessions()() as session, session.begin():
            await session.execute(
                update(SupplyOriginal)
                .where(SupplyOriginal.id == uuid.UUID(original_id))
                .values(purged_at=None, purge_after=purge_after)
            )

    async def delivered_original(
        self, channel_id: str, channel_doc_id: str
    ) -> tuple[Original, OriginalDelivery] | None:
        """这条渠道这一份以前交过 knowbase 的公开原件（只认 disclosure 区，三审第 1 条），
        连同那次交付记录；没交过回 None。"""
        async with self._sessions()() as session:
            row = (
                await session.execute(
                    select(SupplyOriginal, SupplyOriginalDelivery)
                    .join(
                        SupplyOriginal,
                        SupplyOriginal.id == SupplyOriginalDelivery.original_id,
                    )
                    .where(
                        SupplyOriginalDelivery.channel_id == channel_id,
                        SupplyOriginalDelivery.channel_doc_id == channel_doc_id,
                        SupplyOriginalDelivery.zone == "disclosure",
                        SupplyOriginalDelivery.owner_subject == "public:disclosure",
                    )
                    .order_by(SupplyOriginalDelivery.delivered_at.desc())
                    .limit(1)
                )
            ).first()
            if row is None:
                return None
            return _original(row[0]), _delivery(row[1])

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
                purge_after=original.purge_after,
            )
            session.add(row)
            await session.flush()
            return _original(row)

    async def mark_delivered(
        self,
        original_id: str,
        at: datetime,
        purge_after: datetime,
        deliveries: Sequence[OriginalDelivery] | None = None,
        *,
        channel_id: str | None = None,
        channel_doc_id: str | None = None,
        zone: str = "disclosure",
    ) -> None:
        """原件行只记交付时刻与留存到期时间，不再存任何人的 material_id；交付回执按
        （渠道，文档编号，区，归属）写进 supply_original_delivery（三审第 1 条）。"""
        async with self._sessions()() as session, session.begin():
            row = await session.get(
                SupplyOriginal, uuid.UUID(original_id), with_for_update=True
            )
            if row is None:
                return
            row.delivered_at = at
            row.purge_after = purge_after
            cid = channel_id or row.channel_id
            doc_id = channel_doc_id or row.channel_doc_id
            for d in deliveries or []:
                stmt = insert(SupplyOriginalDelivery).values(
                    id=uuid.UUID(d.id),
                    original_id=row.id,
                    channel_id=d.channel_id or cid,
                    channel_doc_id=d.channel_doc_id or doc_id,
                    zone=d.zone or zone,
                    owner_subject=d.owner_subject,
                    knowbase_material_id=d.knowbase_material_id,
                    knowbase_version_no=d.knowbase_version_no,
                    readings=sorted(d.readings),
                    delivered_at=d.delivered_at or at,
                )
                await session.execute(
                    stmt.on_conflict_do_update(
                        constraint="uq_supply_original_delivery",
                        set_={
                            "original_id": row.id,
                            "knowbase_material_id": d.knowbase_material_id,
                            "knowbase_version_no": d.knowbase_version_no,
                            "readings": sorted(d.readings),
                            "delivered_at": d.delivered_at or at,
                        },
                    )
                )


async def submit_in(
    session: AsyncSession,
    demand: Demand,
    items: list[DemandItem],
    jobs: list[Job],
    links: dict[str, str],
    quota: Quota | None,
) -> list[Job]:
    """在调用方的事务里记需求单、需求项、作业（订阅每轮生成时与订阅的进度同一个事务）。"""
    if quota is not None:
        await _check_quota(session, demand, items, quota)
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
            conditions=dict(demand.conditions),
            deadline=demand.deadline,
            note=demand.note,
            created_at=demand.created_at,
        )
    )
    await session.flush()
    # 同一渠道同一描述的未办完作业跨用户共享（0045 第 3.3 节）
    by_key: dict[str, uuid.UUID] = {}
    created: list[Job] = []
    for job in jobs:
        found, new = await _job_for(session, job)
        by_key[job.dedup_key] = found
        if new:
            created.append(job)
    for item in items:
        if (
            item.status == "accepted"
            and item.reason == REASON_SITE_PENDING
            and item.channel_id
            and item.url
        ):
            # 锁住渠道行复核：若并发的审核放行/驳回已经先提交，当场挂作业或转无法满足（三审第 13 条）
            ch_row = await session.get(
                SupplyChannel, item.channel_id, with_for_update=True
            )
            if ch_row is not None and ch_row.review != "pending":
                ch = _channel(ch_row)
                if ch.status == "disabled":
                    item = replace(
                        item, status="unsatisfiable", reason=REASON_CHANNEL_DISABLED
                    )
                elif ch.review == "rejected":
                    item = replace(
                        item, status="unsatisfiable", reason=REASON_SITE_REJECTED
                    )
                elif not Conditions.from_dict(demand.conditions).allows(ch):
                    item = replace(
                        item, status="unsatisfiable", reason=REASON_CONDITIONS_UNMET
                    )
                else:
                    job = url_job(
                        ch,
                        item.url,
                        follow_links=item.follow_links,
                        tier=demand_tier(demand.proposer, demand.app),
                        job_id=str(uuid.uuid4()),
                        now=demand.created_at,
                    )
                    found, new = await _job_for(session, job)
                    by_key[job.dedup_key] = found
                    links[item.id] = job.dedup_key
                    if new:
                        created.append(job)
                    item = replace(
                        item,
                        status="fetching",
                        reason=None,
                        tried_channels=(ch.id,),
                    )
        dedup = links.get(item.id)
        session.add(item_row(item, by_key.get(dedup) if dedup else None))
    await session.flush()
    return created


async def _job_for(session: AsyncSession, job: Job) -> tuple[uuid.UUID, bool]:
    """同一去重键的未办完作业跨用户共享：有就用它（新来的档次更靠前就提上去），没有就建。
    回（作业编号，是不是新建的）。两个受理同时建同一个作业时，后到的那个不撞唯一索引报错，
    改为用先建的那个（`ON CONFLICT DO NOTHING` 再锁住读，独立审第 15 条）。

    在跑的作业也共享：跑完时还有跑的中途挂上来的需求项，作业放回队列再跑一次
    （`finish_attempt`，独立审第 2 条）。"""
    inserted = await session.scalar(
        insert(SupplyJob)
        .values(
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
        .on_conflict_do_nothing(
            index_elements=["dedup_key"],
            # 与部分唯一索引 uq_supply_job_open 的条件逐字一致才认得出
            index_where=text("status IN ('queued', 'running', 'waiting')"),
        )
        .returning(SupplyJob.id)
    )
    if inserted is not None:
        return inserted, True
    existing = await session.scalar(
        select(SupplyJob)
        .where(SupplyJob.dedup_key == job.dedup_key, SupplyJob.status.in_(OPEN_JOB))
        .with_for_update()
    )
    if existing is None:  # 刚结束：再建一次
        return await _job_for(session, job)
    if job.tier < existing.tier:
        existing.tier = job.tier
    return existing.id, False


async def settle_rows(
    session: AsyncSession,
    query,
    status: str,
    at: datetime,
    *,
    reason: str | None = None,
    receipt: dict[str, object] | None = None,
) -> int:
    """在调用方的事务里把查到的需求项往前推到 `status`（结束了的不动），给变了的单各排一条
    状态通知。回改了几项。"""
    rows = await session.execute(query.with_for_update())
    changed: set[uuid.UUID] = set()
    count = 0
    for row in rows.scalars():
        if (
            row.status == "timeout"
            and status in ("ready", "partial")
            and receipt is not None
            and not row.receipt
        ):
            # 超时后数据到了：状态不再前进，回执照样记上、照样推一次状态（0045 第 2.5 节）
            row.receipt = dict(receipt)
            row.updated_at = at
            changed.add(row.demand_id)
            count += 1
            continue
        if row.status == "timeout" and status == "cancelled" and bool(row.receipt):
            # 超时后数据已经到了并记上回执的，不再改已取消
            continue
        if not can_move(row.status, status):
            continue  # 迟到的事件不能把已取消、超时改成已就绪
        row.status = status
        row.updated_at = at
        if reason is not None:
            row.reason = reason[:200]
        elif status == "ready":
            row.reason = None  # 换过渠道时记的上一条为什么没取到，办成了就不留
        if receipt is not None:
            row.receipt = dict(receipt)
        changed.add(row.demand_id)
        count += 1
    if changed:
        await session.flush()
        await enqueue_status(session, changed, at)
    return count


async def _check_quota(
    session: AsyncSession, demand: Demand, items: list[DemandItem], quota: Quota
) -> None:
    """按归属用户数配额（0045 第 2.4 节第 4 条）。同一用户的受理排队数，并发也不会一起超。

    每小时新单、在途单数只数本人亲手提与应用、专家代提的：订阅每轮生成的单不挤这两项
    （订阅条数另有上限，二审第 7 条）；每月新取份数订阅生成的也算，订阅每轮结尾按同一个
    锁核（`supply_subscription.finish_round`）。"""
    owner = demand.owner
    await lock_quota(session, owner)
    now = demand.created_at
    last_hour = await session.scalar(
        select(func.count())
        .select_from(SupplyDemand)
        .where(
            SupplyDemand.owner_subject == owner,
            SupplyDemand.proposer != "subscription",
            SupplyDemand.created_at >= hour_ago(now),
        )
    )
    open_demands = await session.scalar(
        select(func.count(func.distinct(SupplyDemandItem.demand_id)))
        .join(SupplyDemand, SupplyDemand.id == SupplyDemandItem.demand_id)
        .where(
            SupplyDemand.owner_subject == owner,
            SupplyDemand.proposer != "subscription",
            SupplyDemandItem.status.not_in(tuple(TERMINAL)),
        )
    )
    this_month = await month_used(session, owner, now)
    check_quota(
        quota,
        Usage(int(last_hour or 0), int(open_demands or 0), this_month),
        new_items=sum(1 for i in items if i.channel_id is not None),
    )


async def lock_quota(session: AsyncSession, owner: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"knowfeed.supply.quota:{owner}"},
    )


async def month_used(session: AsyncSession, owner: str, now: datetime) -> int:
    """这位用户本月新取了几份（有渠道的需求项，订阅生成的也算）。"""
    return int(
        await session.scalar(
            select(func.count())
            .select_from(SupplyDemandItem)
            .join(SupplyDemand, SupplyDemand.id == SupplyDemandItem.demand_id)
            .where(
                SupplyDemand.owner_subject == owner,
                SupplyDemandItem.channel_id.is_not(None),
                SupplyDemandItem.created_at >= month_start(now),
            )
        )
        or 0
    )


async def settle_channel_searches(
    session: AsyncSession, channel_id: str, reason: str, at: datetime
) -> None:
    """渠道停用、审不过或暂停时：取消它上面的在途搜作业，没有别的在途搜作业的搜索当场结清（三审第 5 条）。"""
    open_rows = list(
        (
            await session.execute(
                select(SupplyJob).where(
                    SupplyJob.channel_id == channel_id,
                    SupplyJob.kind == "search",
                    SupplyJob.status.in_(OPEN_JOB),
                )
            )
        ).scalars()
    )
    if not open_rows:
        return
    search_ids: set[uuid.UUID] = set()
    for job in open_rows:
        job.status = "failed"
        job.last_error = reason[:200]
        job.finished_at = at
        job.dispatched_at = None
        job.dispatch_token = None
        sid = (job.payload or {}).get("search_id")
        if isinstance(sid, str):
            try:
                search_ids.add(uuid.UUID(sid))
            except ValueError:
                pass
    await session.flush()
    for search_uuid in search_ids:
        search = await session.get(SupplySearch, search_uuid, with_for_update=True)
        if search is None or search.status != "searching":
            continue
        prefix = f"search:{search_uuid}:"
        still_open = await session.scalar(
            select(func.count())
            .select_from(SupplyJob)
            .where(
                SupplyJob.kind == "search",
                SupplyJob.status.in_(OPEN_JOB),
                SupplyJob.dedup_key.like(f"{prefix}%"),
            )
        )
        if still_open:
            continue
        if search.candidates:
            search.status = "ready"
            search.expires_at = at + timedelta(days=7)
            search.note = reason[:200]
        else:
            search.status = "ended"
            search.note = reason[:200]
