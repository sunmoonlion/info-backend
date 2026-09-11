"""Info integration checks: real domain writes and template delivery share a DB."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from test_durable_delivery_db import db as db
from test_durable_delivery_db import sql

from app.application.services import info_crawl_service as service
from app.infrastructure.storage.object_storage import StoredObject

ROOT = Path(__file__).resolve().parents[1]


class MemoryStorage:
    def put_bytes(self, *, object_key, data, content_type, **kwargs):
        return StoredObject(
            "test-bucket",
            object_key,
            "immutable-version",
            hashlib.sha256(data).hexdigest(),
            len(data),
            content_type,
        )


async def upload(session, monkeypatch):
    monkeypatch.setattr(service, "get_object_storage", MemoryStorage)
    return await service.ingest_uploaded_file(
        session,
        filename="test.md",
        content=b"# Title\n\nReliable content",
        content_type="text/markdown",
    )


async def test_crawl_request_persists_intent_and_manual_creation_stays_manual(db):
    async with db() as s:
        manual = await service.create_crawl_job(
            s, target_url="https://example.com/manual", source_id=None, enqueue=False
        )
        assert await sql(db, "SELECT count(*) FROM outbox_message") == 0
        job = await service.create_crawl_job(
            s, target_url="https://example.com/run", source_id=None, enqueue=True
        )
        assert (
            await sql(
                db,
                "SELECT count(*) FROM outbox_message WHERE aggregate_key=:id",
                id=str(job.id),
            )
            == 1
        )
        await service.request_crawl_job(s, job.id)
        assert await sql(db, "SELECT count(*) FROM outbox_message") == 1
        await service.request_crawl_job(s, manual.id)
        assert await sql(db, "SELECT count(*) FROM outbox_message") == 2


async def test_crawl_intent_failure_rolls_back_the_job(db, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("outbox unavailable")

    monkeypatch.setattr(service, "enqueue_task", fail)
    with pytest.raises(RuntimeError):
        async with db() as s:
            await service.create_crawl_job(
                s,
                target_url="https://example.com/rollback",
                source_id=None,
                enqueue=True,
            )
    assert await sql(db, "SELECT count(*) FROM crawl_job") == 0
    assert await sql(db, "SELECT count(*) FROM outbox_message") == 0


async def test_uploaded_version_and_index_command_commit_together(db, monkeypatch):
    async with db() as s:
        version = await upload(s, monkeypatch)
        assert await sql(db, "SELECT count(*) FROM info_document_version") == 1
        assert await sql(
            db, "SELECT aggregate_key FROM outbox_message WHERE topic='info.index.v1'"
        ) == str(version.id)


async def test_index_intent_failure_does_not_leave_a_committed_version(db, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("index enqueue failed")

    monkeypatch.setattr(service, "_enqueue_index_document_version", fail)
    with pytest.raises(RuntimeError):
        async with db() as s:
            await upload(s, monkeypatch)
    assert await sql(db, "SELECT count(*) FROM info_document_version") == 0
    assert await sql(db, "SELECT count(*) FROM crawl_job") == 0


async def test_distribution_retries_preserve_remote_business_identity(db, monkeypatch):
    from app.application.services.durable_tasks import DurableTasks
    from app.infrastructure.messaging.delivery_handlers import get_delivery_handlers

    async with db() as s:
        version = await upload(s, monkeypatch)
        record = await service.create_knowledge_distribution(
            s, document_version_id=version.id, dispatch=True
        )
        record_id = record.id
    message_id = await sql(
        db, "SELECT id FROM outbox_message WHERE topic='info.distribution.dispatch.v1'"
    )
    calls = []

    class Client:
        async def ingest_document(self, payload):
            calls.append(payload)
            if len(calls) == 1:
                raise ConnectionError("response lost after remote accept")
            return {"id": "same-remote-ingestion"}

    monkeypatch.setattr(service, "get_knowledge_app_client", Client)
    runtime = DurableTasks(db, handlers=get_delivery_handlers())
    with pytest.raises(RuntimeError, match="not_acknowledged"):
        await runtime.consume(message_id)
    assert await sql(db, "SELECT count(*) FROM inbox_message") == 0
    assert await runtime.consume(message_id)
    assert calls[0] == calls[1]
    assert await runtime.consume(message_id) is False
    assert len(calls) == 2
    assert (
        await sql(
            db, "SELECT status FROM distribution_record WHERE id=:id", id=record_id
        )
        == "succeeded"
    )


async def test_duplicate_crawl_after_terminal_commit_does_not_fetch_again(
    db, monkeypatch
):
    from app.application.services.durable_tasks import DurableTasks
    from app.infrastructure.messaging.delivery_handlers import get_delivery_handlers

    async with db() as s:
        job = await service.create_crawl_job(
            s, target_url="https://example.com/done", source_id=None, enqueue=True
        )
        job.status = "succeeded"
        await s.commit()
    message_id = await sql(
        db, "SELECT id FROM outbox_message WHERE topic='info.crawl.v1'"
    )

    class NoNetwork:
        def __init__(self, **kwargs):
            raise AssertionError("terminal crawl must not fetch again")

    monkeypatch.setattr(service.httpx, "AsyncClient", NoNetwork)
    assert await DurableTasks(db, handlers=get_delivery_handlers()).consume(message_id)


@pytest.mark.parametrize("failure", ["lease_lost", "cancelled"])
async def test_interrupted_crawl_does_not_commit_a_terminal_result(
    db, monkeypatch, failure
):
    from app.application.services.durable_tasks import DurableTasks
    from app.infrastructure.messaging.delivery_handlers import get_delivery_handlers
    from app.infrastructure.messaging.durable_delivery import DeliveryLeaseLost

    monkeypatch.setattr(service, "get_object_storage", MemoryStorage)
    async with db() as s:
        job = await service.create_crawl_job(
            s,
            target_url="https://example.com/interrupted",
            source_id=None,
            enqueue=True,
        )
        job_id = job.id
    message_id = await sql(
        db, "SELECT id FROM outbox_message WHERE topic='info.crawl.v1'"
    )
    error = DeliveryLeaseLost if failure == "lease_lost" else asyncio.CancelledError

    class InterruptedClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            raise error("execution interrupted")

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(service.httpx, "AsyncClient", InterruptedClient)
    with pytest.raises(error):
        await DurableTasks(db, handlers=get_delivery_handlers()).consume(message_id)
    assert (
        await sql(db, "SELECT status FROM crawl_job WHERE id=:id", id=job_id)
        == "running"
    )
    assert (
        await sql(db, "SELECT finished_at FROM crawl_job WHERE id=:id", id=job_id)
        is None
    )
    assert await sql(db, "SELECT count(*) FROM inbox_message") == 0


async def test_rebuild_reports_queued_and_defers_remote_indexing(db, monkeypatch):
    async with db() as s:
        await upload(s, monkeypatch)

    class Search:
        enabled = True
        index_name = "isolated-test-index"

        async def ensure_index(self):
            raise AssertionError("rebuild request must only persist commands")

    monkeypatch.setattr(service, "get_info_search_index", Search)
    async with db() as s:
        result = await service.rebuild_search_index(s, limit=10)
    assert result["queued"] == 1
    assert result["indexed"] == 0
    assert (
        await sql(db, "SELECT count(*) FROM outbox_message WHERE topic='info.index.v1'")
        == 2
    )


async def test_rebuild_enqueue_failure_rolls_back_all_commands(db, monkeypatch):
    from sqlalchemy import select

    from app.infrastructure.models.info import InfoDocumentVersion

    async with db() as s:
        await upload(s, monkeypatch)
        version = (await s.execute(select(InfoDocumentVersion))).scalar_one()
        s.add(
            InfoDocumentVersion(
                document_id=version.document_id,
                version_no=2,
                source_url=version.source_url,
                title="Second version",
                content_hash="b" * 64,
            )
        )
        await s.commit()

    class Search:
        enabled = True
        index_name = "isolated-test-index"

    original = service.enqueue_task
    calls = 0

    async def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("outbox unavailable")
        return await original(*args, **kwargs)

    monkeypatch.setattr(service, "get_info_search_index", Search)
    monkeypatch.setattr(service, "enqueue_task", fail_second)
    with pytest.raises(RuntimeError, match="outbox unavailable"):
        async with db() as s:
            await service.rebuild_search_index(s, limit=10)
    assert await sql(db, "SELECT count(*) FROM outbox_message") == 1


def test_legacy_cli_rejects_unsupported_limit(monkeypatch):
    from app.cli.drain_delivery_outbox import main

    monkeypatch.setattr("sys.argv", ["drain_delivery_outbox", "--limit", "1"])
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 2


async def test_legacy_migration_preserves_id_and_completed_receipt(db, monkeypatch):
    import importlib.util
    import uuid

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    path = ROOT / "alembic/versions/20260911_0007_durable_delivery.py"
    spec = importlib.util.spec_from_file_location("info_delivery_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async def migrate(fn):
        async with db() as s, s.begin():
            connection = await s.connection()

            def apply(conn):
                with Operations.context(MigrationContext.configure(conn)):
                    fn()

            await connection.run_sync(apply)

    await migrate(module.downgrade)
    async with db() as s:
        version = await upload(s, monkeypatch)
        record = await service.create_knowledge_distribution(
            s, document_version_id=version.id, dispatch=False
        )
        message_id = uuid.uuid4()
        await s.execute(
            text("""
            INSERT INTO delivery_outbox_message(id,topic,aggregate_type,aggregate_id,
                idempotency_key,payload,state,available_at)
            VALUES (:id,'info.distribution.dispatch.v1','distribution_record',:record,
                :key,CAST(:payload AS jsonb),'completed',clock_timestamp())
        """),
            {
                "id": message_id,
                "record": record.id,
                "key": f"info.distribution:{record.id}:dispatch-v1",
                "payload": '{"distribution_id":"' + str(record.id) + '"}',
            },
        )
        await s.commit()
    await migrate(module.upgrade)
    assert (
        await sql(db, "SELECT count(*) FROM outbox_message WHERE id=:id", id=message_id)
        == 1
    )
    assert (
        await sql(
            db, "SELECT count(*) FROM inbox_message WHERE message_id=:id", id=message_id
        )
        == 1
    )
    assert await sql(db, "SELECT count(*) FROM delivery_outbox_message_legacy") == 1
    assert await sql(db, "SELECT to_regclass('delivery_outbox_message')") is None

    from sqlalchemy.exc import DBAPIError

    from app.application.services.durable_tasks import enqueue_task

    # A command accepted after cutover must survive downgrade and re-upgrade.
    async with db() as s, s.begin():
        new_id = await enqueue_task(
            s,
            topic="info.index.v1",
            key=str(version.id),
            payload={"document_version_id": str(version.id)},
            deduplication_key="migration-roundtrip-new-command",
        )
    with pytest.raises(DBAPIError, match="drain or restore backup"):
        await migrate(module.downgrade)
    assert (
        await sql(db, "SELECT to_regclass('delivery_outbox_message_legacy')")
        is not None
    )
    # Synthetic completion is only a fixture for the migration roundtrip, not an
    # operational drain procedure (real completion requires domain verification).
    await sql(
        db,
        "INSERT INTO inbox_message(consumer,message_id) "
        "SELECT topic,id FROM outbox_message ON CONFLICT DO NOTHING",
    )
    before_count = await sql(db, "SELECT count(*) FROM outbox_message")
    before_receipts = await sql(db, "SELECT count(*) FROM inbox_message")
    await migrate(module.downgrade)
    assert (
        await sql(
            db, "SELECT state FROM delivery_outbox_message WHERE id=:id", id=message_id
        )
        == "completed"
    )
    await migrate(module.upgrade)
    assert await sql(db, "SELECT count(*) FROM outbox_message") == before_count
    assert await sql(db, "SELECT count(*) FROM inbox_message") == before_receipts
    assert (
        await sql(
            db, "SELECT count(*) FROM inbox_message WHERE message_id=:id", id=new_id
        )
        == 1
    )
