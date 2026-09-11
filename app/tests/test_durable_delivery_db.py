"""Run real migrations and fault scenarios in a disposable PostgreSQL schema."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.services.durable_tasks import DurableTasks, enqueue_task
from app.infrastructure.messaging.durable_delivery import DeliveryLeaseLost
from app.tasks.durable_delivery import pump

ROOT = Path(__file__).resolve().parents[1]
TOPIC = "test.counter.v1"


def migrate(connection):
    modules = []
    with Operations.context(MigrationContext.configure(connection)):
        for path in sorted((ROOT / "alembic/versions").glob("20*.py")):
            spec = importlib.util.spec_from_file_location(path.stem, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.upgrade()
            modules.append(module)
    return modules


@pytest_asyncio.fixture
async def db():
    url = os.environ.get("DELIVERY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set DELIVERY_TEST_DATABASE_URL to a disposable *_tests database")
    parsed = make_url(url)
    assert parsed.database and parsed.database.endswith("_tests")
    schema = "delivery_test_" + uuid.uuid4().hex
    engine = create_async_engine(
        parsed.set(drivername="postgresql+asyncpg"),
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    try:
        async with engine.begin() as c:
            await c.execute(text(f'CREATE SCHEMA "{schema}"'))
            # Match database provisioning: older domain migrations use uuid-ossp.
            await c.execute(
                text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public')
            )
            await c.run_sync(migrate)
            await c.execute(
                text(
                    "CREATE TABLE delivery_test_counter "
                    "(id int PRIMARY KEY, value int NOT NULL)"
                )
            )
            await c.execute(text("INSERT INTO delivery_test_counter VALUES (1,0)"))
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        async with engine.begin() as c:
            await c.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


async def sql(db, query, **params):
    async with db() as s, s.begin():
        result = await s.execute(text(query), params)
        return result.scalar_one_or_none() if result.returns_rows else None


async def request(db, *, key="one", payload=None):
    async with db() as s, s.begin():
        return await enqueue_task(
            s,
            topic=TOPIC,
            key="counter:1",
            payload=payload or {},
            deduplication_key=key,
        )


async def increment(s, payload):
    await s.execute(text("UPDATE delivery_test_counter SET value=value+1 WHERE id=1"))


def runtime(db, **kwargs):
    return DurableTasks(db, handlers={TOPIC: increment}, **kwargs)


async def age_published(db):
    await sql(
        db,
        "UPDATE outbox_message SET published_at=clock_timestamp()-interval '2 hours'",
    )


async def due(db):
    await sql(
        db,
        "UPDATE outbox_message SET available_at=clock_timestamp()-interval '2 hours'",
    )


async def test_business_and_intent_rollback_together(db):
    with pytest.raises(RuntimeError):
        async with db() as s, s.begin():
            await increment(s, {})
            await enqueue_task(
                s, topic=TOPIC, key="counter:1", payload={}, deduplication_key="one"
            )
            raise RuntimeError("crash before commit")
    assert await sql(db, "SELECT value FROM delivery_test_counter") == 0
    assert await sql(db, "SELECT count(*) FROM outbox_message") == 0


async def test_intent_idempotency_rejects_changed_payload(db):
    first = await request(db)
    assert await request(db) == first
    with pytest.raises(ValueError, match="different intent"):
        await request(db, payload={"changed": True})


async def test_duplicate_delivery_commits_once(db):
    message_id = await request(db)
    delivery = runtime(db)
    assert sorted(
        await asyncio.gather(delivery.consume(message_id), delivery.consume(message_id))
    ) == [False, True]
    assert await delivery.consume(message_id) is False
    assert await sql(db, "SELECT value FROM delivery_test_counter") == 1
    assert await sql(db, "SELECT count(*) FROM inbox_message") == 1


async def test_handler_failure_rolls_back_business_and_inbox(db):
    async def fail(s, payload):
        await increment(s, payload)
        raise RuntimeError("before acknowledgement")

    message_id = await request(db)
    with pytest.raises(RuntimeError):
        await DurableTasks(db, handlers={TOPIC: fail}).consume(message_id)
    assert await sql(db, "SELECT value FROM delivery_test_counter") == 0
    assert await sql(db, "SELECT count(*) FROM inbox_message") == 0
    assert await runtime(db).consume(message_id)


async def test_lost_broker_message_dead_letter_replay_and_ack(db):
    message_id = await request(db)
    delivery = runtime(db, max_attempts=2)

    async def lost(message):
        pass

    assert await pump(delivery, lost) == 1
    await age_published(db)
    assert await delivery.reconcile() == 1
    assert await pump(delivery, lost) == 1
    await age_published(db)
    assert await delivery.reconcile() == 0
    assert await delivery.claim_delivery() is None
    assert (
        await sql(
            db, "SELECT count(*) FROM outbox_dead_letter WHERE replayed_at IS NULL"
        )
        == 1
    )
    await delivery.replay(message_id)
    assert await delivery.consume(message_id)
    assert await pump(delivery, lost) == 1
    await age_published(db)
    assert await delivery.reconcile() == 0
    assert await delivery.consume(message_id) is False


async def test_broker_failure_is_bounded_and_error_is_sanitized(db):
    await request(db)

    async def fail(message):
        raise ConnectionError("amqp://secret-password")

    delivery = runtime(db, max_attempts=1)
    await pump(delivery, fail)
    assert await delivery.claim_delivery() is None
    assert (
        await sql(db, "SELECT error_code FROM outbox_dead_letter") == "ConnectionError"
    )


async def test_expired_delivery_owner_cannot_publish_or_release_new_owner(db):
    await request(db)
    delivery = runtime(db)
    old = await delivery.claim_delivery()
    await sql(
        db,
        "UPDATE outbox_message "
        "SET lease_expires_at=clock_timestamp()-interval '1 second'",
    )
    new = await delivery.claim_delivery()
    assert old["lease_owner"] != new["lease_owner"]
    with pytest.raises(DeliveryLeaseLost):
        await delivery.finish_delivery(old)
    await delivery.finish_delivery(new)


async def test_expired_execution_cannot_commit_intermediate_progress(db):
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def stale(s, payload):
        entered.set()
        await proceed.wait()
        await increment(s, payload)
        await s.commit()

    message_id = await request(db)
    delivery = DurableTasks(db, handlers={TOPIC: stale})
    worker = asyncio.create_task(delivery.consume(message_id))
    await entered.wait()
    await sql(
        db,
        "UPDATE outbox_execution SET expires_at=clock_timestamp()-interval '1 second'",
    )
    new = await runtime(db).claim_execution(message_id)
    assert new
    proceed.set()
    with pytest.raises(DeliveryLeaseLost):
        await worker
    assert await sql(db, "SELECT value FROM delivery_test_counter") == 0
    assert await sql(db, "SELECT epoch FROM outbox_execution") == new[0].epoch
    await runtime(db).release(new[0])
    assert await runtime(db).consume(message_id)


async def test_heartbeat_protects_long_execution_and_reconcile(db):
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def slow(s, payload):
        entered.set()
        await proceed.wait()
        await increment(s, payload)

    message_id = await request(db)
    delivery = DurableTasks(db, handlers={TOPIC: slow}, lease_seconds=1)
    message = await delivery.claim_delivery()
    await delivery.finish_delivery(message)
    worker = asyncio.create_task(delivery.consume(message_id))
    await entered.wait()
    await asyncio.sleep(1.2)
    await age_published(db)
    assert await delivery.reconcile() == 0
    assert await delivery.claim_execution(message_id) is None
    proceed.set()
    assert await worker


async def test_ack_and_effect_rollback_when_final_commit_fails(db):
    async def failing_commit(s, payload):
        await increment(s, payload)

        def fail(_):
            raise RuntimeError("database commit failed")

        event.listen(s.sync_session, "before_commit", fail, once=True)

    message_id = await request(db)
    with pytest.raises(RuntimeError):
        await DurableTasks(db, handlers={TOPIC: failing_commit}).consume(message_id)
    assert await sql(db, "SELECT count(*) FROM inbox_message") == 0
    assert await sql(db, "SELECT value FROM delivery_test_counter") == 0


async def test_unknown_topic_is_never_dispatched(db):
    await request(db)
    delivery = DurableTasks(db, handlers={})
    assert await delivery.claim_delivery() is None
    assert await delivery.reconcile() == 0


async def test_migration_downgrade_upgrade_preserves_outbox(db):
    message_id = await request(db)
    async with db() as s, s.begin():

        def roundtrip(connection):
            from app.infrastructure.messaging.delivery_schema import downgrade, upgrade

            with Operations.context(MigrationContext.configure(connection)):
                downgrade()
                upgrade()

        connection = await s.connection()
        await connection.run_sync(roundtrip)
    assert await sql(db, "SELECT id FROM outbox_message") == message_id
    assert await runtime(db).consume(message_id)


async def test_repeated_publisher_crashes_are_bounded(db):
    await request(db)
    delivery = runtime(db, max_attempts=1)
    assert await delivery.claim_delivery()
    await sql(db, "UPDATE outbox_message SET lease_expires_at='2000-01-01'")
    await delivery.reconcile()
    assert await delivery.claim_delivery() is None
    assert (
        await sql(db, "SELECT error_code FROM outbox_dead_letter")
        == "delivery_attempts_exhausted"
    )
