"""B7c: real PostgreSQL concurrency, write fences and non-destructive migration."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from test_durable_delivery_db import db as db
from test_durable_delivery_db import sql
from test_info_delivery_db import upload

from app.application.services import info_crawl_service as service
from app.cli.distribution_preflight import audit_database
from app.infrastructure.models.info import DistributionRecord

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/20260913_0009_distribution_identity.py"
)
COUNT_COMMANDS = (
    "SELECT count(*) FROM outbox_message WHERE topic='info.distribution.dispatch.v1'"
)


async def migrate(db, action):
    spec = importlib.util.spec_from_file_location("distribution_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    async with db() as s, s.begin():
        connection = await s.connection()

        def apply(c):
            with Operations.context(MigrationContext.configure(c)):
                getattr(module, action)()

        await connection.run_sync(apply)


async def seed(db, monkeypatch, dispatch=False):
    async with db() as s:
        version = await upload(s, monkeypatch)
        return await service.create_knowledge_distribution(
            s, document_version_id=version.id, dispatch=dispatch
        )


async def snapshot(db):
    return await sql(
        db,
        """
        SELECT jsonb_agg(to_jsonb(d)-'write_protocol_version' ORDER BY id)
        FROM distribution_record d
    """,
    )


async def clone(db, record_id, dataset, *, protocol=True, value=1):
    column = ",write_protocol_version" if protocol else ""
    expression = ",:protocol" if protocol else ""
    await sql(
        db,
        f"""
        INSERT INTO distribution_record
          (id,document_id,document_version_id,target_app,target_dataset,
           content_hash,status,payload,created_at,updated_at{column})
        SELECT :new_id,document_id,document_version_id,target_app,:dataset,
               content_hash,status,payload,created_at,updated_at{expression}
        FROM distribution_record WHERE id=:id
    """,
        new_id=uuid4(),
        id=record_id,
        dataset=dataset,
        protocol=value,
    )


@pytest.mark.parametrize("dispatch", [False, True])
async def test_concurrent_creators_share_one_record_and_command(
    db, monkeypatch, dispatch
):
    async with db() as s:
        version = await upload(s, monkeypatch)

    async def create(index):
        async with db() as s:
            return await service.create_knowledge_distribution(
                s,
                document_version_id=version.id,
                target_dataset=[None, "", "default"][index % 3],
                dispatch=dispatch,
            )

    records = await asyncio.wait_for(
        asyncio.gather(*(create(i) for i in range(12))), 15
    )
    assert len({r.id for r in records}) == 1
    assert all(r.payload == records[0].payload for r in records)
    assert await sql(db, "SELECT count(*) FROM distribution_record") == 1
    assert await sql(db, COUNT_COMMANDS) == int(dispatch)


async def test_manual_then_dispatch_is_one_intent_and_same_frozen_snapshot(
    db, monkeypatch
):
    original = await seed(db, monkeypatch)
    frozen = copy.deepcopy(original.payload)

    async def must_not_select(*args, **kwargs):
        raise AssertionError(
            "duplicate creation must not select a replacement artifact"
        )

    monkeypatch.setattr(service, "_select_distribution_artifact", must_not_select)
    for dispatch in (False, True, True, False):
        async with db() as s:
            result = await service.create_knowledge_distribution(
                s, document_version_id=original.document_version_id, dispatch=dispatch
            )
        assert result.id == original.id
        assert result.payload == frozen
    assert await sql(db, COUNT_COMMANDS) == 1


async def test_retry_after_commit_ack_loss_returns_committed_identity(db, monkeypatch):
    async with db() as s:
        version = await upload(s, monkeypatch)
    async with db() as lost:
        commit = lost.commit

        async def commit_then_lose_response():
            await commit()
            raise ConnectionError("commit acknowledgement lost")

        monkeypatch.setattr(lost, "commit", commit_then_lose_response)
        with pytest.raises(ConnectionError, match="acknowledgement lost"):
            await service.create_knowledge_distribution(
                lost, document_version_id=version.id, dispatch=True
            )
    committed = await snapshot(db)
    async with db() as retry:
        repeated = await service.create_knowledge_distribution(
            retry, document_version_id=version.id, dispatch=True
        )
    assert str(repeated.id) == committed[0]["id"]
    assert await snapshot(db) == committed
    assert await sql(db, COUNT_COMMANDS) == 1


@pytest.mark.parametrize("status", ["running", "failed", "succeeded", "cancelled"])
async def test_repeated_creation_keeps_state_receipt_and_does_not_retry(
    db, monkeypatch, status
):
    original = await seed(db, monkeypatch)
    async with db() as s:
        await service.update_distribution_status(
            s,
            distribution_id=original.id,
            status=status,
            last_error="preserve",
            metadata={"remote_id": "existing-receipt"},
        )
    before = await snapshot(db)
    async with db() as s:
        result = await service.create_knowledge_distribution(
            s, document_version_id=original.document_version_id, dispatch=True
        )
    assert result.id == original.id and result.status == status
    assert await snapshot(db) == before
    assert await sql(db, COUNT_COMMANDS) == 0


async def test_explicit_retry_retains_id_and_duplicate_create_keeps_generation(
    db, monkeypatch
):
    original = await seed(db, monkeypatch, dispatch=True)
    async with db() as s:
        await service.update_distribution_status(
            s, distribution_id=original.id, status="failed", last_error="timeout"
        )
        retried = await service.retry_distribution(s, distribution_id=original.id)
        frozen = copy.deepcopy(retried.payload)
        repeated = await service.create_knowledge_distribution(
            s, document_version_id=original.document_version_id, dispatch=True
        )
    assert repeated.id == original.id
    assert repeated.payload == frozen
    assert len(repeated.payload["retry_history"]) == 1
    assert await sql(db, COUNT_COMMANDS) == 2


async def test_existing_identity_map_is_refreshed_before_deciding_to_dispatch(
    db, monkeypatch
):
    original = await seed(db, monkeypatch)
    async with db() as stale:
        cached = await stale.get(DistributionRecord, original.id)
        assert cached.status == "pending"
        await stale.commit()
        async with db() as current:
            await service.update_distribution_status(
                current,
                distribution_id=original.id,
                status="succeeded",
                last_error=None,
            )
        repeated = await service.create_knowledge_distribution(
            stale, document_version_id=original.document_version_id, dispatch=True
        )
        assert repeated is cached and repeated.status == "succeeded"
    assert await sql(db, COUNT_COMMANDS) == 0


async def test_distinct_datasets_and_versions_are_distinct_deliveries(db, monkeypatch):
    original = await seed(db, monkeypatch)
    async with db() as s:
        other_dataset = await service.create_knowledge_distribution(
            s,
            document_version_id=original.document_version_id,
            target_dataset="another",
        )
        version = await upload(s, monkeypatch)
        other_version = await service.create_knowledge_distribution(
            s, document_version_id=version.id
        )
    assert len({original.id, other_dataset.id, other_version.id}) == 3
    assert (
        len(
            {
                r.payload["idempotency_key"]
                for r in (original, other_dataset, other_version)
            }
        )
        == 3
    )


@pytest.mark.parametrize("existing", [False, True])
async def test_outbox_failure_rolls_back_creation_or_reuse(db, monkeypatch, existing):
    async with db() as s:
        version = await upload(s, monkeypatch)
        if existing:
            await service.create_knowledge_distribution(
                s, document_version_id=version.id
            )
    before = await snapshot(db)

    async def fail(*args, **kwargs):
        raise RuntimeError("injected outbox failure")

    monkeypatch.setattr(service, "ensure_distribution_dispatch_outbox", fail)
    with pytest.raises(RuntimeError, match="outbox failure"):
        async with db() as s:
            await service.create_knowledge_distribution(
                s, document_version_id=version.id, dispatch=True
            )
    assert await snapshot(db) == before
    assert await sql(db, COUNT_COMMANDS) == 0


@pytest.mark.parametrize("dataset", [None, "", "default"])
async def test_database_unique_index_fences_non_service_duplicate_writers(
    db, monkeypatch, dataset
):
    original = await seed(db, monkeypatch)
    with pytest.raises(IntegrityError, match="uq_distribution_logical_delivery_v1"):
        await clone(db, original.id, dataset)
    assert await sql(db, "SELECT count(*) FROM distribution_record") == 1


@pytest.mark.parametrize("protocol,value", [(False, 1), (True, 2)])
async def test_old_or_unknown_writer_fails_on_unused_dataset(
    db, monkeypatch, protocol, value
):
    original = await seed(db, monkeypatch)
    with pytest.raises(IntegrityError):
        await clone(db, original.id, "unused", protocol=protocol, value=value)
    assert await sql(db, "SELECT count(*) FROM distribution_record") == 1


@pytest.mark.parametrize(
    "assignment",
    [
        "target_dataset='changed'",
        "target_app='changed'",
        "content_hash='changed'",
        "document_id=uuid_generate_v4()",
        "document_version_id=uuid_generate_v4()",
    ],
)
async def test_database_prevents_rekeying_existing_delivery(
    db, monkeypatch, assignment
):
    original = await seed(db, monkeypatch)
    with pytest.raises(DBAPIError, match="distribution_identity_is_immutable"):
        await sql(
            db,
            f"UPDATE distribution_record SET {assignment} WHERE id=:id",
            id=original.id,
        )


@pytest.mark.parametrize("dataset", [None, "", "default"])
async def test_preflight_and_migration_reject_legacy_duplicates_without_changes(
    db, monkeypatch, dataset
):
    original = await seed(db, monkeypatch, dispatch=True)
    await migrate(db, "downgrade")
    await clone(db, original.id, dataset, protocol=False)
    before = await snapshot(db)
    report = await audit_database(db)
    assert report["read_only"] and not report["ready"]
    assert report["conflicting_groups"] == 1 and report["conflicting_records"] == 2
    assert "payload" not in str(report) and "default" not in str(report)
    with pytest.raises(RuntimeError, match="distribution_identity_preflight_failed"):
        await migrate(db, "upgrade")
    assert await snapshot(db) == before
    assert await sql(db, COUNT_COMMANDS) == 1
    assert (
        await sql(
            db,
            "SELECT count(*) FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='distribution_record' AND column_name='write_protocol_version'",
        )
        == 0
    )


@pytest.mark.parametrize("dataset", [None, "", "default"])
async def test_nonempty_migration_roundtrip_preserves_legacy_rows_and_intents(
    db, monkeypatch, dataset
):
    original = await seed(db, monkeypatch, dispatch=True)
    await migrate(db, "downgrade")
    await sql(
        db,
        "UPDATE distribution_record SET target_dataset=:dataset WHERE id=:id",
        dataset=dataset,
        id=original.id,
    )
    before = await snapshot(db)
    assert (await audit_database(db))["ready"]
    await migrate(db, "upgrade")
    assert await snapshot(db) == before
    async with db() as s:
        repeated = await service.create_knowledge_distribution(
            s, document_version_id=original.document_version_id, dispatch=True
        )
    assert repeated.id == original.id
    assert await snapshot(db) == before
    assert await sql(db, COUNT_COMMANDS) == 1
    await migrate(db, "downgrade")
    assert await snapshot(db) == before
    await migrate(db, "upgrade")
    assert await snapshot(db) == before


async def test_preflight_samples_are_bounded_but_counts_are_complete(db, monkeypatch):
    original = await seed(db, monkeypatch)
    await migrate(db, "downgrade")
    async with db() as s, s.begin():
        await s.execute(
            text("""
            INSERT INTO distribution_record
              (id,document_id,document_version_id,target_app,target_dataset,
               content_hash,status,payload,created_at,updated_at)
            SELECT uuid_generate_v4(),document_id,document_version_id,target_app,
                   'private-dataset-' || i,content_hash,status,payload,created_at,updated_at
            FROM distribution_record CROSS JOIN generate_series(1,60) i
                 CROSS JOIN generate_series(1,2) copies
            WHERE id=:id
        """),
            {"id": original.id},
        )
    report = await audit_database(db)
    assert report["total_records"] == 121
    assert report["conflicting_groups"] == 60 and report["conflicting_records"] == 120
    assert len(report["samples"]) == 50
    assert "private-dataset" not in str(report)


def test_offline_migration_is_rejected():
    spec = importlib.util.spec_from_file_location("distribution_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(
        MigrationContext.configure(url="postgresql://", opts={"as_sql": True})
    ):
        with pytest.raises(RuntimeError, match="online data audit"):
            module.upgrade()
