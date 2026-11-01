"""迁移回退时先收拾这一版才有的活，不留旧代码读不了的行：0021 回退收拾巨潮互动易渠道上的
（独立审第 16 条），0022 回退收拾「看新的」「搜」与给网址的（二审第 14 条）。真 PostgreSQL、
一次性 schema。"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from test_drop_security_line_db import ROOT, _all, _run

TARGET = ROOT / "alembic/versions/20261030_0021_supply_schedule.py"
UP_TO = [
    p
    for p in sorted((ROOT / "alembic/versions").glob("20*.py"))
    if p.name <= TARGET.name
]


def _upto(target):
    return [
        p
        for p in sorted((ROOT / "alembic/versions").glob("20*.py"))
        if p.name <= target.name
    ]


async def _schema(paths):
    import os

    url = os.environ.get("DELIVERY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set DELIVERY_TEST_DATABASE_URL to a disposable *_tests database")
    parsed = make_url(url)
    assert parsed.database and parsed.database.endswith("_tests")
    schema = "downgrade_test_" + uuid.uuid4().hex
    engine = create_async_engine(
        parsed.set(drivername="postgresql+asyncpg"),
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    async with engine.begin() as c:
        await c.execute(text(f'CREATE SCHEMA "{schema}"'))
        await c.execute(
            text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public')
        )
        await c.run_sync(_run, paths)
    return engine, schema


WEB = ROOT / "alembic/versions/20261030_0022_supply_web_subscriptions.py"


@pytest_asyncio.fixture
async def at_0022():
    engine, schema = await _schema(_upto(WEB))
    try:
        yield engine
    finally:
        async with engine.begin() as c:
            await c.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


async def test_0022_downgrade_settles_url_watch_and_search_work(at_0022):
    engine = at_0022
    demand, sub_demand, item, sub_item, url_job, watch, sub_job, key_job = (
        uuid.uuid4() for _ in range(8)
    )
    async with engine.begin() as c:
        await c.execute(
            text(
                """INSERT INTO supply_demand (id, owner_subject, proposer, chain, app,
                confirmation_id, idempotency_key, digest, created_at)
                VALUES (:d, 'alice', 'app', '[]', 'knowbench', 'conf', 'idem-0001',
                'x', now()),
                (:sd, 'alice', 'subscription', '[]', 'knowfeed', 'subscription:s1@1',
                'idem-sub-0001', 'y', now())"""
            ),
            {"d": demand, "sd": sub_demand},
        )
        for job_id, kind, payload in (
            (url_job, "fetch", '{"url": "https://www.stats.gov.cn/a"}'),
            (watch, "watch", '{"target": "t", "owner": "alice"}'),
            (
                sub_job,
                "fetch",
                '{"entry": {"channel_id": "cninfo"}, "key": {"subject": "000858.SZ"}}',
            ),
            (key_job, "fetch", '{"key": {"subject": "000858.SZ"}}'),
        ):
            await c.execute(
                text(
                    """INSERT INTO supply_job (id, kind, channel_id, dedup_key, payload,
                    status, tier, attempts, created_at)
                    VALUES (:j, :k, 'cninfo', :d, CAST(:p AS jsonb), 'queued', 1, 0, now())"""
                ),
                {"j": job_id, "k": kind, "d": f"x:{job_id}", "p": payload},
            )
        await c.execute(
            text(
                """INSERT INTO supply_demand_item (id, demand_id, key, key_text,
                readings, confirmation_item, status, channel_id, job_id, receipt,
                url, created_at, updated_at)
                VALUES (:i, :d, '{}', 'k', '["original"]', 'c1', 'accepted',
                'site-x', NULL, '{}', 'https://www.stats.gov.cn/a', now(), now()),
                (:si, :sd, '{"subject": "000858.SZ", "doc_type": "annual_report"}',
                '000858.SZ|annual_report|None|None', '["original"]', 's1', 'fetching',
                'cninfo', :sj, '{}', NULL, now(), now())"""
            ),
            {"i": item, "d": demand, "si": sub_item, "sd": sub_demand, "sj": sub_job},
        )
    async with engine.begin() as c:
        await c.run_sync(_run, [WEB], "downgrade")
    jobs = dict(await _all(engine, "SELECT id, status FROM supply_job"))
    # 精确描述的作业旧代码读得了，照旧排着；订阅带 entry 的作业与需求项一并收口（三审第 17 条）
    assert jobs == {
        url_job: "failed",
        watch: "failed",
        sub_job: "failed",
        key_job: "queued",
    }
    assert await _all(
        engine,
        "SELECT status, reason FROM supply_demand_item ORDER BY confirmation_item",
    ) == [
        ("unsatisfiable", "feature_removed"),
        ("unsatisfiable", "feature_removed"),
    ]
    [(restored_key,)] = await _all(
        engine, "SELECT key FROM supply_demand_item WHERE id = :i", i=item
    )
    assert restored_key["doc_type"] == "webpage" and restored_key["subject"]


@pytest_asyncio.fixture
async def at_0021():
    import os

    url = os.environ.get("DELIVERY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set DELIVERY_TEST_DATABASE_URL to a disposable *_tests database")
    parsed = make_url(url)
    assert parsed.database and parsed.database.endswith("_tests")
    schema = "schedule_downgrade_test_" + uuid.uuid4().hex
    engine = create_async_engine(
        parsed.set(drivername="postgresql+asyncpg"),
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    try:
        async with engine.begin() as c:
            await c.execute(text(f'CREATE SCHEMA "{schema}"'))
            await c.execute(
                text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public')
            )
            await c.run_sync(_run, UP_TO)
        yield engine
    finally:
        async with engine.begin() as c:
            await c.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


async def test_downgrade_settles_work_on_the_removed_channel(at_0021):
    engine = at_0021
    demand, item, job, done = (uuid.uuid4() for _ in range(4))
    async with engine.begin() as c:
        await c.execute(
            text(
                """INSERT INTO supply_demand (id, owner_subject, proposer, chain, app,
                confirmation_id, idempotency_key, digest, created_at)
                VALUES (:d, 'alice', 'app', '[]', 'knowbench', 'conf', 'idem-0001',
                'x', now())"""
            ),
            {"d": demand},
        )
        for job_id, status in ((job, "queued"), (done, "done")):
            await c.execute(
                text(
                    """INSERT INTO supply_job (id, kind, channel_id, dedup_key, payload,
                    status, tier, attempts, created_at)
                    VALUES (:j, 'fetch', 'cninfo-irm', :k, '{}', :s, 1, 0, now())"""
                ),
                {"j": job_id, "k": f"fetch:{job_id}", "s": status},
            )
        await c.execute(
            text(
                """INSERT INTO supply_demand_item (id, demand_id, key, key_text,
                readings, confirmation_item, status, channel_id, job_id, receipt,
                created_at, updated_at)
                VALUES (:i, :d, '{}', 'k', '["original"]', 'c1', 'fetching',
                'cninfo-irm', :j, '{}', now(), now())"""
            ),
            {"i": item, "d": demand, "j": job},
        )
    async with engine.begin() as c:
        await c.run_sync(_run, [TARGET], "downgrade")
    jobs = dict(await _all(engine, "SELECT id, status FROM supply_job"))
    assert jobs == {job: "failed", done: "done"}
    assert await _all(engine, "SELECT status, reason FROM supply_demand_item") == [
        ("unsatisfiable", "channel_disabled")
    ]
    assert (
        await _all(engine, "SELECT id FROM supply_channel WHERE id = 'cninfo-irm'")
        == []
    )
