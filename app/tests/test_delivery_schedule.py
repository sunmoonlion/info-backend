"""Shared scheduling contract, including real-DB atomic hand-off fault cases."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import event
from test_durable_delivery_db import (
    TOPIC,
    age_published,
    due,
    increment,
    request,
    runtime,
    sql,
)
from test_durable_delivery_db import (
    db as db,
)

from app.application.dto.outbox import NOT_BEFORE_HEADER, OutboxEvent
from app.application.services.durable_tasks import DurableTasks, enqueue_task
from app.infrastructure.messaging.durable_delivery import DeliveryLeaseLost
from app.infrastructure.repositories.outbox import SqlOutboxRepository
from app.tasks.durable_delivery import pump


def intent(**kwargs):
    return OutboxEvent(
        topic=TOPIC,
        aggregate_key="counter:1",
        deduplication_key="next",
        payload={},
        **kwargs,
    )


def test_schedule_requires_timezone_and_reserves_metadata():
    with pytest.raises(ValidationError):
        intent(not_before=datetime(2026, 9, 13))
    with pytest.raises(ValidationError, match="reserved"):
        intent(headers={NOT_BEFORE_HEADER: "2026-09-13T00:00:00+00:00"})
    value = intent(
        not_before=datetime(2026, 9, 13, 8, tzinfo=timezone(timedelta(hours=8)))
    )
    assert value.not_before == datetime(2026, 9, 13, tzinfo=UTC)
    assert value.transport_headers() == {
        NOT_BEFORE_HEADER: "2026-09-13T00:00:00.000000+00:00"
    }
    assert intent().transport_headers() == {}


def test_schedule_counts_towards_header_and_payload_limits():
    headers = {str(i): "v" for i in range(32)}
    intent(headers=headers)
    with pytest.raises(ValidationError, match="headers"):
        intent(headers=headers, not_before=datetime.now(UTC))
    payload = {"v": "x" * 262_110}
    OutboxEvent(topic=TOPIC, aggregate_key="a", deduplication_key="a", payload=payload)
    with pytest.raises(ValidationError, match="256 KiB"):
        OutboxEvent(
            topic=TOPIC,
            aggregate_key="a",
            deduplication_key="a",
            payload=payload,
            not_before=datetime.now(UTC),
        )


async def schedule(db, when, *, key="next"):
    async with db() as s, s.begin():
        return await enqueue_task(
            s,
            topic=TOPIC,
            key="counter:1",
            payload={},
            deduplication_key=key,
            not_before=when,
        )


async def database_time(db):
    return await sql(db, "SELECT clock_timestamp()")


async def future(db):
    return await database_time(db) + timedelta(hours=1)


async def test_future_message_neither_publishes_nor_takes_worker_lease(db):
    when = await future(db)
    message_id = await schedule(db, when)
    assert await sql(db, "SELECT available_at FROM outbox_message") == when
    delivery = runtime(db)
    assert await delivery.claim_delivery() is None
    assert await delivery.consume(message_id) is False
    assert await delivery.reconcile() == 0
    async with db() as s, s.begin():
        assert (
            await SqlOutboxRepository().claim_batch(
                s, owner="test", limit=1, lease_seconds=60
            )
            == []
        )
    assert await sql(db, "SELECT attempt_count FROM outbox_message") == 0
    assert await sql(db, "SELECT count(*) FROM outbox_execution") == 0
    assert await sql(db, "SELECT count(*) FROM inbox_message") == 0
    assert await sql(db, "SELECT value FROM delivery_test_counter") == 0


async def test_due_message_works_after_publisher_advances_transport_backoff(db):
    message_id = await schedule(db, await database_time(db) - timedelta(seconds=1))
    delivery = runtime(db)
    published = []

    async def publish(message):
        published.append(message["id"])

    assert await pump(delivery, publish) == 1
    assert published == [message_id]
    # Successful publication advances available_at; that is NOT a consumer timer.
    assert await sql(db, "SELECT available_at>clock_timestamp() FROM outbox_message")
    assert await runtime(db).consume(message_id) is True
    assert await runtime(db).consume(message_id) is False
    assert await sql(db, "SELECT value FROM delivery_test_counter") == 1


async def test_due_message_also_works_through_repository_claim(db):
    message_id = await schedule(db, await database_time(db) - timedelta(seconds=1))
    async with db() as s, s.begin():
        claimed = await SqlOutboxRepository().claim_batch(
            s, owner="test", limit=1, lease_seconds=60
        )
    assert [item.id for item in claimed] == [message_id]
    assert await runtime(db).consume(message_id)


async def test_schedule_is_immutable_idempotent_intent_not_mutable_backoff(db):
    when = await future(db)
    message_id = await schedule(db, when)
    await due(db)
    before = await sql(db, "SELECT available_at FROM outbox_message")
    same_time = when.astimezone(timezone(timedelta(hours=8)))
    assert await schedule(db, same_time) == message_id
    assert await sql(db, "SELECT available_at FROM outbox_message") == before
    for changed in (when + timedelta(seconds=1), None):
        with pytest.raises(ValueError, match="different intent"):
            await schedule(db, changed)
    # Neither publishing path nor a direct broker hint can bypass the original timer.
    assert await runtime(db).claim_delivery() is None
    async with db() as s, s.begin():
        assert (
            await SqlOutboxRepository().claim_batch(
                s, owner="test", limit=1, lease_seconds=60
            )
            == []
        )
    assert await runtime(db).consume(message_id) is False


async def test_retry_cannot_add_a_schedule_to_an_existing_immediate_intent(db):
    message_id = await schedule(db, None)
    with pytest.raises(ValueError, match="different intent"):
        await schedule(db, await future(db))
    assert await schedule(db, None) == message_id


async def test_mutated_dto_cannot_forge_reserved_header(db):
    value = intent()
    value.headers[NOT_BEFORE_HEADER] = "infinity"
    with pytest.raises(ValidationError, match="reserved"):
        async with db() as s, s.begin():
            await SqlOutboxRepository().enqueue(s, value)
    assert await sql(db, "SELECT count(*) FROM outbox_message") == 0


async def test_reconcile_and_dead_letter_replay_do_not_advance_original_timer(db):
    message_id = await schedule(db, await future(db))
    delivery = runtime(db)
    # Fault injection: stale publication metadata despite a future schedule.
    await sql(db, "UPDATE outbox_message SET status='published'")
    await age_published(db)
    assert await delivery.reconcile() == 1
    assert await delivery.claim_delivery() is None
    assert await delivery.consume(message_id) is False
    await sql(
        db,
        "INSERT INTO outbox_dead_letter(message_id,error_code) "
        "VALUES (:id,'test_fault')",
        id=message_id,
    )
    await delivery.replay(message_id)
    assert await delivery.claim_delivery() is None
    assert await delivery.consume(message_id) is False
    assert await sql(db, "SELECT attempt_count FROM outbox_message") == 0


async def test_transport_retry_keeps_schedule_and_backoff(db):
    message_id = await schedule(db, await database_time(db) - timedelta(seconds=1))
    delivery = runtime(db)
    before = await sql(db, "SELECT headers FROM outbox_message")
    message = await delivery.claim_delivery()
    await delivery.finish_delivery(message, error="ConnectionError")
    assert await delivery.claim_delivery() is None
    assert await sql(db, "SELECT headers FROM outbox_message") == before
    await due(db)
    assert (await delivery.claim_delivery())["id"] == message_id


@pytest.mark.parametrize("failure", ["body", "commit", None])
async def test_successor_business_and_inbox_are_one_transaction(db, failure):
    message_id = await request(db)
    when = await future(db)

    async def advance(s, payload):
        await increment(s, payload)
        await enqueue_task(
            s,
            topic=TOPIC,
            key="counter:1",
            payload={},
            deduplication_key="next",
            not_before=when,
        )
        # A separate connection cannot yet observe either state or successor.
        assert await sql(db, "SELECT value FROM delivery_test_counter") == 0
        assert await sql(db, "SELECT count(*) FROM outbox_message") == 1

        def fail(_):
            raise RuntimeError("injected crash")

        if failure == "body":
            fail(s)
        if failure == "commit":
            event.listen(s.sync_session, "before_commit", fail, once=True)

    delivery = DurableTasks(db, handlers={TOPIC: advance})
    if failure:
        with pytest.raises(RuntimeError, match="injected crash"):
            await delivery.consume(message_id)
        assert await sql(db, "SELECT count(*) FROM outbox_message") == 1
        assert await sql(db, "SELECT count(*) FROM inbox_message") == 0
        assert await sql(db, "SELECT value FROM delivery_test_counter") == 0
        failure = None
    assert await delivery.consume(message_id)
    assert await delivery.consume(message_id) is False
    assert await sql(db, "SELECT count(*) FROM outbox_message") == 2
    assert await sql(db, "SELECT count(*) FROM inbox_message") == 1
    assert await sql(db, "SELECT value FROM delivery_test_counter") == 1
    successor = await sql(
        db, "SELECT id FROM outbox_message WHERE deduplication_key='next'"
    )
    assert await runtime(db).consume(successor) is False


@pytest.mark.parametrize("failure", ["lease", "cancel"])
async def test_lost_worker_cannot_leave_successor_without_ack(db, failure):
    message_id = await request(db)
    when = await future(db)
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def advance(s, payload):
        await increment(s, payload)
        await enqueue_task(
            s,
            topic=TOPIC,
            key="counter:1",
            payload={},
            deduplication_key="next",
            not_before=when,
        )
        entered.set()
        await proceed.wait()

    worker = asyncio.create_task(
        DurableTasks(db, handlers={TOPIC: advance}).consume(message_id)
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    if failure == "lease":
        await sql(db, "UPDATE outbox_execution SET expires_at='2000-01-01'")
        proceed.set()
        with pytest.raises(DeliveryLeaseLost):
            await worker
    else:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
    assert await sql(db, "SELECT count(*) FROM outbox_message") == 1
    assert await sql(db, "SELECT count(*) FROM inbox_message") == 0
    assert await sql(db, "SELECT value FROM delivery_test_counter") == 0
    assert await runtime(db).consume(message_id)
