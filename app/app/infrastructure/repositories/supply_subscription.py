"""订阅、发现游标、按主题找的记录（PostgreSQL，SDD 0045 第 2.2、2.5 节，第 3.3 节，第五节）。

- 订阅与每轮记录；各订阅自己的进度（哪一份取了、本月超额没取、跳过）。
- 渠道的发现与游标：订同一个对象的人共用，看新的作业写，各订阅各自取用。
- 一轮的结尾（记进度、生成需求单、排作业、记这一轮）在一个事务里做完，订阅行锁住，
  同一轮不会记两次。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, time

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.supply.models import (
    REASON_CHANNEL_DISABLED,
    TERMINAL,
    Demand,
    DemandItem,
    Entry,
    Job,
    SupplyError,
)
from app.domain.supply.quota import BEIJING
from app.domain.supply.subscription import (
    CANDIDATES_VALID,
    LIVE,
    Round,
    Search,
    Subscription,
)
from app.infrastructure.models.supply import (
    SupplyChannel,
    SupplyDemand,
    SupplyDemandItem,
    SupplyDiscovery,
    SupplyJob,
    SupplySearch,
    SupplySubscription,
    SupplySubscriptionRound,
    SupplySubscriptionTake,
    SupplyWatchCursor,
)
from app.infrastructure.repositories.supply import (
    OPEN_JOB,
    _job_for,
    lock_quota,
    month_used,
    settle_rows,
    submit_in,
)


def _subscription(row: SupplySubscription) -> Subscription:
    return Subscription(
        id=str(row.id),
        owner=row.owner_subject,
        by=row.by,
        readings=tuple(row.readings),
        every_hours=row.every_hours,
        starts_on=row.starts_on,
        ends_on=row.ends_on,
        monthly_cap=row.monthly_cap,
        status=row.status,
        revision=row.revision,
        purpose=row.purpose,
        created_at=row.created_at,
        updated_at=row.updated_at,
        subjects=tuple(row.subjects),
        doc_types=tuple(row.doc_types),
        urls=tuple(row.urls),
        topic=row.topic,
        status_reason=row.status_reason,
        skip_backlog=row.skip_backlog,
        next_check_at=row.next_check_at,
        last_checked_at=row.last_checked_at,
        round_id=str(row.round_id) if row.round_id else None,
        channels=dict(row.channels or {}),
    )


def _values(sub: Subscription) -> dict[str, object]:
    return {
        "owner_subject": sub.owner,
        "by": sub.by,
        "subjects": list(sub.subjects),
        "doc_types": list(sub.doc_types),
        "urls": list(sub.urls),
        "topic": sub.topic,
        "readings": list(sub.readings),
        "every_hours": sub.every_hours,
        "starts_on": sub.starts_on,
        "ends_on": sub.ends_on,
        "monthly_cap": sub.monthly_cap,
        "purpose": sub.purpose,
        "status": sub.status,
        "status_reason": sub.status_reason,
        "revision": sub.revision,
        "skip_backlog": sub.skip_backlog,
        "channels": dict(sub.channels),
        "next_check_at": sub.next_check_at,
        "last_checked_at": sub.last_checked_at,
        "round_id": uuid.UUID(sub.round_id) if sub.round_id else None,
        "updated_at": sub.updated_at,
    }


def _round(row: SupplySubscriptionRound) -> Round:
    return Round(
        id=str(row.id),
        subscription_id=str(row.subscription_id),
        started_at=row.started_at,
        finished_at=row.finished_at,
        found=row.found,
        taken=row.taken,
        over_cap=row.over_cap,
        demand_id=str(row.demand_id) if row.demand_id else None,
        note=row.note,
    )


def _search(row: SupplySearch) -> Search:
    return Search(
        id=str(row.id),
        owner=row.owner_subject,
        topic=row.topic,
        sites=tuple(row.sites or ()),
        wanted=row.wanted,
        status=row.status,
        created_at=row.created_at,
        candidates=tuple(row.candidates or ()),
        expires_at=row.expires_at,
        subscription_id=str(row.subscription_id) if row.subscription_id else None,
        note=row.note,
    )


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError):
        return None


class SqlSubscriptionRepository:
    def __init__(self, sessions: Callable[[], async_sessionmaker[AsyncSession]]):
        self._sessions = sessions

    # —— 订阅 ——

    async def add(self, sub: Subscription, *, max_live: int | None = None) -> None:
        async with self._sessions()() as session, session.begin():
            if max_live is not None:
                await lock_quota(session, f"sub:{sub.owner}")
                live = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(SupplySubscription)
                        .where(
                            SupplySubscription.owner_subject == sub.owner,
                            SupplySubscription.status.in_(tuple(LIVE)),
                        )
                    )
                    or 0
                )
                if live >= max_live:
                    raise SupplyError(
                        429, "quota_subscriptions", "too many subscriptions"
                    )
            session.add(
                SupplySubscription(
                    id=uuid.UUID(sub.id), created_at=sub.created_at, **_values(sub)
                )
            )

    async def remove(self, subscription_id: str) -> None:
        key = _uuid(subscription_id)
        if key is None:
            return
        async with self._sessions()() as session, session.begin():
            await session.execute(
                delete(SupplySubscription).where(SupplySubscription.id == key)
            )

    async def change(
        self,
        subscription_id: str,
        values: dict[str, object],
        *,
        when_status: tuple[str, ...] | None = None,
        when_revision: int | None = None,
    ) -> Subscription | None:
        """只改给的栏位，带条件（状态是这几种、修订还是这一版）；条件不成立回 None。不整行
        覆盖：后台一轮与本人暂停、续订同时写时，谁也不抹掉谁的改动（二审第 9 条）。"""
        key = _uuid(subscription_id)
        if key is None:
            return None
        clean = dict(values)
        if "round_id" in clean:
            clean["round_id"] = (
                uuid.UUID(str(clean["round_id"])) if clean["round_id"] else None
            )
        query = update(SupplySubscription).where(SupplySubscription.id == key)
        if when_status is not None:
            query = query.where(SupplySubscription.status.in_(when_status))
        if when_revision is not None:
            query = query.where(SupplySubscription.revision == when_revision)
        async with self._sessions()() as session, session.begin():
            row = await session.scalar(
                query.values(**clean).returning(SupplySubscription)
            )
            return _subscription(row) if row is not None else None

    async def get(self, subscription_id: str) -> Subscription | None:
        key = _uuid(subscription_id)
        if key is None:
            return None
        async with self._sessions()() as session:
            row = await session.get(SupplySubscription, key)
            return _subscription(row) if row is not None else None

    async def of_owner(self, owner: str) -> list[Subscription]:
        async with self._sessions()() as session:
            rows = await session.execute(
                select(SupplySubscription)
                .where(SupplySubscription.owner_subject == owner)
                .order_by(SupplySubscription.created_at.desc())
                .limit(200)
            )
            return [_subscription(r) for r in rows.scalars()]

    async def count_live(self, owner: str) -> int:
        async with self._sessions()() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(SupplySubscription)
                    .where(
                        SupplySubscription.owner_subject == owner,
                        SupplySubscription.status.in_(tuple(LIVE)),
                    )
                )
                or 0
            )

    async def due(self, now: datetime, limit: int) -> list[Subscription]:
        """到点该查的（生效中、没有正在跑的一轮），连同已经过了截止日的——那些不看下次
        查的时间、也不看有没有在跑的一轮，马上转「已到期」（二审第 8 条）。"""
        today = now.astimezone(BEIJING).date()
        async with self._sessions()() as session:
            rows = await session.execute(
                select(SupplySubscription)
                .where(
                    SupplySubscription.status.in_(tuple(LIVE)),
                    or_(
                        SupplySubscription.ends_on < today,
                        and_(
                            SupplySubscription.round_id.is_(None),
                            SupplySubscription.next_check_at.is_not(None),
                            SupplySubscription.next_check_at <= now,
                        ),
                    ),
                )
                .order_by(SupplySubscription.next_check_at)
                .limit(limit)
            )
            return [_subscription(r) for r in rows.scalars()]

    async def watching(self, limit: int) -> list[Subscription]:
        async with self._sessions()() as session:
            rows = await session.execute(
                select(SupplySubscription)
                .where(
                    SupplySubscription.status.in_(tuple(LIVE)),
                    SupplySubscription.round_id.is_not(None),
                )
                .order_by(SupplySubscription.updated_at)
                .limit(limit)
            )
            return [_subscription(r) for r in rows.scalars()]

    async def rounds(self, subscription_id: str, limit: int) -> list[Round]:
        key = _uuid(subscription_id)
        if key is None:
            return []
        async with self._sessions()() as session:
            rows = await session.execute(
                select(SupplySubscriptionRound)
                .where(SupplySubscriptionRound.subscription_id == key)
                .order_by(SupplySubscriptionRound.started_at.desc())
                .limit(limit)
            )
            return [_round(r) for r in rows.scalars()]

    async def start_round(
        self, sub: Subscription, round_: Round, jobs: list[Job]
    ) -> bool:
        """记这一轮开始、排看新的作业（同一对象的已有作业照用）。订阅行加行锁且仍在生效中、
        没有正在跑的一轮时才占位成功（三审第 9 条）。"""
        async with self._sessions()() as session, session.begin():
            row = await session.scalar(
                select(SupplySubscription)
                .where(
                    SupplySubscription.id == uuid.UUID(sub.id),
                    SupplySubscription.round_id.is_(None),
                    SupplySubscription.status == "active",
                )
                .with_for_update()
            )
            if row is None:
                return False
            session.add(
                SupplySubscriptionRound(
                    id=uuid.UUID(round_.id),
                    subscription_id=uuid.UUID(sub.id),
                    started_at=round_.started_at,
                    found=0,
                    taken=0,
                    over_cap=0,
                    note=round_.note,
                )
            )
            for job in jobs:
                await _job_for(session, job)
            row.round_id = uuid.UUID(round_.id)
            row.updated_at = round_.started_at
            return True

    async def set_round_search(
        self, subscription_id: str, round_id: str, search_id: str, at: datetime
    ) -> bool:
        key = _uuid(subscription_id)
        rkey = _uuid(round_id)
        if key is None or rkey is None:
            return False
        async with self._sessions()() as session, session.begin():
            result = await session.execute(
                update(SupplySubscription)
                .where(
                    SupplySubscription.id == key,
                    SupplySubscription.round_id == rkey,
                    SupplySubscription.status.in_(tuple(LIVE)),
                )
                .values(channels={"search": search_id}, updated_at=at)
            )
            return bool(result.rowcount)

    async def open_jobs(self, dedup_keys: list[str]) -> int:
        if not dedup_keys:
            return 0
        async with self._sessions()() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(SupplyJob)
                    .where(
                        SupplyJob.dedup_key.in_(dedup_keys),
                        SupplyJob.status.in_(OPEN_JOB),
                    )
                )
                or 0
            )

    async def fresh(
        self, sub: Subscription, pairs: list[tuple[str, str]]
    ) -> list[tuple[str, dict[str, object], date | None, datetime]]:
        """这条订阅的各对象上发现了、这条订阅还没处理过的条目：
        （对象，条目，披露日期，发现时间）。"""
        if not pairs:
            return []
        taken = select(SupplySubscriptionTake.channel_doc_id).where(
            SupplySubscriptionTake.subscription_id == uuid.UUID(sub.id),
            SupplySubscriptionTake.channel_id == SupplyDiscovery.channel_id,
        )
        out = []
        since = datetime.combine(sub.starts_on, time(0, 0), tzinfo=BEIJING)
        async with self._sessions()() as session:
            for channel_id, target in pairs:
                rows = await session.execute(
                    select(SupplyDiscovery)
                    .where(
                        SupplyDiscovery.channel_id == channel_id,
                        SupplyDiscovery.target == target[:400],
                        SupplyDiscovery.channel_doc_id.not_in(taken),
                        # 订阅起始日之前的这条订阅永远不取：不从最老的扫起（二审第 19 条）
                        or_(
                            SupplyDiscovery.disclosed_on >= sub.starts_on,
                            and_(
                                SupplyDiscovery.disclosed_on.is_(None),
                                SupplyDiscovery.found_at >= since,
                            ),
                        ),
                    )
                    .order_by(SupplyDiscovery.found_at, SupplyDiscovery.channel_doc_id)
                    .limit(2000)
                )
                out += [
                    (target, dict(r.entry), r.disclosed_on, r.found_at)
                    for r in rows.scalars()
                ]
        return out

    async def backlog(self, sub: Subscription) -> list[tuple[str, dict[str, object]]]:
        """本月以前超额没取的（下月初优先补取）：（对象，条目）。"""
        async with self._sessions()() as session:
            rows = await session.execute(
                select(SupplySubscriptionTake)
                .where(
                    SupplySubscriptionTake.subscription_id == uuid.UUID(sub.id),
                    SupplySubscriptionTake.status == "over_cap",
                )
                .order_by(SupplySubscriptionTake.at)
            )
            return [(r.target, dict(r.entry)) for r in rows.scalars()]

    async def taken_since(self, sub: Subscription, since: datetime) -> int:
        async with self._sessions()() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(SupplySubscriptionTake)
                    .where(
                        SupplySubscriptionTake.subscription_id == uuid.UUID(sub.id),
                        SupplySubscriptionTake.status == "taken",
                        SupplySubscriptionTake.at >= since,
                    )
                )
                or 0
            )

    async def finish_round(
        self,
        sub: Subscription,
        round_: Round,
        takes: list[tuple[str, str, dict[str, object], str, str | None]],
        demand: tuple[Demand, list[DemandItem], list[Job], dict[str, str]] | None,
        *,
        next_check_at: datetime,
        at: datetime,
        owner_monthly: int | None = None,
    ) -> bool:
        """一轮的结尾，一个事务：记各条的进度（`takes`：对象、状态、条目、渠道内编号、
        取的那一项的编号），生成需求单与作业，记这一轮的数，订阅回到空闲、定下次查的时间。
        这一轮已经结过的（订阅行上的轮次不是它）什么都不做，回 False。

        `owner_monthly` 是这位用户每人每月新取份数的配额：在同一个事务里（同受理时那把
        配额锁）数这个月已经取了几份，超出的那几项不生成，记「本月超额未取」，下月补
        （二审第 7 条）。"""
        async with self._sessions()() as session, session.begin():
            row = await session.get(
                SupplySubscription, uuid.UUID(sub.id), with_for_update=True
            )
            if row is None or str(row.round_id) != round_.id:
                return False
            if demand is not None and owner_monthly is not None:
                await lock_quota(session, sub.owner)
                room = max(0, owner_monthly - await month_used(session, sub.owner, at))
                made, items, jobs, links = demand
                if len(items) > room:
                    dropped = {i.id for i in items[room:]}
                    items = items[:room]
                    links = {k: v for k, v in links.items() if k not in dropped}
                    used = set(links.values())
                    jobs = [j for j in jobs if j.dedup_key in used]
                    takes = [
                        (t, "over_cap" if item_id in dropped else st, e, d, item_id)
                        for t, st, e, d, item_id in takes
                    ]
                    round_ = replace(
                        round_,
                        taken=len(items),
                        over_cap=round_.over_cap + len(dropped),
                    )
                    demand = (made, items, jobs, links) if items else None
            for target, status, entry, doc_id, _ in takes:
                await session.execute(
                    insert(SupplySubscriptionTake)
                    .values(
                        subscription_id=row.id,
                        channel_id=str(entry["channel_id"]),
                        channel_doc_id=doc_id[:200],
                        status=status,
                        entry=entry,
                        target=target[:400],
                        round_id=uuid.UUID(round_.id),
                        at=at,
                    )
                    .on_conflict_do_update(
                        index_elements=[
                            "subscription_id",
                            "channel_id",
                            "channel_doc_id",
                        ],
                        set_={
                            "status": status,
                            "round_id": uuid.UUID(round_.id),
                            "at": at,
                        },
                    )
                )
            demand_id = None
            if demand is not None:
                made, items, jobs, links = demand
                await submit_in(session, made, items, jobs, links, None)
                demand_id = uuid.UUID(made.id)
            await session.execute(
                update(SupplySubscriptionRound)
                .where(SupplySubscriptionRound.id == uuid.UUID(round_.id))
                .values(
                    finished_at=at,
                    found=round_.found,
                    taken=round_.taken,
                    over_cap=round_.over_cap,
                    demand_id=demand_id,
                    note=round_.note,
                )
            )
            row.round_id = None
            row.last_checked_at = at
            row.next_check_at = next_check_at
            row.updated_at = at
            return True

    async def end_items(self, sub: Subscription, reason: str, at: datetime) -> int:
        """订阅到期、撤了：它生成的在途需求项转「已取消」（0045 第 2.5 节）。"""
        async with self._sessions()() as session, session.begin():
            return await settle_rows(
                session,
                select(SupplyDemandItem)
                .join(SupplyDemand, SupplyDemand.id == SupplyDemandItem.demand_id)
                .where(
                    SupplyDemand.confirmation_id.like(f"subscription:{sub.id}@%"),
                    SupplyDemandItem.status.not_in(tuple(TERMINAL)),
                ),
                "cancelled",
                at,
                reason=reason,
            )

    # —— 发现与游标 ——

    async def cursor(self, channel_id: str, target: str) -> dict[str, object]:
        async with self._sessions()() as session:
            row = await session.get(SupplyWatchCursor, (channel_id, target[:400]))
            return dict(row.cursor) if row is not None else {}

    async def save_watch(
        self,
        channel_id: str,
        target: str,
        found: list[tuple[str, dict[str, object], date | None]],
        cursor: dict[str, object],
        at: datetime,
    ) -> int:
        """记这一次看到的新条目（`found`：渠道内编号、条目、披露日期）与新游标。回新记了几条。"""
        made = 0
        async with self._sessions()() as session, session.begin():
            for doc_id, entry, disclosed in found:
                result = await session.execute(
                    insert(SupplyDiscovery)
                    .values(
                        channel_id=channel_id,
                        target=target[:400],
                        channel_doc_id=doc_id[:200],
                        entry=entry,
                        disclosed_on=disclosed,
                        found_at=at,
                    )
                    .on_conflict_do_nothing()
                )
                made += int(result.rowcount or 0)
            await session.execute(
                insert(SupplyWatchCursor)
                .values(
                    channel_id=channel_id,
                    target=target[:400],
                    cursor=cursor,
                    checked_at=at,
                )
                .on_conflict_do_update(
                    index_elements=["channel_id", "target"],
                    set_={"cursor": cursor, "checked_at": at},
                )
            )
        return made

    async def prune_discoveries(self, before: datetime) -> int:
        """发现记录只留订阅还用得到的：订阅最长一年，早于 `before`（一年多以前）发现的、
        又已被各订阅处理过的不再有用，删掉（二审第 19 条）。"""
        async with self._sessions()() as session, session.begin():
            result = await session.execute(
                delete(SupplyDiscovery).where(SupplyDiscovery.found_at < before)
            )
            return int(result.rowcount or 0)  # type: ignore[attr-defined]

    # —— 按主题找 ——

    async def add_search(
        self,
        search: Search,
        jobs: list[Job],
        candidates: list[dict[str, object]] | None = None,
    ) -> None:
        """记一次搜索与它的作业；`candidates` 是复用的结果（短时间里同一渠道同一话题的）。"""
        reused = [
            {**c, "ref": f"k{n}"} for n, c in enumerate(candidates or [], start=1)
        ]
        async with self._sessions()() as session, session.begin():
            session.add(
                SupplySearch(
                    id=uuid.UUID(search.id),
                    owner_subject=search.owner,
                    topic=search.topic,
                    sites=list(search.sites),
                    wanted=search.wanted,
                    status=search.status,
                    candidates=reused,
                    note=search.note,
                    subscription_id=uuid.UUID(search.subscription_id)
                    if search.subscription_id
                    else None,
                    created_at=search.created_at,
                    expires_at=search.expires_at,
                )
            )
            await session.flush()
            for job in jobs:
                await _job_for(session, job)

    async def count_searches(self, owner: str, since: datetime) -> int:
        async with self._sessions()() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(SupplySearch)
                    .where(
                        SupplySearch.owner_subject == owner,
                        SupplySearch.subscription_id.is_(None),
                        SupplySearch.created_at >= since,
                    )
                )
                or 0
            )

    async def open_searches(self, owner: str) -> int:
        async with self._sessions()() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(SupplySearch)
                    .where(
                        SupplySearch.owner_subject == owner,
                        SupplySearch.subscription_id.is_(None),
                        SupplySearch.status == "searching",
                    )
                )
                or 0
            )

    async def recent_candidates(
        self, channel_id: str, topic: str, since: datetime
    ) -> list[dict[str, object]] | None:
        """`since` 以后同一话题、这条渠道已经搜完的那一次的候选（可能是空的）；没有回 None。"""
        async with self._sessions()() as session:
            rows = await session.execute(
                select(SupplySearch)
                .where(
                    SupplySearch.topic == topic,
                    SupplySearch.created_at >= since,
                    SupplySearch.status.in_(("ready", "ended")),
                    SupplySearch.note.is_distinct_from("no_search_channel"),
                )
                .order_by(SupplySearch.created_at.desc())
                .limit(20)
            )
            for row in rows.scalars():
                asked = await session.scalar(
                    select(func.count())
                    .select_from(SupplyJob)
                    .where(
                        SupplyJob.dedup_key == f"search:{row.id}:{channel_id}",
                        SupplyJob.status == "done",
                    )
                )
                if asked:
                    return [
                        dict(c)
                        for c in row.candidates or []
                        if c.get("channel_id") == channel_id
                    ]
            return None

    async def get_search(self, search_id: str) -> Search | None:
        key = _uuid(search_id)
        if key is None:
            return None
        async with self._sessions()() as session:
            row = await session.get(SupplySearch, key)
            return _search(row) if row is not None else None

    async def searches_of(self, owner: str, limit: int) -> list[Search]:
        async with self._sessions()() as session:
            rows = await session.execute(
                select(SupplySearch)
                .where(SupplySearch.owner_subject == owner)
                .order_by(SupplySearch.created_at.desc())
                .limit(limit)
            )
            return [_search(r) for r in rows.scalars()]

    async def add_candidates(
        self,
        search_id: str,
        job_id: str,
        found: list[dict[str, object]],
        *,
        note: str | None,
        ready_at: datetime,
        expires_at: datetime,
    ) -> Search | None:
        """记这条渠道搜到的候选；这次搜索的作业都跑完了（除了这一个）就转「候选已出」。"""
        async with self._sessions()() as session, session.begin():
            row = await session.get(
                SupplySearch, uuid.UUID(search_id), with_for_update=True
            )
            if row is None or row.status != "searching":
                return None
            have = list(row.candidates or [])
            row.candidates = have + [
                {**c, "ref": f"k{len(have) + n + 1}"} for n, c in enumerate(found)
            ]
            if note:
                row.note = note[:200]
            others = await session.scalar(
                select(func.count())
                .select_from(SupplyJob)
                .where(
                    SupplyJob.dedup_key.like(f"search:{search_id}:%"),
                    SupplyJob.status.in_(OPEN_JOB),
                    SupplyJob.id != uuid.UUID(job_id),
                )
            )
            if not others:
                row.status = "ready"
                row.expires_at = expires_at
            return _search(row)

    async def end_search(self, search_id: str) -> None:
        async with self._sessions()() as session, session.begin():
            await session.execute(
                update(SupplySearch)
                .where(SupplySearch.id == uuid.UUID(search_id))
                .values(status="ended")
            )

    async def claim_search_pick(self, search_id: str, owner: str) -> bool:
        """勾选候选前原子抢占 `status = 'ready' -> 'ended'`（三审第 8 条）。"""
        key = _uuid(search_id)
        if key is None:
            return False
        async with self._sessions()() as session, session.begin():
            result = await session.execute(
                update(SupplySearch)
                .where(
                    SupplySearch.id == key,
                    SupplySearch.owner_subject == owner,
                    SupplySearch.status == "ready",
                )
                .values(status="ended")
            )
            return bool(result.rowcount)

    async def restore_search_ready(self, search_id: str) -> None:
        key = _uuid(search_id)
        if key is None:
            return
        async with self._sessions()() as session, session.begin():
            await session.execute(
                update(SupplySearch)
                .where(SupplySearch.id == key, SupplySearch.status == "ended")
                .values(status="ready")
            )

    async def expire_searches(self, now: datetime) -> int:
        """候选出了 7 天没人勾：作废（0045 第 2.5 节）。同时巡检卡在 `searching` 的搜索：
        其下挂在非 active/approved 渠道上的 search 作业转失败，已无未完 search 作业的收口为
        `ready`（有候选）或 `ended`（无候选）（三审第 5 条）。"""
        async with self._sessions()() as session, session.begin():
            result = await session.execute(
                update(SupplySearch)
                .where(SupplySearch.status == "ready", SupplySearch.expires_at <= now)
                .values(status="ended", note="expired")
            )
            changed = int(result.rowcount or 0)
            searching = (
                (
                    await session.execute(
                        select(SupplySearch)
                        .where(SupplySearch.status == "searching")
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            if searching:
                channels = {
                    c.id: c
                    for c in (await session.execute(select(SupplyChannel))).scalars()
                }
                for row in searching:
                    prefix = f"search:{row.id}:"
                    jobs = (
                        (
                            await session.execute(
                                select(SupplyJob).where(
                                    SupplyJob.dedup_key.like(f"{prefix}%"),
                                    SupplyJob.status.in_(OPEN_JOB),
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    remaining = 0
                    for job in jobs:
                        ch = channels.get(job.channel_id)
                        if (
                            ch is None
                            or ch.status != "active"
                            or ch.review != "approved"
                        ):
                            job.status = "failed"
                            job.last_error = REASON_CHANNEL_DISABLED
                            job.finished_at = now
                            job.updated_at = now
                            job.dispatch_token = None
                            job.dispatched_at = None
                        else:
                            remaining += 1
                    if remaining == 0:
                        if list(row.candidates or []):
                            row.status = "ready"
                            row.expires_at = now + CANDIDATES_VALID
                        else:
                            row.status = "ended"
                            row.note = row.note or "channel_unavailable"
                        changed += 1
            return changed


def entry_of(data: dict[str, object]) -> Entry:
    return Entry.from_dict(data)
