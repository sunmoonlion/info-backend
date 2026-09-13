"""Real isolated migrations and HTTP readiness; no business database access."""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from alembic.config import Config
from alembic.runtime.environment import EnvironmentContext
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bootstrap import api
from app.infrastructure.storage import schema_readiness as readiness

ROOT = Path(__file__).resolve().parents[1]
READY_PATHS = ("/health/ready", "/ready", "/api/health")


def migrate(connection, target="head", *, downgrade=False):
    config = Config()
    config.set_main_option("script_location", str(ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)

    def migrations(revision, context):
        # The same migration traversal used by Alembic command.upgrade/downgrade.
        if downgrade:
            return scripts._downgrade_revs(target, revision)
        return scripts._upgrade_revs(target, revision)

    with EnvironmentContext(config, scripts, fn=migrations) as environment:
        environment.configure(connection=connection)
        with environment.begin_transaction():
            environment.run_migrations()


@pytest.fixture(autouse=True)
def clear_revision_cache():
    readiness.expected_schema_revision.cache_clear()
    yield
    readiness.expected_schema_revision.cache_clear()


@pytest_asyncio.fixture
async def schema_db():
    url = os.environ.get("DELIVERY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set DELIVERY_TEST_DATABASE_URL to a disposable *_tests database")
    parsed = make_url(url)
    assert parsed.database and parsed.database.endswith("_tests")
    schema = "readiness_test_" + uuid.uuid4().hex
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
            await c.run_sync(migrate)
        yield SimpleNamespace(
            sessions=async_sessionmaker(engine, expire_on_commit=False),
            engine=engine,
            schema=schema,
        )
    finally:
        async with engine.begin() as c:
            await c.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


@pytest_asyncio.fixture
async def client(schema_db, monkeypatch):
    monkeypatch.setattr(
        api, "get_postgres", lambda: SimpleNamespace(session_factory=schema_db.sessions)
    )
    monkeypatch.setattr(
        api,
        "get_redis",
        lambda: SimpleNamespace(
            client=SimpleNamespace(ping=AsyncMock(return_value=True))
        ),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api.create_app()),
        base_url="http://testserver",
    ) as connection:
        yield connection


async def execute(schema_db, statement, **params):
    async with schema_db.sessions() as s, s.begin():
        await s.execute(text(statement), params)


async def assert_status(client, status):
    for path in READY_PATHS:
        response = await client.get(path)
        assert response.status_code == status
        assert response.json() == {"status": "ready" if status == 200 else "not_ready"}
        assert response.headers["x-content-type-options"] == "nosniff"
    for path in ("/health/live", "/health"):
        response = await client.get(path)
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


@pytest.mark.parametrize(
    "fault", ["healthy", "empty", "old", "ahead", "multi", "missing"]
)
async def test_revision_http_aliases_fail_closed(schema_db, client, fault):
    if fault == "missing":
        await execute(schema_db, "DROP TABLE alembic_version")
    elif fault == "empty":
        await execute(schema_db, "DELETE FROM alembic_version")
    elif fault in {"old", "ahead"}:
        await execute(schema_db, "UPDATE alembic_version SET version_num=:v", v=fault)
    elif fault == "multi":
        await execute(schema_db, "INSERT INTO alembic_version VALUES ('unexpected')")
    await assert_status(client, 200 if fault == "healthy" else 503)


async def test_probe_does_not_write_and_does_not_cache_database_readiness(
    schema_db, client
):
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(schema_db.engine.sync_engine, "before_cursor_execute", record)
    try:
        await assert_status(client, 200)
    finally:
        event.remove(schema_db.engine.sync_engine, "before_cursor_execute", record)
    assert len(statements) == len(READY_PATHS)
    assert all(
        s == "SELECT version_num FROM alembic_version LIMIT 2" for s in statements
    )
    await execute(schema_db, "UPDATE alembic_version SET version_num='ahead'")
    await assert_status(client, 503)
    await execute(
        schema_db,
        "UPDATE alembic_version SET version_num=:v",
        v=readiness.expected_schema_revision(),
    )
    await assert_status(client, 200)


async def test_real_migration_downgrade_and_forward(schema_db, client):
    await assert_status(client, 200)
    scripts = ScriptDirectory(str(ROOT / "alembic"))
    previous = scripts.get_revision("head").down_revision
    assert isinstance(previous, str)
    async with schema_db.engine.begin() as c:
        await c.run_sync(
            lambda connection: migrate(connection, previous, downgrade=True)
        )
    await assert_status(client, 503)
    async with schema_db.engine.begin() as c:
        await c.run_sync(migrate)
    await assert_status(client, 200)


async def test_api_principal_requires_only_version_select(
    schema_db, client, monkeypatch
):
    role = "readiness_role_" + uuid.uuid4().hex
    await execute(schema_db, f'CREATE ROLE "{role}" NOLOGIN')
    try:
        await execute(
            schema_db, f'GRANT USAGE ON SCHEMA "{schema_db.schema}" TO "{role}"'
        )

        @asynccontextmanager
        async def role_session():
            async with schema_db.sessions() as s:
                await s.execute(text(f'SET LOCAL ROLE "{role}"'))
                yield s

        monkeypatch.setattr(
            api, "get_postgres", lambda: SimpleNamespace(session_factory=role_session)
        )
        await assert_status(client, 503)
        await execute(schema_db, f'GRANT SELECT ON alembic_version TO "{role}"')
        await assert_status(client, 200)
        await execute(schema_db, f'REVOKE SELECT ON alembic_version FROM "{role}"')
        await assert_status(client, 503)
    finally:
        await execute(schema_db, f'DROP OWNED BY "{role}"')
        await execute(schema_db, f'DROP ROLE "{role}"')


@pytest.mark.parametrize("fault", ["exception", "negative_ping"])
async def test_redis_failure_is_private_and_recovers(client, monkeypatch, fault):
    ping = AsyncMock(return_value=False)
    if fault == "exception":
        ping.side_effect = RuntimeError("secret://sensitive-host/password")
    monkeypatch.setattr(
        api, "get_redis", lambda: SimpleNamespace(client=SimpleNamespace(ping=ping))
    )
    await assert_status(client, 503)
    ping.side_effect = None
    ping.return_value = True
    await assert_status(client, 200)


async def test_connection_error_and_broken_packaging_are_private(
    client, monkeypatch, tmp_path
):
    original = api.get_postgres

    def unavailable():
        raise ConnectionError("postgresql://secret:password@private-host/database")

    monkeypatch.setattr(
        api, "get_postgres", lambda: SimpleNamespace(session_factory=unavailable)
    )
    await assert_status(client, 503)
    monkeypatch.setattr(api, "get_postgres", original)
    with monkeypatch.context() as patch:
        patch.setattr(readiness, "_MIGRATIONS_PATH", tmp_path / "missing")
        await assert_status(client, 503)
    await assert_status(client, 200)


async def test_slow_redis_is_cancelled_by_probe_budget(client, monkeypatch):
    cancelled = asyncio.Event()

    async def ping():
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr(api, "READINESS_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(
        api, "get_redis", lambda: SimpleNamespace(client=SimpleNamespace(ping=ping))
    )
    async with asyncio.timeout(1):
        await assert_status(client, 503)
    assert cancelled.is_set()


@pytest.mark.parametrize("recovery_delay", [0.0, 0.1])
async def test_database_lock_timeout_releases_connection_and_recovers(
    schema_db, client, monkeypatch, recovery_delay
):
    with monkeypatch.context() as fault:
        fault.setattr(api, "READINESS_TIMEOUT_SECONDS", 0.05)
        async with schema_db.engine.begin() as blocker:
            await blocker.execute(
                text("LOCK TABLE alembic_version IN ACCESS EXCLUSIVE MODE")
            )
            async with asyncio.timeout(2):
                await assert_status(client, 503)
    assert api.READINESS_TIMEOUT_SECONDS == readiness.READINESS_TIMEOUT_SECONDS
    if recovery_delay:
        original_ping = api.get_redis().client.ping

        async def healthy_but_slow_ping():
            # Controlled healthy latency: above the 50 ms injected fault budget,
            # well below the real 2 seconds. Never a wait for a race to turn green.
            await asyncio.sleep(recovery_delay)
            return await original_ping()

        monkeypatch.setattr(
            api,
            "get_redis",
            lambda: SimpleNamespace(
                client=SimpleNamespace(ping=healthy_but_slow_ping)
            ),
        )
    await assert_status(client, 200)


async def test_caller_cancellation_is_not_turned_into_ready_or_503(client, monkeypatch):
    entered = asyncio.Event()

    async def ping():
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(
        api, "get_redis", lambda: SimpleNamespace(client=SimpleNamespace(ping=ping))
    )
    task = asyncio.create_task(client.get("/ready"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("packaging", ["missing", "empty", "multiple_heads", "valid"])
def test_packaged_migration_metadata_without_running_upgrade(
    tmp_path, monkeypatch, packaging
):
    root = tmp_path / "alembic"
    monkeypatch.setattr(readiness, "_MIGRATIONS_PATH", root)
    if packaging != "missing":
        versions = root / "versions"
        versions.mkdir(parents=True)
        (root / "env.py").write_text("raise AssertionError('must never run env.py')\n")
        if packaging != "empty":
            (versions / "base.py").write_text(
                "revision = 'base'\ndown_revision = None\n"
                "def upgrade(): raise AssertionError('must never run migration')\n"
            )
        if packaging == "multiple_heads":
            for name in ("left", "right"):
                (versions / f"{name}.py").write_text(
                    f"revision = '{name}'\ndown_revision = 'base'\n"
                )
    if packaging == "valid":
        assert readiness.expected_schema_revision() == "base"
    else:
        with pytest.raises((CommandError, readiness.SchemaNotReady)):
            readiness.expected_schema_revision()
