"""迁移 20261028_0019（SDD 0045 第六节第 2 条，decisions ⑰）：证券专线的七张表删掉，
可靠投递里三类旧任务的记录删掉；供给的表、别的任务、通用采集的表都留着。downgrade 建回的
空表与 0018 时一模一样。真 PostgreSQL、一次性 schema：先迁到 0018 造数，再单跑这一个迁移
的 upgrade 与 downgrade。"""

from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "alembic/versions/20261028_0019_drop_security_line.py"
BEFORE = [
    p
    for p in sorted((ROOT / "alembic/versions").glob("20*.py"))
    if p.name < TARGET.name
]
TABLES = (
    "security_ingestion",
    "security_ingestion_item",
    "security_dataset",
    "security_request",
    "security_request_requester",
    "security_watchlist",
    "security_watchlist_log",
)


def _run(connection, paths, action: str = "upgrade") -> None:
    with Operations.context(MigrationContext.configure(connection)):
        for path in paths:
            spec = importlib.util.spec_from_file_location(path.stem, path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            getattr(module, action)()


@pytest_asyncio.fixture
async def at_0018():
    url = os.environ.get("DELIVERY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set DELIVERY_TEST_DATABASE_URL to a disposable *_tests database")
    parsed = make_url(url)
    assert parsed.database and parsed.database.endswith("_tests")
    schema = "drop_security_test_" + uuid.uuid4().hex
    engine = create_async_engine(
        parsed.set(drivername="postgresql+asyncpg"),
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )

    async def migrate(action: str) -> None:
        async with engine.begin() as c:
            await c.run_sync(_run, [TARGET], action)

    try:
        async with engine.begin() as c:
            await c.execute(text(f'CREATE SCHEMA "{schema}"'))
            await c.execute(
                text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public')
            )
            await c.run_sync(_run, BEFORE)
        yield engine, migrate
    finally:
        async with engine.begin() as c:
            await c.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


async def _all(engine, sql: str, **params) -> list[tuple]:
    async with engine.begin() as c:
        return [tuple(r) for r in (await c.execute(text(sql), params)).all()]


async def _shape(engine) -> dict[str, list[tuple]]:
    """证券专线七张表的列、约束、索引，用来比 downgrade 建回的与 0018 时的。"""
    names = {"names": list(TABLES)}
    return {
        "columns": await _all(
            engine,
            "SELECT table_name, column_name, data_type, character_maximum_length,"
            " is_nullable, column_default FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = ANY(:names)"
            " ORDER BY table_name, column_name",
            **names,
        ),
        "constraints": await _all(
            engine,
            "SELECT t.relname, c.conname, pg_get_constraintdef(c.oid)"
            " FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid"
            " WHERE t.relnamespace = current_schema()::regnamespace"
            " AND t.relname = ANY(:names) ORDER BY t.relname, c.conname",
            **names,
        ),
        "indexes": await _all(
            engine,
            "SELECT tablename, indexname, indexdef FROM pg_indexes"
            " WHERE schemaname = current_schema() AND tablename = ANY(:names)"
            " ORDER BY tablename, indexname",
            **names,
        ),
    }


async def _seed(engine) -> None:
    """0018 时的样子：关注清单是 0013 种的十家；造一个批准了的申请、它的批次与数据集；
    可靠投递里三类旧任务各一条，外加一条供给作业与一条证券名录。"""
    async with engine.begin() as c:
        ingestion = (
            await c.execute(
                text(
                    "INSERT INTO security_ingestion (security_code, market, status,"
                    " requested_at, dataset_build_error) VALUES ('600009', 'SH',"
                    " 'succeeded', now(), 'statement_missing') RETURNING id"
                )
            )
        ).scalar_one()
        await c.execute(
            text(
                "INSERT INTO security_dataset (security_code, dataset_id, data_version,"
                " status, ingestion_id, bucket, object_key, sha256, size_bytes,"
                " start_date, end_date, built_at) VALUES ('600009', 'sh600009',"
                " 'v1', 'published', :i, 'b', 'k', :sha, 1, '2016-12-31',"
                " '2025-12-31', now())"
            ),
            {"i": ingestion, "sha": "a" * 64},
        )
        request = (
            await c.execute(
                text(
                    "INSERT INTO security_request (security_code, kind, status,"
                    " ingestion_id) VALUES ('600009', 'initial', 'approved', :i)"
                    " RETURNING id"
                ),
                {"i": ingestion},
            )
        ).scalar_one()
        await c.execute(
            text(
                "INSERT INTO security_request_requester (request_id, actor_id,"
                " requested_at) VALUES (:r, :a, now())"
            ),
            {"r": request, "a": uuid.uuid4()},
        )
        for topic in (
            "knowfeed.security.ingest.v1",
            "knowfeed.security.dataset.build.v1",
            "knowfeed.security.dataset.register.v1",
            "knowfeed.supply.job.v1",
        ):
            await c.execute(
                text(
                    "INSERT INTO outbox_message (id, topic, aggregate_key,"
                    " deduplication_key, payload) VALUES (:id, :t, 'k', :t, '{}')"
                ),
                {"id": uuid.uuid4(), "t": topic},
            )
        await c.execute(
            text(
                "INSERT INTO supply_security (subject, name, refreshed_at)"
                " VALUES ('600009', '上海机场', now())"
            )
        )


async def test_the_security_line_goes_and_supply_stays(at_0018) -> None:
    engine, migrate = at_0018
    before = await _shape(engine)
    assert {r[0] for r in before["columns"]} == set(TABLES)
    await _seed(engine)
    assert await _all(engine, "SELECT count(*) FROM security_watchlist") == [(10,)]
    channels = await _all(engine, "SELECT id FROM supply_channel ORDER BY id")
    assert channels

    await migrate("upgrade")
    for table in TABLES:
        assert await _all(engine, "SELECT to_regclass(:t)", t=table) == [(None,)]
    # 旧任务的记录删了；供给作业那条留着
    assert await _all(engine, "SELECT topic FROM outbox_message") == [
        ("knowfeed.supply.job.v1",)
    ]
    # 供给的表与数据、通用采集的表都在
    assert await _all(engine, "SELECT id FROM supply_channel ORDER BY id") == channels
    assert await _all(engine, "SELECT subject FROM supply_security") == [("600009",)]
    for table in ("crawl_job", "raw_artifact"):
        assert await _all(engine, "SELECT to_regclass(:t) IS NOT NULL", t=table) == [
            (True,)
        ]

    # 退回：建回的空表与 0018 时一模一样，删掉的行（含关注清单）不恢复
    await migrate("downgrade")
    assert await _shape(engine) == before
    for table in TABLES:
        assert await _all(engine, f"SELECT count(*) FROM {table}") == [(0,)]


def test_no_model_maps_a_security_line_table() -> None:
    from app.infrastructure.models import Base

    assert not [t for t in Base.metadata.tables if t.startswith("security_")]
