"""Committed, retained Inbox evidence is not per-worker health or a lifetime counter."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from test_durable_delivery_db import TOPIC, increment, request, runtime, sql
from test_durable_delivery_db import db as db

from app.application.services.durable_tasks import DurableTasks
from app.infrastructure.messaging.delivery_observation import (
    collect_delivery_snapshot,
    render_prometheus,
)
from app.infrastructure.messaging.durable_delivery import DurableDelivery


async def progress(db, policy=None):
    snapshot = await collect_delivery_snapshot(db, {"tasks": policy or runtime(db)})
    return snapshot["topics"][0]


async def test_uncommitted_and_rolled_back_receipts_are_not_progress(db):
    identifier = await request(db)
    async with db() as writer, writer.begin():
        await writer.execute(
            text("INSERT INTO inbox_message(consumer,message_id) VALUES (:c,:id)"),
            {"c": TOPIC, "id": identifier},
        )
        assert (await progress(db))["retained_receipt_messages"] == 0
        await writer.rollback()
    assert (await progress(db))["latest_receipt_recorded_timestamp_seconds"] == 0


async def test_handler_transaction_is_not_complete_before_commit(db):
    identifier = await request(db)
    started = asyncio.Event()
    release = asyncio.Event()

    async def held(session, payload):
        await increment(session, payload)
        started.set()
        await release.wait()

    policy = DurableTasks(db, handlers={TOPIC: held})
    task = asyncio.create_task(policy.consume(identifier))
    try:
        await asyncio.wait_for(started.wait(), 3)
        row = await progress(db, policy)
        assert row["active_execution_messages"] == 1
        assert row["retained_receipt_messages"] == 0
        assert await sql(db, "SELECT value FROM delivery_test_counter") == 0
        release.set()
        assert await asyncio.wait_for(task, 3)
        row = await progress(db, policy)
        assert row["retained_receipt_messages"] == 1
        assert row["incomplete_messages"] == 0
        expected = await sql(
            db, "SELECT extract(epoch FROM processed_at) FROM inbox_message"
        )
        assert row["latest_receipt_recorded_timestamp_seconds"] == pytest.approx(
            float(expected)
        )
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_handler_rollback_then_recovery_and_duplicate_do_not_overcount(db):
    identifier = await request(db)

    async def fail(session, payload):
        await increment(session, payload)
        raise RuntimeError("synthetic-rollback")

    with pytest.raises(RuntimeError, match="synthetic-rollback"):
        await DurableTasks(db, handlers={TOPIC: fail}).consume(identifier)
    assert await sql(db, "SELECT value FROM delivery_test_counter") == 0
    assert (await progress(db))["retained_receipt_messages"] == 0
    assert await runtime(db).consume(identifier)
    first = await progress(db)
    assert not await runtime(db).consume(identifier)
    assert await progress(db) == first
    assert first["retained_receipt_messages"] == 1
    assert await sql(db, "SELECT value FROM delivery_test_counter") == 1


async def test_only_exact_consumer_and_retained_topic_receipts_count(db):
    identifier = await request(db)
    await sql(
        db,
        "INSERT INTO inbox_message(consumer,message_id) VALUES ('wrong',:id)",
        id=identifier,
    )
    await sql(
        db,
        "INSERT INTO inbox_message(consumer,message_id) VALUES (:c,gen_random_uuid())",
        c=TOPIC,
    )
    assert (await progress(db))["retained_receipt_messages"] == 0
    custom = DurableDelivery(db, topics={TOPIC: "wrong"})
    assert (await progress(db, custom))["retained_receipt_messages"] == 1
    hint = DurableDelivery(db, topics={TOPIC: None})
    await sql(db, "UPDATE outbox_message SET status='published'")
    row = await progress(db, hint)
    assert row["incomplete_messages"] == 0
    assert (
        row["retained_receipt_messages"]
        == row["latest_receipt_recorded_timestamp_seconds"]
        == 0
    )


async def test_retention_and_out_of_order_record_times_are_not_monotonic_counters(db):
    first = await request(db)
    assert await runtime(db).consume(first)
    await sql(db, "UPDATE inbox_message SET processed_at='2000-01-01T00:00:00Z'")
    second = await request(db, key="two")
    assert await runtime(db).consume(second)
    await sql(
        db,
        "UPDATE inbox_message SET processed_at='1999-01-01T00:00:00Z' "
        "WHERE message_id=:id",
        id=second,
    )
    row = await progress(db)
    assert row["retained_receipt_messages"] == 2
    assert row["latest_receipt_recorded_timestamp_seconds"] == 946684800
    # Only synthetic test data: demonstrate why production retention needs a policy.
    await sql(db, "DELETE FROM outbox_message WHERE id=:id", id=first)
    row = await progress(db)
    assert row["retained_receipt_messages"] == 1
    assert row["latest_receipt_recorded_timestamp_seconds"] == 915148800


@pytest.mark.parametrize("timestamp", ["infinity", "-infinity"])
@pytest.mark.parametrize("with_valid", [False, True])
async def test_nonfinite_receipt_time_fails_snapshot_instead_of_emitting_metrics(
    db, timestamp, with_valid
):
    if with_valid:
        valid = await request(db, key="valid")
        assert await runtime(db).consume(valid)
    identifier = await request(db)
    await sql(
        db,
        "INSERT INTO inbox_message(consumer,message_id,processed_at) "
        "VALUES (:c,:id,CAST(CAST(:at AS text) AS timestamptz))",
        c=TOPIC,
        id=identifier,
        at=timestamp,
    )
    with pytest.raises(ValueError, match="invalid receipt timestamp"):
        await progress(db)


async def test_progress_is_available_in_existing_prometheus_output(db):
    identifier = await request(db)
    assert await runtime(db).consume(identifier)
    output = render_prometheus(
        await collect_delivery_snapshot(db, {"tasks": runtime(db)})
    )
    assert "# TYPE sunmoonai_delivery_retained_receipt_messages gauge" in output
    assert (
        "sunmoonai_delivery_retained_receipt_messages"
        f'{{policy="tasks",topic="{TOPIC}"}} 1' in output
    )
    assert "latest_receipt_recorded_timestamp_seconds" in output
    assert "_total" not in output
