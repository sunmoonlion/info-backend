"""Identity/versions and 0008 migration: real PostgreSQL, no business database."""

from __future__ import annotations

import asyncio
import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import DBAPIError, IntegrityError
from test_crawl_concurrency_db import fake_crawl
from test_durable_delivery_db import db as db
from test_durable_delivery_db import sql
from test_info_delivery_db import MemoryStorage

from app.application.services import info_crawl_service as service
from app.cli.identity_preflight import audit_database
from app.domain.info_identity_v1 import identity_key_v1
from app.infrastructure.models.info import InfoDocument, InfoDocumentVersion

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic/versions/20260912_0008_canonical_identity.py"
)


async def migrate(db, action):
    spec = importlib.util.spec_from_file_location("identity_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    async with db() as s, s.begin():
        connection = await s.connection()

        def apply(c):
            with Operations.context(MigrationContext.configure(c)):
                getattr(module, action)()

        await connection.run_sync(apply)


async def document(s, url="https://example.com/article"):
    return await service._find_or_create_document(
        session=s,
        source=None,
        url=url,
        title="Test",
        published_at=None,
        content_hash="a" * 64,
    )


async def add_version(s, doc):
    version = InfoDocumentVersion(
        document_id=doc.id,
        version_no=await service._next_version_no(s, doc.id),
        source_url=doc.canonical_url,
        title="Test",
        content_hash="a" * 64,
    )
    s.add(version)
    await s.flush()
    doc.current_version_id = version.id
    await service._enqueue_index_document_version(s, version.id)
    return version


async def test_concurrent_creation_and_versions_share_one_document(db):
    async def write(index):
        async with db() as s:
            url = (
                "https://EXAMPLE.com:443/article#x"
                if index % 2
                else "https://example.com/article"
            )
            doc = await document(s, url)
            version = await add_version(s, doc)
            await asyncio.sleep(0)  # Deliberately interleave independent transactions.
            await s.commit()
            return doc.id, version.version_no

    results = await asyncio.wait_for(
        asyncio.gather(*(write(i) for i in range(8))), timeout=15
    )
    assert len({result[0] for result in results}) == 1
    assert sorted(result[1] for result in results) == list(range(1, 9))
    assert await sql(db, "SELECT count(*) FROM info_document") == 1
    assert await sql(db, "SELECT count(*) FROM outbox_message") == 8
    assert (
        await sql(
            db,
            "SELECT v.version_no FROM info_document d JOIN info_document_version v ON v.id=d.current_version_id",
        )
        == 8
    )


async def test_concurrent_uploads_preserve_existing_filename_version_semantics(
    db, monkeypatch
):
    monkeypatch.setattr(service, "get_object_storage", MemoryStorage)

    async def upload(index):
        async with db() as s:
            return await service.ingest_uploaded_file(
                s,
                filename="same.md",
                content=f"# Version {index}".encode(),
                content_type="text/markdown",
            )

    versions = await asyncio.wait_for(
        asyncio.gather(*(upload(i) for i in range(6))), timeout=15
    )
    assert len({v.document_id for v in versions}) == 1
    assert sorted(v.version_no for v in versions) == [1, 2, 3, 4, 5, 6]
    assert await sql(db, "SELECT count(*) FROM outbox_message") == 6


async def test_crawl_alias_reuses_current_version_for_unchanged_content(
    db, monkeypatch
):
    calls = fake_crawl(monkeypatch)
    monkeypatch.setattr(
        service, "_extract_html", lambda html, url: ("Title", "Body", "Body", None)
    )
    for url in ("https://EXAMPLE.com:443/article#x", "https://example.com/article"):
        async with db() as s:
            job = await service.create_crawl_job(
                s, target_url=url, source_id=None, enqueue=False
            )
            result = await service.process_crawl_job(s, job.id)
            assert result.status == "succeeded"
    assert len(calls) == 2
    assert await sql(db, "SELECT count(*) FROM info_document") == 1
    assert await sql(db, "SELECT count(*) FROM info_document_version") == 1


async def test_row_lock_refreshes_preloaded_current_version(db):
    async with db() as seed:
        doc = await document(seed)
        doc_id = doc.id
        await seed.commit()
    async with db() as stale:
        loaded = await stale.get(InfoDocument, doc_id)
        assert loaded.current_version_id is None
        async with db() as newer:
            current = await document(newer)
            version = await add_version(newer, current)
            await newer.commit()
        locked = await document(stale)
        assert locked is loaded
        assert locked.current_version_id == version.id
        assert await service._next_version_no(stale, doc_id) == 2


async def test_rollback_releases_creation_and_does_not_consume_version(db):
    started, release = asyncio.Event(), asyncio.Event()

    async def failed_writer():
        async with db() as s:
            doc = await document(s)
            await add_version(s, doc)
            started.set()
            await release.wait()
            await s.rollback()

    first = asyncio.create_task(failed_writer())
    await asyncio.wait_for(started.wait(), timeout=5)

    async def winner():
        async with db() as s:
            doc = await document(s)
            version = await add_version(s, doc)
            await s.commit()
            return version.version_no

    second = asyncio.create_task(winner())
    try:
        await asyncio.sleep(0.03)
        assert not second.done()
        release.set()
        assert await asyncio.wait_for(second, timeout=5) == 1
        await first
    finally:
        release.set()
        first.cancel()
        second.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
    assert await sql(db, "SELECT count(*) FROM info_document_version") == 1
    assert await sql(db, "SELECT count(*) FROM outbox_message") == 1


async def test_constraints_reject_duplicate_missing_or_changed_identity(db):
    async with db() as s:
        original = await document(s)
        doc_id = original.id
        await s.commit()
    with pytest.raises(IntegrityError):
        async with db() as s:
            s.add(
                InfoDocument(
                    canonical_url="https://EXAMPLE.com:443/article#x", title="dup"
                )
            )
            await s.commit()
    with pytest.raises(IntegrityError):
        await sql(
            db,
            "INSERT INTO info_document(id,canonical_url,title,status,metadata_json) VALUES (:id,'https://old-writer.test','old','active','{}')",
            id=uuid.uuid4(),
        )
    with pytest.raises(IntegrityError, match="canonical_identity_is_immutable"):
        await sql(
            db,
            "UPDATE info_document SET canonical_url='https://wrong.test' WHERE id=:id",
            id=doc_id,
        )
    with pytest.raises(IntegrityError, match="canonical_identity_is_immutable"):
        await sql(
            db,
            "UPDATE info_document SET canonical_identity=:key WHERE id=:id",
            id=doc_id,
            key="f" * 64,
        )
    with pytest.raises(IntegrityError, match="write_protocol_version"):
        await sql(
            db,
            "INSERT INTO info_document_version(id,document_id,version_no,source_url,title,content_hash,extraction_status,metadata_json) "
            "VALUES (:id,:doc,1,'https://example.com/article','old writer',:hash,'succeeded','{}')",
            id=uuid.uuid4(),
            doc=doc_id,
            hash="a" * 64,
        )
    assert (
        await sql(db, "SELECT canonical_url FROM info_document")
        == "https://example.com/article"
    )


async def test_digest_collision_or_corrupt_identity_never_merges(db):
    async with db() as s:
        s.add(
            InfoDocument(
                canonical_url="https://wrong.test",
                canonical_identity=identity_key_v1("https://example.com/article"),
                title="corrupt",
            )
        )
        await s.commit()
    with pytest.raises(ValueError, match="^canonical_identity_conflict$"):
        async with db() as s:
            await document(s)
    assert await sql(db, "SELECT count(*) FROM info_document") == 1


async def legacy_document(db, url):
    doc_id = uuid.uuid4()
    await sql(
        db,
        "INSERT INTO info_document(id,canonical_url,title,status,metadata_json) VALUES (:id,:url,'legacy','active','{}')",
        id=doc_id,
        url=url,
    )
    return doc_id


@pytest.mark.parametrize("invalid", [False, True])
async def test_migration_preflight_refuses_conflicts_atomically(db, invalid):
    await migrate(db, "downgrade")
    await legacy_document(db, "https://EXAMPLE.com:443/article#x")
    await legacy_document(
        db, "invalid-url" if invalid else "https://example.com/article"
    )
    report = await audit_database(db)
    assert report["read_only"] is True and report["ready"] is False
    assert report["invalid_urls"] == int(invalid)
    assert report["duplicate_groups"] == int(not invalid)
    with pytest.raises(RuntimeError, match="canonical_identity_preflight_failed"):
        await migrate(db, "upgrade")
    assert await sql(db, "SELECT count(*) FROM info_document") == 2
    assert (
        await sql(
            db,
            "SELECT count(*) FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='info_document' AND column_name='canonical_identity'",
        )
        == 0
    )


async def test_migration_roundtrip_preserves_rows_urls_versions_and_intents(db):
    async with db() as s:
        doc = await document(s, "https://EXAMPLE.com:443/article#original")
        version = await add_version(s, doc)
        doc_id, version_id = doc.id, version.id
        await s.commit()
    before = await sql(
        db,
        "SELECT to_jsonb(d)-'canonical_identity' FROM info_document d WHERE id=:id",
        id=doc_id,
    )
    await migrate(db, "downgrade")
    await legacy_document(db, None)
    report = await audit_database(db)
    assert (
        report["read_only"] and report["ready"] and report["normalization_changes"] == 1
    )
    await migrate(db, "upgrade")
    assert await sql(
        db, "SELECT canonical_identity FROM info_document WHERE id=:id", id=doc_id
    ) == identity_key_v1("https://example.com/article")
    assert (
        await sql(
            db,
            "SELECT to_jsonb(d)-'canonical_identity' FROM info_document d WHERE id=:id",
            id=doc_id,
        )
        == before
    )
    assert (
        await sql(
            db, "SELECT current_version_id FROM info_document WHERE id=:id", id=doc_id
        )
        == version_id
    )
    assert await sql(db, "SELECT count(*) FROM info_document_version") == 1
    assert await sql(db, "SELECT count(*) FROM outbox_message") == 1
    await migrate(db, "downgrade")
    await migrate(db, "upgrade")
    assert (await audit_database(db))["ready"] is True


async def test_new_writer_fails_closed_before_schema_upgrade(db):
    await migrate(db, "downgrade")
    with pytest.raises(DBAPIError):
        async with db() as s:
            await document(s)
    assert await sql(db, "SELECT count(*) FROM info_document") == 0


async def test_failure_after_backfill_rolls_back_ddl_and_preserves_legacy_rows(
    db, monkeypatch
):
    from app.domain import info_identity_v1

    await migrate(db, "downgrade")
    await legacy_document(db, "https://EXAMPLE.com:443/article#x")
    await legacy_document(db, "https://example.com/article")
    # Incorrect audit approval exercises the independent DB uniqueness guard
    # after ADD COLUMN/backfill, not just pre-DDL rejection.
    monkeypatch.setattr(
        info_identity_v1, "audit_identity_rows", lambda rows: {"ready": True}
    )
    with pytest.raises(IntegrityError, match="uq_info_document_canonical_identity"):
        await migrate(db, "upgrade")
    assert await sql(db, "SELECT count(*) FROM info_document") == 2
    assert (
        await sql(
            db,
            "SELECT count(*) FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='info_document' AND column_name='canonical_identity'",
        )
        == 0
    )


async def test_preflight_and_migration_cross_page_boundary_without_skipping(db):
    await migrate(db, "downgrade")
    await sql(
        db,
        "INSERT INTO info_document(id,canonical_url,title,status,metadata_json) "
        "SELECT gen_random_uuid(),'https://EXAMPLE.com:443/article/'||g||'#old','legacy','active','{}' "
        "FROM generate_series(1,503) g",
    )
    report = await audit_database(db)
    assert report["ready"] and report["rows"] == 503
    assert report["normalization_changes"] == 503
    await migrate(db, "upgrade")
    assert (
        await sql(db, "SELECT count(DISTINCT canonical_identity) FROM info_document")
        == 503
    )
    assert (
        await sql(
            db, "SELECT count(*) FROM info_document WHERE canonical_url LIKE '%#old'"
        )
        == 503
    )
