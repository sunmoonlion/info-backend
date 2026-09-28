"""建数据集的留存与登记：真数据库（含本次迁移），对象存储用内存替身。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_durable_delivery_db import db as db
from test_durable_delivery_db import sql
from test_security_dataset import STATEMENTS, pages_of, payloads, reports

from app.application.securities.dataset_service import (
    PUBLISHED,
    QUALITY_FAILED,
    SecurityDatasetService,
)
from app.domain.securities import (
    FetchRequest,
    IngestionStatus,
    ItemKind,
    RawResponse,
    SecurityCode,
    SourceCode,
)
from app.domain.securities.dataset import DatasetBuildError
from app.infrastructure.securities import (
    SqlBatchReader,
    SqlDatasetStore,
    SqlIngestionStore,
)
from app.infrastructure.storage.object_storage import StoredObject

CODE = SecurityCode("600009")


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

    def put_json(self, *, object_key, payload):
        data = json.dumps(payload, ensure_ascii=False).encode()
        return self.put_bytes(
            object_key=object_key, data=data, content_type="application/json"
        )

    def get_bytes(
        self,
        *,
        object_key,
        version_id=None,
        expected_sha256=None,
        max_bytes=64 * 1024 * 1024,
    ):
        data = self.objects[object_key]
        if len(data) > max_bytes:
            raise RuntimeError("stored_object_too_large")
        if expected_sha256 and hashlib.sha256(data).hexdigest() != expected_sha256:
            raise RuntimeError("stored_object_digest_mismatch")
        return data


class Reports:
    def __init__(self) -> None:
        self.pages = {}

    def extract_pages(self, pdf: bytes, *, max_pages: int):
        return self.pages[pdf]


def clock() -> datetime:
    return datetime.now(UTC)


async def ingested(db, storage, reader: Reports, *, damage=None, status=None) -> str:
    """把夹具当作一个已经采完的批次登记进库。"""
    sessions = async_sessionmaker(db.kw["bind"], autocommit=False, autoflush=False)
    store = SqlIngestionStore(session_factory=sessions, storage=storage, clock=clock)
    ingestion_id = await store.start(CODE, sources=["cninfo", "eastmoney-f10"])
    assert await store.begin(ingestion_id) == CODE
    seq = 0
    for statement in STATEMENTS:
        for number, body in enumerate(payloads(statement), 1):
            if damage and statement == "balance_sheet" and number == 1:
                body = damage(body)
            seq += 1
            await store.archive(
                ingestion_id,
                code=CODE,
                seq=seq,
                request=FetchRequest(
                    source=SourceCode.EASTMONEY_F10,
                    kind=ItemKind.STATEMENT_DATA,
                    url="https://emweb.securities.eastmoney.com/x",
                    name=f"{statement}-{number}.json",
                    meta={"statement": statement},
                ),
                response=RawResponse(200, "application/json", body, "x"),
            )
    for report in reports():
        pdf = b"%PDF-" + report["sha256"].encode()
        reader.pages[pdf] = pages_of(report)
        seq += 1
        await store.archive(
            ingestion_id,
            code=CODE,
            seq=seq,
            request=FetchRequest(
                source=SourceCode.CNINFO,
                kind=ItemKind.REPORT_FILE,
                url="http://static.cninfo.com.cn/finalpage/x.PDF",
                name=f"annual-{report['meta']['fiscal_year']}.pdf",
                meta=report["meta"],
            ),
            response=RawResponse(200, "application/pdf", pdf, "x"),
        )
    await store.finish(
        ingestion_id, status=status or IngestionStatus.SUCCEEDED, summary={}
    )
    return ingestion_id


def service(db, storage, reader: Reports, **limits) -> SecurityDatasetService:
    sessions = async_sessionmaker(db.kw["bind"], autocommit=False, autoflush=False)
    return SecurityDatasetService(
        batches=SqlBatchReader(session_factory=sessions, storage=storage, **limits),
        reports=reader,
        store=SqlDatasetStore(session_factory=sessions, storage=storage, clock=clock),
        today=lambda: date(2026, 9, 27),
    )


async def test_dataset_is_archived_registered_and_traceable_to_its_batch(db):
    storage, reader = MemoryStorage(), Reports()
    ingestion_id = await ingested(db, storage, reader)
    summary = await service(db, storage, reader).build_latest("600009")
    assert summary.status == PUBLISHED and summary.ingestion_id == ingestion_id
    prefix = f"info/securities/code=600009/datasets/{summary.data_version}"
    file = storage.objects[f"{prefix}/sh600009-financials.sqlite"]
    assert hashlib.sha256(file).hexdigest() == summary.sha256
    assert file.startswith(b"SQLite format 3")
    manifest = json.loads(storage.objects[f"{prefix}/manifest.json"])
    assert manifest["sha256"] == summary.sha256
    assert manifest["source_ingestion_id"] == ingestion_id
    assert manifest["quality_passed"] is True and manifest["status"] == "published"
    quality = json.loads(storage.objects[f"{prefix}/quality.json"])
    assert quality["passed"] is True and len(quality["checks"]) == 15
    assert (
        await sql(
            db,
            "SELECT status || '/' || data_version || '/' || sha256 || '/' || "
            "ingestion_id::text FROM security_dataset",
        )
        == f"published/{summary.data_version}/{summary.sha256}/{ingestion_id}"
    )
    assert (
        await sql(
            db, "SELECT row_counts->>'official_key_figures' FROM security_dataset"
        )
        == "177"
    )
    assert await sql(db, "SELECT quality->>'passed' FROM security_dataset") == "true"
    assert "追溯调整后" in await sql(
        db, "SELECT metadata_json->>'restatement_note' FROM security_dataset"
    )


async def test_building_the_same_batch_again_adds_nothing(db):
    storage, reader = MemoryStorage(), Reports()
    await ingested(db, storage, reader)
    first = await service(db, storage, reader).build_latest("600009")
    puts = storage.puts
    second = await service(db, storage, reader).build_latest("600009")
    assert (first.record_id, first.data_version) == (
        second.record_id,
        second.data_version,
    )
    assert storage.puts == puts
    assert await sql(db, "SELECT count(*) FROM security_dataset") == 1


async def test_the_newest_succeeded_batch_is_used(db):
    storage, reader = MemoryStorage(), Reports()
    await ingested(db, storage, reader, status=IngestionStatus.FAILED)
    with pytest.raises(DatasetBuildError, match="no_succeeded_ingestion"):
        await service(db, storage, reader).build_latest("600009")
    older = await ingested(db, storage, reader)
    newer = await ingested(db, storage, reader)
    await ingested(db, storage, reader, status=IngestionStatus.FAILED)
    summary = await service(db, storage, reader).build_latest("600009")
    assert summary.ingestion_id == newer != older
    with pytest.raises(DatasetBuildError, match="no_succeeded_ingestion"):
        await service(db, storage, reader).build_latest("600519")


async def test_a_damaged_batch_is_registered_as_failed(db):
    def damage(body: bytes) -> bytes:
        data = json.loads(body)
        data["data"][0]["TOTAL_LIABILITIES"] += 5000.0
        return json.dumps(data).encode()

    storage, reader = MemoryStorage(), Reports()
    await ingested(db, storage, reader, damage=damage)
    summary = await service(db, storage, reader).build_latest("600009")
    assert summary.status == QUALITY_FAILED
    assert await sql(db, "SELECT status FROM security_dataset") == "quality_failed"
    assert (
        await sql(db, "SELECT quality->'failed_blocking'->>0 FROM security_dataset")
        == "Q-R01"
    )
    prefix = f"info/securities/code=600009/datasets/{summary.data_version}"
    assert json.loads(storage.objects[f"{prefix}/manifest.json"])["status"] == (
        "quality_failed"
    )


async def test_an_archive_that_no_longer_matches_its_checksum_is_refused(db):
    storage, reader = MemoryStorage(), Reports()
    await ingested(db, storage, reader)
    key = next(k for k in storage.objects if k.endswith("cash_flow-2.json"))
    storage.objects[key] = storage.objects[key].replace(b"600009", b"600008")
    with pytest.raises(RuntimeError, match="stored_object_digest_mismatch"):
        await service(db, storage, reader).build_latest("600009")
    assert await sql(db, "SELECT count(*) FROM security_dataset") == 0


async def test_how_large_an_archive_may_be_read_back_is_set_by_the_caller(db):
    """采得到就要读得回：读回的上限由组装处按采集的上限给，不是写死的。"""
    storage, reader = MemoryStorage(), Reports()
    await ingested(db, storage, reader)
    largest = max(len(v) for v in storage.objects.values())
    with pytest.raises(RuntimeError, match="stored_object_too_large"):
        await service(db, storage, reader, max_bytes=largest - 1).build_latest("600009")
    assert await sql(db, "SELECT count(*) FROM security_dataset") == 0
    summary = await service(db, storage, reader, max_bytes=largest).build_latest(
        "600009"
    )
    assert summary.status == "published"
    with pytest.raises(ValueError, match="max_bytes"):
        service(db, storage, reader, max_bytes=0)


@pytest.mark.parametrize("ingestion_id", ["not-a-uuid", ""])
async def test_unknown_batch_ids(db, ingestion_id):
    with pytest.raises(DatasetBuildError, match="ingestion_not_found"):
        await service(db, MemoryStorage(), Reports()).build(ingestion_id)


async def test_database_refuses_statuses_outside_the_two(db):
    storage, reader = MemoryStorage(), Reports()
    ingestion_id = await ingested(db, storage, reader)
    with pytest.raises(Exception, match="ck_security_dataset_status"):
        await sql(
            db,
            "INSERT INTO security_dataset (security_code, dataset_id, data_version,"
            " status, ingestion_id, bucket, object_key, sha256, size_bytes,"
            " start_date, end_date, built_at) VALUES ('600009','d','v','draft',"
            " CAST(:i AS uuid),'b','k',:s,1,'2020-01-01','2020-12-31',now())",
            i=ingestion_id,
            s="a" * 64,
        )
