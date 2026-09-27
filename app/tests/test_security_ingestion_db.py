"""证券采集的留存与登记：真数据库（含本次迁移），对象存储用内存替身。"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_durable_delivery_db import db as db
from test_durable_delivery_db import sql
from test_security_ingestion import FakeFetcher

from app.application.securities.collectors import (
    CninfoDisclosureCollector,
    EastmoneyF10StatementCollector,
)
from app.application.securities.ingestion_service import SecurityIngestionService
from app.bootstrap import securities as wiring
from app.domain.securities import (
    IngestionNotRunnable,
    IngestionStatus,
    InvalidSecurityCode,
    RawResponse,
    SecurityCode,
)
from app.infrastructure.securities import SqlIngestionStore, security_object_key
from app.infrastructure.storage.object_storage import StoredObject


class MemoryStorage:
    bucket = "test-bucket"

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts = 0

    def put_bytes(self, *, object_key, data, content_type, metadata=None):
        self.puts += 1
        self.objects[object_key] = data
        return StoredObject(
            self.bucket,
            object_key,
            "v1",
            hashlib.sha256(data).hexdigest(),
            len(data),
            content_type,
        )


def clock() -> datetime:
    return datetime.now(UTC)


def production_like(db):
    """生产的会话在提交后让对象过期；夹具的会话不会。用和生产一样的配置来测。"""
    return async_sessionmaker(db.kw["bind"], autocommit=False, autoflush=False)


def build(db, storage, fetcher=None):
    store = SqlIngestionStore(
        session_factory=production_like(db), storage=storage, clock=clock
    )

    async def pause() -> None:
        return None

    service = SecurityIngestionService(
        fetcher=fetcher or FakeFetcher(),
        store=store,
        collectors=[
            CninfoDisclosureCollector(today=date(2026, 9, 27)),
            EastmoneyF10StatementCollector(),
        ],
        clock=clock,
        pause=pause,
        backoff=lambda attempt: pause(),
    )
    return service, store


async def test_batch_rows_and_archived_objects_agree(db):
    storage = MemoryStorage()
    service, _ = build(db, storage)
    summary = await service.ingest("600009")
    assert summary.status is IngestionStatus.SUCCEEDED and summary.items == 21

    assert await sql(db, "SELECT status FROM security_ingestion") == "succeeded"
    assert await sql(db, "SELECT market FROM security_ingestion") == "SH"
    assert await sql(db, "SELECT count(*) FROM security_ingestion_item") == 21
    assert await sql(db, "SELECT count(*) FROM raw_artifact") == 21
    assert (
        await sql(db, "SELECT count(*) FROM crawl_job WHERE job_type='security'") == 21
    )
    assert (
        await sql(
            db,
            "SELECT started_at <= finished_at AND requested_at <= started_at "
            "FROM security_ingestion",
        )
        is True
    )
    assert (
        await sql(db, "SELECT summary->>'new_artifacts' FROM security_ingestion")
        == "21"
    )
    # 每条登记的校验值都能从留存的对象复算出来（MVP-01）
    async with db() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT i.sha256, i.size_bytes, a.object_key, a.sha256 "
                    "FROM security_ingestion_item i "
                    "JOIN raw_artifact a ON a.id=i.raw_artifact_id ORDER BY i.seq"
                )
            )
        ).all()
    assert len(rows) == 21
    for item_sha, size, key, artifact_sha in rows:
        data = storage.objects[key]
        assert hashlib.sha256(data).hexdigest() == item_sha == artifact_sha
        assert len(data) == size and item_sha in key


async def test_sources_are_registered_with_their_real_copyright_status(db):
    service, _ = build(db, MemoryStorage())
    await service.ingest("600009")
    assert (
        await sql(
            db,
            "SELECT trust_level || '/' || copyright_status FROM info_source "
            "WHERE code='cninfo'",
        )
        == "official/public_disclosure"
    )
    assert (
        await sql(
            db,
            "SELECT trust_level || '/' || copyright_status FROM info_source "
            "WHERE code='eastmoney-f10'",
        )
        == "third_party/unconfirmed_internal_only"
    )
    assert await sql(db, "SELECT count(*) FROM info_source") == 2


async def test_second_batch_references_existing_objects(db):
    storage = MemoryStorage()
    first = await build(db, storage)[0].ingest("600009")
    second = await build(db, storage)[0].ingest("600009")
    assert first.ingestion_id != second.ingestion_id
    assert (second.new_artifacts, second.reused_artifacts) == (0, 21)
    assert storage.puts == 21 and len(storage.objects) == 21  # MVP-02
    assert await sql(db, "SELECT count(*) FROM raw_artifact") == 21
    assert await sql(db, "SELECT count(*) FROM security_ingestion") == 2
    assert await sql(db, "SELECT count(*) FROM security_ingestion_item") == 42
    assert await sql(db, "SELECT count(*) FROM crawl_job") == 42  # 请求记录每次都留
    assert (
        await sql(
            db,
            "SELECT count(*) FROM security_ingestion_item WHERE reused "
            "AND ingestion_id=:id",
            id=second.ingestion_id,
        )
        == 21
    )
    assert await sql(db, "SELECT count(*) FROM info_source") == 2


async def test_changed_content_becomes_a_new_object(db):
    storage = MemoryStorage()
    await build(db, storage)[0].ingest("600009")
    changed = FakeFetcher(
        {
            "xjllbDateAjaxNew": RawResponse(
                200,
                "application/json",
                b'{"data":[{"REPORT_DATE":"2026-09-30 00:00:00"}]}',
                "x",
            )
        }
    )
    summary = await build(db, storage, changed)[0].ingest("600009")
    assert summary.status is IngestionStatus.SUCCEEDED
    # 现金流量表的报告期列表变了，随后那一批数据的请求与内容也跟着变
    # 11 个巨潮请求、1 个类型页、两张表各 3 个都没变；现金流量表 2 个是新的
    assert summary.items == 20
    assert (summary.new_artifacts, summary.reused_artifacts) == (2, 18)
    assert storage.puts == 23


async def test_failed_response_is_archived_and_the_batch_fails(db):
    storage = MemoryStorage()
    fetcher = FakeFetcher(
        {"lrbDateAjaxNew": RawResponse(503, "text/html", b"busy", "x")}
    )
    summary = await build(db, storage, fetcher)[0].ingest("600009")
    assert summary.status is IngestionStatus.FAILED
    assert (
        await sql(
            db,
            "SELECT status || '/' || error_code || '/' || error_detail "
            "FROM security_ingestion",
        )
        == "failed/http_status/503"
    )
    assert (
        await sql(
            db,
            "SELECT j.status || '/' || j.error_code FROM security_ingestion_item i "
            "JOIN crawl_job j ON j.id=i.crawl_job_id WHERE i.http_status=503",
        )
        == "failed/http_status_503"
    )
    assert b"busy" in storage.objects.values()


async def test_redelivery_of_a_running_batch_marks_it_interrupted(db):
    service, store = build(db, MemoryStorage())
    ingestion_id = await service.request("600009")
    assert await sql(db, "SELECT status FROM security_ingestion") == "pending"
    assert await store.begin(ingestion_id) == SecurityCode("600009")
    assert await sql(db, "SELECT status FROM security_ingestion") == "running"
    with pytest.raises(IngestionNotRunnable):
        await service.run(ingestion_id)
    assert (
        await sql(db, "SELECT status || '/' || error_code FROM security_ingestion")
        == "failed/interrupted"
    )
    with pytest.raises(IngestionNotRunnable):  # 已结束的批次不会被重新打开
        await service.run(ingestion_id)
    assert await sql(db, "SELECT count(*) FROM security_ingestion_item") == 0


@pytest.mark.parametrize("ingestion_id", ["not-a-uuid", "", "0" * 32 + "-"])
async def test_begin_ignores_what_is_not_a_batch_id(db, ingestion_id):
    _, store = build(db, MemoryStorage())
    assert await store.begin(ingestion_id) is None


async def test_begin_returns_none_for_an_unknown_batch(db):
    _, store = build(db, MemoryStorage())
    assert await store.begin("7f3c0f1e-0000-4000-8000-000000000000") is None


async def test_request_commits_the_batch_and_the_task_together(db, monkeypatch):
    monkeypatch.setattr(wiring, "get_object_storage", MemoryStorage)
    sessions = production_like(db)
    async with sessions() as s:
        batch = await wiring.request_security_ingestion(s, sessions, "600009")
        assert batch.status == "pending" and batch.security_code == "600009"
    assert await sql(
        db,
        "SELECT aggregate_key FROM outbox_message "
        "WHERE topic='info.security.ingest.v1'",
    ) == str(batch.id)
    assert await sql(db, "SELECT count(*) FROM security_ingestion") == 1


async def test_request_leaves_nothing_behind_when_the_task_cannot_be_queued(
    db, monkeypatch
):
    async def fail(*args, **kwargs):
        raise RuntimeError("outbox unavailable")

    monkeypatch.setattr(wiring, "get_object_storage", MemoryStorage)
    monkeypatch.setattr(wiring, "enqueue_task", fail)
    with pytest.raises(RuntimeError):
        async with db() as s:
            await wiring.request_security_ingestion(s, db, "600009")
    assert await sql(db, "SELECT count(*) FROM security_ingestion") == 0
    assert await sql(db, "SELECT count(*) FROM outbox_message") == 0


async def test_request_refuses_an_invalid_code_before_touching_the_database(
    db, monkeypatch
):
    monkeypatch.setattr(wiring, "get_object_storage", MemoryStorage)
    with pytest.raises(InvalidSecurityCode):
        async with db() as s:
            await wiring.request_security_ingestion(s, db, "900901")
    assert await sql(db, "SELECT count(*) FROM security_ingestion") == 0
    assert await sql(db, "SELECT count(*) FROM info_source") == 0


def test_object_key_is_decided_by_content_and_cannot_escape_its_prefix():
    sha = "ab" * 32
    key = security_object_key(
        code="600009", source="cninfo", sha256=sha, name="../../etc/passwd"
    )
    assert key == (
        f"info/securities/code=600009/source=cninfo/sha256=ab/{sha}/..-..-etc-passwd"
    )
    assert "/../" not in key and key.count("/") == 6
