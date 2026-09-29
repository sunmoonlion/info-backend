"""采集申请在真实数据库上：迁移、账本、关注清单、和采集批次一起提交。"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from security_requests_support import ALICE, BOB, OWNER, Clock
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_durable_delivery_db import db as db
from test_durable_delivery_db import sql
from test_security_dataset_db import MemoryStorage

from app.application.ports.security_requests import NewRequester, OpenRequestExists
from app.application.securities.request_service import SecurityRequestService
from app.bootstrap import securities as wiring
from app.bootstrap.securities import (
    record_dataset_build_refusal,
    start_security_ingestion,
)
from app.domain.securities import SecurityCode
from app.domain.securities.requests import (
    SYSTEM_WATCHLIST,
    Progress,
    RequestError,
    RequestKind,
    RequestStatus,
)
from app.infrastructure.securities.request_store import SqlRequestStore, SqlWatchlist

NOW = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)
FIRST_TEN = [
    "000858",
    "002415",
    "300750",
    "600009",
    "600276",
    "600519",
    "600900",
    "601888",
    "601899",
    "920185",
]


@pytest.fixture
def sessions(db, monkeypatch):
    monkeypatch.setattr(wiring, "get_object_storage", MemoryStorage)
    return async_sessionmaker(db.kw["bind"], autocommit=False, autoflush=False)


def service_on(session, sessions, **options) -> SecurityRequestService:
    async def start(active, code):
        return str((await start_security_ingestion(active, sessions, code)).id)

    return SecurityRequestService(
        store=SqlRequestStore(session, start_ingestion=start),
        watchlist=SqlWatchlist(session),
        clock=Clock(),
        known_apps=frozenset({"investment", "knowledge"}),
        **options,
    )


async def submit(sessions, code, actor, **kwargs):
    async with sessions() as session:
        return await service_on(session, sessions).submit(
            code, actor_id=actor, **kwargs
        )


# ---------------------------------------------------------------- 迁移
async def test_migration_seeds_the_first_watchlist_with_a_trace(db):
    async with db() as s:
        listed = (
            await s.execute(
                text(
                    "SELECT security_code, added_by FROM security_watchlist "
                    "WHERE removed_at IS NULL ORDER BY security_code"
                )
            )
        ).all()
        logged = (
            await s.execute(
                text(
                    "SELECT security_code, action FROM security_watchlist_log "
                    "ORDER BY security_code"
                )
            )
        ).all()
    assert [row[0] for row in listed] == FIRST_TEN
    assert {row[1] for row in listed} == {"system:seed-2026-09-28"}
    assert logged == [(code, "add") for code in FIRST_TEN]


async def test_the_database_itself_keeps_one_open_request_per_company(db):
    insert = text(
        "INSERT INTO security_request (security_code, kind, status) "
        "VALUES (:code, 'initial', 'pending')"
    )
    async with db() as s, s.begin():
        await s.execute(insert, {"code": "000001"})
        await s.execute(insert, {"code": "000002"})
    with pytest.raises(IntegrityError, match="uq_security_request_open_code"):
        async with db() as s, s.begin():
            await s.execute(insert, {"code": "000001"})
    # 关掉的不占位置；关掉必须带结果
    async with db() as s, s.begin():
        await s.execute(
            text(
                "UPDATE security_request SET open=false, outcome='rejected', "
                "closed_at=now() WHERE security_code='000001'"
            )
        )
        await s.execute(insert, {"code": "000001"})
    with pytest.raises(IntegrityError, match="ck_security_request_closed_with_outcome"):
        async with db() as s, s.begin():
            await s.execute(
                text(
                    "UPDATE security_request SET open=false WHERE security_code='000002'"
                )
            )
    with pytest.raises(
        IntegrityError, match="ck_security_request_approved_has_ingestion"
    ):
        async with db() as s, s.begin():
            await s.execute(
                text(
                    "UPDATE security_request SET status='approved' "
                    "WHERE security_code='000002'"
                )
            )


# ---------------------------------------------------------------- 提交与批准
async def test_request_outside_the_watchlist_is_stored_and_nothing_is_queued(
    db, sessions
):
    view = await submit(
        sessions,
        "000001",
        ALICE,
        reason="要看净息差",
        origin_app="investment",
        origin_ref="task:42",
    )
    assert view.progress is Progress.PENDING
    assert view.request.kind is RequestKind.INITIAL
    assert await sql(db, "SELECT count(*) FROM security_ingestion") == 0
    assert await sql(db, "SELECT count(*) FROM outbox_message") == 0
    async with sessions() as session:
        stored = await service_on(session, sessions).one_of_mine(
            view.request.request_id, actor_id=ALICE
        )
    mine = stored.request.requester(ALICE)
    assert (mine.reason, mine.origin.app, mine.origin.ref) == (
        "要看净息差",
        "investment",
        "task:42",
    )


async def test_request_on_the_watchlist_is_queued_in_the_same_commit(db, sessions):
    view = await submit(sessions, "600519", ALICE)
    assert view.progress is Progress.QUEUED
    assert view.request.decided_by == SYSTEM_WATCHLIST
    assert await sql(db, "SELECT count(*) FROM security_ingestion") == 1
    assert (
        await sql(
            db,
            "SELECT aggregate_key FROM outbox_message "
            "WHERE topic='info.security.ingest.v1'",
        )
        == view.request.ingestion_id
    )


async def test_approval_and_the_batch_succeed_or_fail_together(db, sessions):
    pending = await submit(sessions, "000001", ALICE)

    class Broken(Exception):
        pass

    async def start_then_fail(active, code):
        await start_security_ingestion(active, sessions, code)
        raise Broken

    async with sessions() as session:
        service = SecurityRequestService(
            store=SqlRequestStore(session, start_ingestion=start_then_fail),
            watchlist=SqlWatchlist(session),
            clock=Clock(),
        )
        with pytest.raises(Broken):
            await service.approve(pending.request.request_id, by=OWNER)
    assert await sql(db, "SELECT count(*) FROM security_ingestion") == 0
    assert await sql(db, "SELECT count(*) FROM outbox_message") == 0
    assert await sql(db, "SELECT status FROM security_request") == "pending"

    async with sessions() as session:
        approved = await service_on(session, sessions).approve(
            pending.request.request_id, by=OWNER, add_to_watchlist=True
        )
    assert approved.progress is Progress.QUEUED
    assert await sql(db, "SELECT count(*) FROM security_ingestion") == 1
    assert await sql(db, "SELECT count(*) FROM outbox_message") == 1
    assert (
        await sql(
            db,
            "SELECT added_by FROM security_watchlist WHERE security_code='000001'",
        )
        == OWNER
    )


async def test_progress_follows_the_batch_and_the_dataset(db, sessions):
    view = await submit(sessions, "600519", ALICE)
    request_id, ingestion_id = view.request.request_id, view.request.ingestion_id

    async def seen():
        async with sessions() as session:
            return await service_on(session, sessions).one_of_mine(
                request_id, actor_id=ALICE
            )

    async def run(statement, **params):
        async with db() as s, s.begin():
            await s.execute(text(statement), params)

    await run(
        "UPDATE security_ingestion SET status='running' WHERE id=:i", i=ingestion_id
    )
    assert (await seen()).progress is Progress.COLLECTING
    await run(
        "UPDATE security_ingestion SET status='succeeded' WHERE id=:i", i=ingestion_id
    )
    assert (await seen()).progress is Progress.BUILDING
    await run(
        "INSERT INTO security_dataset (security_code, dataset_id, data_version, status,"
        " ingestion_id, bucket, object_key, sha256, size_bytes, start_date, end_date,"
        " built_at) VALUES ('600519', 'sh600519-financials', 'sh600519-financials-aaaa',"
        " 'published', :i, 'b', 'k', :sha, 1, '2016-12-31', '2025-12-31', now())",
        i=ingestion_id,
        sha="a" * 64,
    )
    assert (await seen()).progress is Progress.REGISTERING
    assert await sql(db, "SELECT open FROM security_request") is True
    await run("UPDATE security_dataset SET knowledge_registered_at=now()")
    done = await seen()
    assert done.progress is Progress.AVAILABLE
    assert (done.dataset.data_version, done.dataset.end_date) == (
        "sh600519-financials-aaaa",
        "2025-12-31",
    )
    assert await sql(db, "SELECT outcome FROM security_request") == "available"
    # 到了终点就能再申请；这次是「申请更新」
    again = await submit(sessions, "600519", BOB)
    assert again.request.request_id != request_id
    assert again.request.kind is RequestKind.REFRESH


async def test_a_refused_build_is_recorded_and_ends_the_request(db, sessions):
    view = await submit(sessions, "600519", ALICE)
    async with db() as s, s.begin():
        await s.execute(text("UPDATE security_ingestion SET status='succeeded'"))
    await record_dataset_build_refusal(
        sessions, view.request.ingestion_id, "statement_missing"
    )
    await record_dataset_build_refusal(sessions, "not-a-uuid", "x")
    await record_dataset_build_refusal(sessions, str(uuid.uuid4()), "x")
    assert (
        await sql(db, "SELECT dataset_build_error FROM security_ingestion")
        == "statement_missing"
    )
    async with sessions() as session:
        seen = await service_on(session, sessions).one_of_mine(
            view.request.request_id, actor_id=ALICE
        )
    assert seen.progress is Progress.FAILED
    assert await sql(db, "SELECT outcome FROM security_request") == "failed"


# ---------------------------------------------------------------- 几个人
async def test_joining_withdrawing_and_joining_again(db, sessions):
    first = await submit(sessions, "000001", ALICE, reason="甲")
    second = await submit(sessions, "000001", BOB, reason="乙")
    assert second.request.request_id == first.request.request_id
    assert second.request.active_requesters == 2
    async with sessions() as session:
        after = await service_on(session, sessions).withdraw(
            first.request.request_id, actor_id=ALICE
        )
    assert after.request.active_requesters == 1 and after.request.open
    back = await submit(sessions, "000001", ALICE, reason="甲又来了")
    assert back.request.active_requesters == 2
    assert back.request.requester(ALICE).reason == "甲又来了"
    assert await sql(db, "SELECT count(*) FROM security_request_requester") == 2
    async with sessions() as session:
        service = service_on(session, sessions)
        await service.withdraw(first.request.request_id, actor_id=ALICE)
    async with sessions() as session:
        closed = await service_on(session, sessions).withdraw(
            first.request.request_id, actor_id=BOB
        )
    assert closed.progress is Progress.WITHDRAWN
    assert await sql(db, "SELECT outcome FROM security_request") == "withdrawn"


async def test_two_people_submitting_at_once_end_up_in_one_request(db, sessions):
    gate = asyncio.Event()

    async def one(actor):
        async with sessions() as session:
            service = service_on(session, sessions)
            await gate.wait()
            return await service.submit("000001", actor_id=actor)

    tasks = [asyncio.create_task(one(a)) for a in (ALICE, BOB)]
    await asyncio.sleep(0)
    gate.set()
    views = await asyncio.gather(*tasks)
    assert views[0].request.request_id == views[1].request.request_id
    assert await sql(db, "SELECT count(*) FROM security_request") == 1
    assert await sql(db, "SELECT count(*) FROM security_request_requester") == 2


async def test_creating_over_an_open_request_leaves_the_transaction_usable(
    db, sessions
):
    await submit(sessions, "000001", ALICE)
    async with sessions() as session:
        store = SqlRequestStore(session, start_ingestion=None)  # type: ignore[arg-type]
        with pytest.raises(OpenRequestExists):
            await store.create(
                SecurityCode("000001"),
                kind=RequestKind.INITIAL,
                requester=NewRequester(BOB, None, None, NOW),
            )
        # 撞了唯一索引之后，这个会话还能接着读写并提交
        existing = await store.find_open(SecurityCode("000001"))
        await store.join(existing.request_id, NewRequester(BOB, "乙", None, NOW))
        await store.commit()
    assert await sql(db, "SELECT count(*) FROM security_request_requester") == 2


async def test_one_person_submitting_in_parallel_cannot_exceed_the_limit(db, sessions):
    codes = ["000001", "000002", "000004", "000006", "000007", "000008"]

    async def one(code):
        async with sessions() as session:
            try:
                return await service_on(session, sessions, max_open=3).submit(
                    code, actor_id=ALICE
                )
            except RequestError as exc:
                return exc.code

    results = await asyncio.gather(*(one(code) for code in codes))
    refused = [r for r in results if r == "too_many_open_requests"]
    assert len(refused) == 3
    assert await sql(db, "SELECT count(*) FROM security_request") == 3


# ---------------------------------------------------------------- 所有者与清单
async def test_rejection_is_stored_with_its_reason(db, sessions):
    view = await submit(sessions, "000001", ALICE)
    async with sessions() as session:
        rejected = await service_on(session, sessions).reject(
            view.request.request_id, by=OWNER, note="存储不够"
        )
    assert rejected.progress is Progress.REJECTED
    async with sessions() as session:
        service = service_on(session, sessions)
        listed = await service.listing(status=RequestStatus.REJECTED)
        pending = await service.listing(status=RequestStatus.PENDING, open_only=True)
    assert [v.request.decision_note for v in listed] == ["存储不够"]
    assert listed[0].request.decided_by == OWNER and pending == []


async def test_watchlist_changes_are_logged(db, sessions):
    async with sessions() as session:
        service = service_on(session, sessions)
        assert await service.watch("000001", by=OWNER, note="银行") is True
        assert await service.watch("000001", by=OWNER, note=None) is False
        assert await service.unwatch("600009", by=OWNER, note="不看了") is True
        assert await service.unwatch("600009", by=OWNER, note=None) is False
        assert await service.watch("600009", by=OWNER, note="又看了") is True
        codes = [e.security_code for e in await service.watchlist()]
    assert "000001" in codes and "600009" in codes and len(codes) == 11
    async with db() as s:
        trail = (
            await s.execute(
                text(
                    "SELECT action, actor, note FROM security_watchlist_log "
                    "WHERE security_code='600009' ORDER BY at, action"
                )
            )
        ).all()
    # 种子那一条的时间是迁移运行的时刻，测试里的钟是固定的，两者不比先后
    seeded = [(a, n) for a, who, n in trail if who != OWNER]
    by_owner = [(a, n) for a, who, n in trail if who == OWNER]
    assert seeded == [("add", None)]
    assert by_owner == [("remove", "不看了"), ("add", "又看了")]


async def test_unknown_and_malformed_identifiers_are_not_found(db, sessions):
    async with sessions() as session:
        service = service_on(session, sessions)
        for request_id in (str(uuid.uuid4()), "not-a-uuid", ""):
            with pytest.raises(RequestError) as error:
                await service.one_of_mine(request_id, actor_id=ALICE)
            assert error.value.code == "request_not_found"
            with pytest.raises(RequestError):
                await service.approve(request_id, by=OWNER)


async def test_the_build_task_records_a_refusal_on_the_batch(db, sessions, monkeypatch):
    """建库任务被拒绝时不重投，但要留下记录：申请的进度靠它走到终点。"""
    from types import SimpleNamespace

    from app.domain.securities.dataset import DatasetBuildError
    from app.infrastructure.messaging import delivery_handlers
    from app.infrastructure.storage import postgres

    view = await submit(sessions, "600519", ALICE)

    class Refusing:
        async def build(self, ingestion_id):
            raise DatasetBuildError("statement_missing", "利润表缺失")

    monkeypatch.setattr(
        postgres, "get_postgres", lambda: SimpleNamespace(session_factory=sessions)
    )
    monkeypatch.setattr(
        wiring, "build_security_dataset_service", lambda factory: Refusing()
    )
    async with db() as s, s.begin():
        await s.execute(text("UPDATE security_ingestion SET status='succeeded'"))
    async with sessions() as session:
        await delivery_handlers.build_security_dataset(
            session, {"ingestion_id": view.request.ingestion_id}
        )
    assert (
        await sql(db, "SELECT dataset_build_error FROM security_ingestion")
        == "statement_missing"
    )
    assert await sql(db, "SELECT count(*) FROM security_dataset") == 0
    async with sessions() as session:
        seen = await service_on(session, sessions).one_of_mine(
            view.request.request_id, actor_id=ALICE
        )
    assert seen.progress is Progress.FAILED
