"""Read-only aggregate observation, using real disposable PostgreSQL."""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from test_durable_delivery_db import (
    TOPIC,
    age_published,
    request,
    runtime,
    sql,
)
from test_durable_delivery_db import db as db

from app.application.services.durable_tasks import enqueue_task
from app.cli import delivery_metrics as cli
from app.infrastructure.messaging import delivery_observation as observation
from app.infrastructure.messaging.delivery_handlers import get_delivery_handlers
from app.infrastructure.messaging.delivery_observers import get_delivery_observers
from app.infrastructure.messaging.durable_delivery import DurableDelivery


async def collect(db, delivery=None):
    return await observation.collect_delivery_snapshot(
        db, {"tasks": delivery or runtime(db)}
    )


async def gauges(db, delivery=None):
    return (await collect(db, delivery))["topics"][0]


def test_production_observer_uses_registered_handlers_not_a_second_topic_list():
    observers = get_delivery_observers(None)
    assert observers["tasks"].topics == {
        topic: topic for topic in get_delivery_handlers()
    }


async def dump(db):
    return [
        await sql(db, f"SELECT jsonb_agg(to_jsonb(t)) FROM {table} t")
        for table in (
            "outbox_message",
            "inbox_message",
            "outbox_execution",
            "outbox_dead_letter",
        )
    ]


async def test_empty_and_registered_zero_are_not_reported_as_worker_health(db):
    empty = await observation.collect_delivery_snapshot(db, {})
    assert empty["topics"] == [] and empty["unregistered_messages"] == 0
    registered = await gauges(db)
    assert registered["topic"] == TOPIC and registered["receipt_required"]
    assert all(registered[key] == 0 for key in observation.GAUGES)
    assert "healthy" not in str(empty)


async def test_unregistered_topics_are_counted_without_becoming_labels(db):
    await request(db)
    snapshot = await observation.collect_delivery_snapshot(db, {})
    assert snapshot["unregistered_messages"] == 1
    assert TOPIC not in json.dumps(snapshot)
    assert TOPIC not in observation.render_prometheus(snapshot)


async def test_pending_published_ack_and_reconcile_match_runtime_policy(db):
    await request(db, payload={"private": "must-not-leak"})
    row = await gauges(db)
    assert row["messages"] == row["incomplete_messages"] == 1
    assert row["publishable_messages"] == 1
    delivery = runtime(db)
    message = await delivery.claim_delivery()
    assert (await gauges(db))["publishable_messages"] == 0
    await delivery.finish_delivery(message)
    row = await gauges(db)
    assert row["awaiting_receipt_messages"] == 1
    assert row["reconcile_candidates"] == 0
    await age_published(db)
    assert (await gauges(db))["reconcile_candidates"] == 1
    # Wrong consumer's receipt cannot make this policy appear completed.
    await sql(
        db,
        "INSERT INTO inbox_message(consumer,message_id) VALUES ('wrong',:id)",
        id=message["id"],
    )
    assert (await gauges(db))["incomplete_messages"] == 1
    assert await delivery.consume(message["id"])
    row = await gauges(db)
    assert row["messages"] == 1 and row["incomplete_messages"] == 0
    assert row["oldest_due_incomplete_age_seconds"] == 0
    assert row["reconcile_candidates"] == 0
    assert "must-not-leak" not in json.dumps(await collect(db))


async def test_consumer_override_and_rebuildable_hint_use_actual_policy(db):
    identifier = await request(db)
    custom = DurableDelivery(db, topics={TOPIC: "custom.consumer"})
    hint = DurableDelivery(db, topics={TOPIC: None})
    await sql(
        db,
        "UPDATE outbox_message SET status='published',published_at=clock_timestamp()",
    )
    assert (await gauges(db, custom))["incomplete_messages"] == 1
    row = await gauges(db, hint)
    assert not row["receipt_required"]
    assert row["incomplete_messages"] == row["awaiting_receipt_messages"] == 0
    await sql(
        db,
        "INSERT INTO inbox_message(consumer,message_id) VALUES ('custom.consumer',:id)",
        id=identifier,
    )
    assert (await gauges(db, custom))["incomplete_messages"] == 0


async def test_future_schedule_and_retry_backoff_are_not_immediate_queue_lag(db):
    now = await sql(db, "SELECT clock_timestamp()")
    async with db() as s, s.begin():
        await enqueue_task(
            s,
            topic=TOPIC,
            key="future",
            payload={},
            deduplication_key="future",
            not_before=now + timedelta(hours=1),
        )
    row = await gauges(db)
    assert row["scheduled_messages"] == 1
    assert row["publishable_messages"] == row["oldest_due_incomplete_age_seconds"] == 0
    identifier = await request(db)
    await sql(
        db,
        """
        UPDATE outbox_message SET created_at=clock_timestamp()-interval '2 hours',
            available_at=clock_timestamp()+interval '1 hour' WHERE id=:id
    """,
        id=identifier,
    )
    row = await gauges(db)
    assert row["scheduled_messages"] == 1 and row["publishable_messages"] == 0
    assert 7190 <= row["oldest_due_incomplete_age_seconds"] <= 7220


async def test_active_lease_excludes_stalled_then_expiry_recovers(db):
    identifier = await request(db)
    delivery = runtime(db)
    message = await delivery.claim_delivery()
    await delivery.finish_delivery(message)
    await age_published(db)
    lease, _ = await delivery.claim_execution(identifier)
    row = await gauges(db)
    assert row["active_execution_messages"] == 1
    assert row["awaiting_receipt_messages"] == 1
    assert row["reconcile_candidates"] == 0
    await delivery.release(lease)
    row = await gauges(db)
    assert row["active_execution_messages"] == 0 and row["reconcile_candidates"] == 1


async def test_expired_publisher_lease_and_dead_letter_replay(db):
    identifier = await request(db)
    delivery = runtime(db, max_attempts=1)
    await delivery.claim_delivery()
    await sql(
        db,
        "UPDATE outbox_message SET "
        "lease_expires_at=clock_timestamp()-interval '1 minute'",
    )
    assert (await gauges(db, delivery))["expired_publisher_leases"] == 1
    await delivery.reconcile()
    row = await gauges(db, delivery)
    assert row["dead_letter_messages"] == 1 and row["publishable_messages"] == 0
    assert row["oldest_due_incomplete_age_seconds"] == 0
    await delivery.replay(identifier)
    row = await gauges(db, delivery)
    assert row["dead_letter_messages"] == 0 and row["publishable_messages"] == 1


async def test_snapshot_is_read_only_and_does_not_change_any_ledger(db):
    await request(db)
    before = await dump(db)
    statements = []
    engine = db.kw["bind"]

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.strip())

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        await collect(db)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    assert statements[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    assert all(s.startswith(("SELECT", "WITH", "SET")) for s in statements)
    assert "FOR UPDATE" not in " ".join(statements)
    assert await dump(db) == before


async def test_read_only_database_transaction_rejects_even_trusted_bad_extension(db):
    await request(db)
    await sql(
        db,
        """
        CREATE FUNCTION observation_write_probe() RETURNS int LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE delivery_test_counter SET value=value+1;
            RETURN 1;
        END $$
    """,
    )
    malicious = runtime(db)
    malicious.active_execution_sql = "SELECT 1 WHERE observation_write_probe()=1"
    with pytest.raises(DBAPIError, match="read-only transaction"):
        await gauges(db, malicious)
    assert (await gauges(db))["messages"] == 1
    assert await sql(db, "SELECT value FROM delivery_test_counter") == 0


async def test_database_lock_timeout_and_recovery(db, monkeypatch):
    with monkeypatch.context() as fault:
        fault.setattr(observation, "OBSERVATION_TIMEOUT_SECONDS", 0.05)
        async with db() as blocker, blocker.begin():
            await blocker.execute(
                text("LOCK TABLE outbox_message IN ACCESS EXCLUSIVE MODE")
            )
            with pytest.raises(TimeoutError):
                await collect(db)
    # Recovery uses the production budget, not the injected 50 ms fault budget.
    assert (await collect(db))["unregistered_messages"] == 0


async def test_caller_cancel_propagates_and_connection_recovers(db):
    started = asyncio.Event()

    @asynccontextmanager
    async def blocked():
        started.set()
        await asyncio.Future()
        async with db() as s:
            yield s

    task = asyncio.create_task(observation.collect_delivery_snapshot(blocked, {}))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await collect(db))["unregistered_messages"] == 0


async def test_malformed_schedule_fails_instead_of_emitting_partial_zero(db):
    await request(db)
    await sql(
        db,
        """UPDATE outbox_message SET headers=
        '{"sunmoonai.not_before.v1":"private-invalid-date"}'::jsonb""",
    )
    with pytest.raises(DBAPIError):
        await collect(db)


@pytest.mark.parametrize("fault", ["overlap", "label", "policy_limit", "topic_limit"])
async def test_static_policy_bounds_fail_before_database_access(fault):
    policy = DurableDelivery(None, topics={TOPIC: TOPIC})
    policies = {"tasks": policy}
    if fault == "overlap":
        policies["another"] = policy
    elif fault == "label":
        policies = {'bad"label': policy}
    elif fault == "policy_limit":
        policies = {f"p{i}": DurableDelivery(None, topics={}) for i in range(17)}
    else:
        policies = {
            "tasks": DurableDelivery(None, topics={f"t{i}": None for i in range(129)})
        }
    with pytest.raises(ValueError):
        await observation.collect_delivery_snapshot(None, policies)


def snapshot():
    return {
        "schema_version": 1,
        "observed_at": "2026-09-13T00:00:00+00:00",
        "unregistered_messages": 0,
        "topics": [
            {"policy": "tasks", "topic": TOPIC, **dict.fromkeys(observation.GAUGES, 0)}
        ],
    }


def test_text_gauge_families_are_unique_grouped_and_include_freshness():
    output = observation.render_prometheus(snapshot())
    assert output.endswith("\n")
    assert all(
        line.endswith(" gauge")
        for line in output.splitlines()
        if line.startswith("# TYPE")
    )
    assert "snapshot_timestamp_seconds" in output
    samples = [
        line.split()[0] for line in output.splitlines() if not line.startswith("#")
    ]
    assert len(samples) == len(set(samples)) == len(observation.GAUGES) + 2
    for key in observation.GAUGES:
        assert f"# TYPE sunmoonai_delivery_{key} gauge\n" in output


@pytest.mark.parametrize("format_name", ["json", "prometheus"])
def test_cli_formats_output_only_after_success(monkeypatch, capsys, format_name):
    monkeypatch.setattr(sys, "argv", ["metrics", "--format", format_name])
    monkeypatch.setattr(cli, "run", AsyncMock(return_value=snapshot()))
    cli.main()
    captured = capsys.readouterr()
    assert captured.err == ""
    if format_name == "json":
        assert json.loads(captured.out) == snapshot()
    else:
        assert captured.out == observation.render_prometheus(snapshot())


def test_cli_failure_does_not_print_dsn_or_fake_gauges(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["metrics", "--format", "prometheus"])
    monkeypatch.setattr(
        cli, "run", AsyncMock(side_effect=RuntimeError("secret://password"))
    )
    with pytest.raises(SystemExit) as failure:
        cli.main()
    assert failure.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "delivery_observation_failed\n"


async def test_cli_run_initializes_and_closes_without_mutating_runtime(monkeypatch):
    postgres = SimpleNamespace(
        init=AsyncMock(), shutdown=AsyncMock(), session_factory=object()
    )
    monkeypatch.setattr(cli, "get_postgres", lambda: postgres)
    monkeypatch.setattr(cli, "get_delivery_observers", lambda sessions: {})
    collect_mock = AsyncMock(side_effect=TimeoutError())
    monkeypatch.setattr(cli, "collect_delivery_snapshot", collect_mock)
    with pytest.raises(TimeoutError):
        await cli.run()
    postgres.init.assert_awaited_once()
    postgres.shutdown.assert_awaited_once()


async def test_select_only_role_can_collect_and_revocation_fails(db):
    role = "observation_" + uuid.uuid4().hex
    schema = await sql(db, "SELECT current_schema()")
    await sql(db, f'CREATE ROLE "{role}" NOLOGIN')
    try:
        await sql(db, f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"')
        await sql(
            db,
            "GRANT SELECT ON outbox_message,inbox_message,"
            f'outbox_execution,outbox_dead_letter TO "{role}"',
        )

        @asynccontextmanager
        async def as_role():
            # SET ROLE is committed before the collector owns its read-only transaction.
            async with db() as s:
                await s.execute(text(f'SET ROLE "{role}"'))
                await s.commit()
                try:
                    yield s
                finally:
                    await s.rollback()
                    await s.execute(text("RESET ROLE"))
                    await s.commit()

        assert (await collect(as_role))["unregistered_messages"] == 0
        await sql(db, f'REVOKE SELECT ON outbox_message FROM "{role}"')
        with pytest.raises(DBAPIError):
            await collect(as_role)
    finally:
        await sql(db, f'DROP OWNED BY "{role}"')
        await sql(db, f'DROP ROLE "{role}"')


async def test_server_statement_timeout_is_independent_of_outer_budget(db, monkeypatch):
    monkeypatch.setattr(observation, "OBSERVATION_TIMEOUT_SECONDS", 5)
    async with db() as blocker, blocker.begin():
        await blocker.execute(
            text("LOCK TABLE outbox_message IN ACCESS EXCLUSIVE MODE")
        )
        with pytest.raises(DBAPIError, match="statement timeout"):
            await collect(db)
    assert (await collect(db))["unregistered_messages"] == 0
