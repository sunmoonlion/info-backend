"""Deterministic wall-clock regression; only this test engine's SQL is shifted."""

from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import event
from test_durable_delivery_db import TOPIC, request, runtime, sql
from test_durable_delivery_db import db as db

from app.application.services.durable_tasks import (
    DurableTasks,
    assert_execution_current,
    enqueue_task,
)
from app.infrastructure.messaging.delivery_observation import collect_delivery_snapshot
from app.infrastructure.messaging.durable_delivery import DeliveryLeaseLost


@contextmanager
def shifted_database_clock(db, seconds=-3600):
    """Do not change the host, PostgreSQL functions, schemas or other engines."""
    assert isinstance(seconds, int)
    engine = db.kw["bind"].sync_engine
    hits = []

    def shift(conn, cursor, statement, parameters, context, executemany):
        hits.append(True)
        return statement.replace(
            "clock_timestamp()",
            f"(pg_catalog.clock_timestamp() + interval '{seconds} seconds')",
        ), parameters

    event.listen(engine, "before_cursor_execute", shift, retval=True)
    try:
        yield
        assert hits, "real SQL must execute in the fault scope"
    finally:
        event.remove(engine, "before_cursor_execute", shift)


def shift_first_release(db, monkeypatch):
    """Persist the first release at a fast clock, then restore normal reads.

    The original end-to-end scenarios keep all assertions and timings unchanged.
    With explicit released state, no clock read is needed by release at all.
    """
    original = DurableTasks.release
    calls = []

    async def release(self, lease):
        calls.append(lease)
        if len(calls) == 1:
            with shifted_database_clock(db, seconds=3600):
                await original(self, lease)
        else:
            await original(self, lease)

    monkeypatch.setattr(DurableTasks, "release", release)
    return calls


@pytest.mark.parametrize("operation", ["claim", "renew", "guard"])
async def test_released_execution_stays_released_after_clock_regression(db, operation):
    identifier = await request(db)
    delivery = runtime(db)
    lease, _ = await delivery.claim_execution(identifier)
    await delivery.release(lease)
    with shifted_database_clock(db):
        if operation == "claim":
            replacement = await delivery.claim_execution(identifier)
            assert replacement is not None
            assert replacement[0].epoch == lease.epoch + 1
            # Late cleanup of the previous owner cannot revoke its successor.
            await delivery.release(lease)
            await delivery.renew(replacement[0])
        elif operation == "renew":
            with pytest.raises(DeliveryLeaseLost):
                await delivery.renew(lease)
        else:
            async with db() as session:
                session.info["delivery_lease"] = lease
                with pytest.raises(DeliveryLeaseLost):
                    await assert_execution_current(session)


async def test_unscheduled_enqueue_is_still_publishable_after_clock_regression(db):
    identifier = await request(db)
    delivery = runtime(db)
    with shifted_database_clock(db):
        snapshot = await collect_delivery_snapshot(db, {"tasks": delivery})
        row = snapshot["topics"][0]
        assert row["scheduled_messages"] == 0
        assert row["publishable_messages"] == 1
        assert row["oldest_due_incomplete_age_seconds"] == 0
        message = await delivery.claim_delivery()
        assert message is not None and message["id"] == identifier


@pytest.mark.parametrize("scheduled", [False, True])
async def test_replay_resets_backoff_but_preserves_immutable_schedule(db, scheduled):
    now = await sql(db, "SELECT clock_timestamp()")
    async with db() as session, session.begin():
        identifier = await enqueue_task(
            session,
            topic=TOPIC,
            key="clock",
            payload={},
            deduplication_key="clock",
            not_before=now + timedelta(hours=2) if scheduled else None,
        )
    delivery = runtime(db)
    async with db() as session, session.begin():
        await delivery._dead_letter(session, identifier, "test_fault")
    await delivery.replay(identifier)
    with shifted_database_clock(db):
        row = (await collect_delivery_snapshot(db, {"tasks": delivery}))["topics"][0]
        assert row["dead_letter_messages"] == 0
        assert row["scheduled_messages"] == int(scheduled)
        assert row["publishable_messages"] == int(not scheduled)
        message = await delivery.claim_delivery()
        if scheduled:
            assert message is None
            assert await delivery.consume(identifier) is False
        else:
            assert message is not None and message["id"] == identifier


async def test_reconcile_resets_retry_eligibility_without_wall_clock_gate(db):
    identifier = await request(db)
    delivery = runtime(db)
    message = await delivery.claim_delivery()
    await delivery.finish_delivery(message)
    await sql(
        db,
        "UPDATE outbox_message SET published_at=clock_timestamp()-interval '2 hours'",
    )
    assert await delivery.reconcile() == 1
    with shifted_database_clock(db):
        message = await delivery.claim_delivery()
        assert message is not None and message["id"] == identifier


async def test_real_retry_backoff_is_not_bypassed(db):
    await request(db)
    delivery = runtime(db)
    message = await delivery.claim_delivery()
    await delivery.finish_delivery(message, error="temporary")
    with shifted_database_clock(db):
        assert await delivery.claim_delivery() is None


async def test_active_lease_is_not_stolen_after_clock_regression(db):
    identifier = await request(db)
    delivery = runtime(db)
    lease, _ = await delivery.claim_execution(identifier)
    with shifted_database_clock(db):
        assert await delivery.claim_execution(identifier) is None
        await delivery.renew(lease)
