"""登记记录与三步任务链：真数据库（含本次迁移）、真的持久任务运行时。

采集 → 建数据集 → 向知识服务登记；前一步成功才排下一步，下一步和本步的完成记录
在同一个事务里提交。对象存储、取数、知识服务用替身。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_durable_delivery_db import db as db
from test_durable_delivery_db import sql
from test_security_dataset_db import MemoryStorage, Reports, clock, ingested
from test_security_dataset_db import service as dataset_service
from test_security_ingestion import FakeFetcher
from test_security_ingestion_db import build as ingestion_service

from app.application.securities.registration_service import (
    DatasetRegistrationService,
)
from app.application.services.durable_tasks import DurableTasks
from app.bootstrap import securities as wiring
from app.domain.securities import IngestionStatus, RawResponse, SecurityCode
from app.domain.securities.registration import RegistrationError
from app.infrastructure.messaging.delivery_handlers import get_delivery_handlers
from app.infrastructure.securities import SqlDatasetRecords
from app.infrastructure.storage import postgres as postgres_module

BUILD = "info.security.dataset.build.v1"
REGISTER = "info.security.dataset.register.v1"


def production_like(db):
    return async_sessionmaker(db.kw["bind"], autocommit=False, autoflush=False)


class Registrar:
    def __init__(self, *errors: RegistrationError, configured: bool = True) -> None:
        self.errors = list(errors)
        self.configured = configured
        self.calls = []

    async def register(self, dataset):
        self.calls.append(dataset)
        if self.errors:
            raise self.errors.pop(0)


def records(db) -> SqlDatasetRecords:
    return SqlDatasetRecords(session_factory=production_like(db), clock=clock)


@pytest.fixture
def world(db, monkeypatch):
    """把三步的服务都接到替身上，返回替身，供各测试检查。"""
    parts = SimpleNamespace(
        storage=MemoryStorage(),
        reader=Reports(),
        registrar=Registrar(),
        fetcher=FakeFetcher(),
    )
    monkeypatch.setattr(
        postgres_module,
        "get_postgres",
        lambda: SimpleNamespace(session_factory=production_like(db)),
    )
    monkeypatch.setattr(wiring, "get_object_storage", lambda: parts.storage)
    monkeypatch.setattr(
        wiring,
        "build_security_ingestion_service",
        lambda sessions: ingestion_service(db, parts.storage, parts.fetcher)[0],
    )
    monkeypatch.setattr(
        wiring,
        "build_security_dataset_service",
        lambda sessions: dataset_service(db, parts.storage, parts.reader),
    )
    monkeypatch.setattr(
        wiring,
        "build_dataset_registration_service",
        lambda sessions: DatasetRegistrationService(
            records=records(db), registrar=parts.registrar
        ),
    )
    return parts


def runtime(db) -> DurableTasks:
    return DurableTasks(db, handlers=get_delivery_handlers())


async def message(db, topic: str):
    return await sql(db, "SELECT id FROM outbox_message WHERE topic=:t", t=topic)


async def queued(db, topic: str) -> int:
    return await sql(db, "SELECT count(*) FROM outbox_message WHERE topic=:t", t=topic)


async def queue_build(db, ingestion_id: str):
    async with db() as s:
        await wiring.enqueue_dataset_build(s, ingestion_id)
        await s.commit()
    return await message(db, BUILD)


async def built(db, world, **kwargs) -> str:
    """建好一个数据集并排好登记任务，返回数据集记录的标识。"""
    ingestion_id = await ingested(db, world.storage, world.reader, **kwargs)
    assert await runtime(db).consume(await queue_build(db, ingestion_id))
    return str(await sql(db, "SELECT id FROM security_dataset"))


# ---------------------------------------------------------------- 登记记录


async def test_records_are_read_back_as_they_were_stored(db, world):
    record_id = await built(db, world)
    record = await records(db).get(record_id)
    assert record is not None and record.status == "published"
    assert record.security_code == "600009" and record.bucket == "test-bucket"
    assert record.object_key.endswith(
        f"/datasets/{record.data_version}/sh600009-financials.sqlite"
    )
    assert world.storage.objects[record.object_key].startswith(b"SQLite format 3")
    assert record.version_id == "v1" and record.registered_at is None
    assert record.registration_error is None
    assert await records(db).latest_published(SecurityCode("600009")) == record
    assert await records(db).latest_published(SecurityCode("600519")) is None


@pytest.mark.parametrize("record_id", ["not-a-uuid", "", None])
async def test_what_is_not_a_record_id_finds_nothing(db, record_id):
    assert await records(db).get(record_id) is None
    assert await records(db).get("7f3c0f1e-0000-4000-8000-000000000000") is None


async def test_a_blocked_dataset_is_not_the_latest_published(db, world):
    def damage(body: bytes) -> bytes:
        data = json.loads(body)
        data["data"][0]["TOTAL_LIABILITIES"] += 5000.0
        return json.dumps(data).encode()

    await built(db, world, damage=damage)
    assert await sql(db, "SELECT status FROM security_dataset") == "quality_failed"
    assert await records(db).latest_published(SecurityCode("600009")) is None


async def test_success_clears_the_last_failure_and_failure_keeps_the_last_success(
    db, world
):
    record_id = await built(db, world)
    target = records(db)
    await target.mark_registration_failed(record_id, "knowledge_unreachable")
    failed = await target.get(record_id)
    assert failed.registered_at is None
    assert failed.registration_error == "knowledge_unreachable"
    await target.mark_registered(record_id)
    done = await target.get(record_id)
    assert done.registered_at is not None and done.registration_error is None
    await target.mark_registration_failed(record_id, "x" * 200)
    again = await target.get(record_id)
    assert again.registered_at == done.registered_at
    assert again.registration_error == "x" * 80


# ---------------------------------------------------------------- 采集 → 建数据集


async def test_a_succeeded_ingestion_queues_the_dataset_build(db, world):
    sessions = production_like(db)
    async with sessions() as s:
        batch = await wiring.request_security_ingestion(s, sessions, "600009")
        ingestion_id = str(batch.id)
    assert await queued(db, BUILD) == 0
    assert await runtime(db).consume(await message(db, "info.security.ingest.v1"))
    assert await sql(db, "SELECT status FROM security_ingestion") == "succeeded"
    assert (
        await sql(
            db,
            "SELECT aggregate_key || '/' || (payload->>'ingestion_id') "
            "FROM outbox_message WHERE topic=:t",
            t=BUILD,
        )
        == f"{ingestion_id}/{ingestion_id}"
    )


async def test_a_failed_ingestion_queues_nothing(db, world):
    world.fetcher = FakeFetcher(
        {"lrbDateAjaxNew": RawResponse(404, "text/html", b"gone", "x")}
    )
    sessions = production_like(db)
    async with sessions() as s:
        await wiring.request_security_ingestion(s, sessions, "600009")
    assert await runtime(db).consume(await message(db, "info.security.ingest.v1"))
    assert await sql(db, "SELECT status FROM security_ingestion") == "failed"
    assert await queued(db, BUILD) == 0


async def test_a_redelivered_ingestion_does_not_queue_a_second_build(db, world):
    sessions = production_like(db)
    async with sessions() as s:
        await wiring.request_security_ingestion(s, sessions, "600009")
    message_id = await message(db, "info.security.ingest.v1")
    assert await runtime(db).consume(message_id)
    assert await runtime(db).consume(message_id) is False
    assert await queued(db, BUILD) == 1
    assert await sql(db, "SELECT count(*) FROM security_ingestion_item") == 21


# ---------------------------------------------------------------- 建数据集 → 登记


async def test_a_published_dataset_queues_its_registration(db, world):
    record_id = await built(db, world)
    assert await sql(db, "SELECT status FROM security_dataset") == "published"
    assert (
        await sql(
            db,
            "SELECT aggregate_key || '/' || (payload->>'record_id') "
            "FROM outbox_message WHERE topic=:t",
            t=REGISTER,
        )
        == f"{record_id}/{record_id}"
    )
    assert world.registrar.calls == []  # 登记是下一个任务的事


async def test_a_blocked_dataset_queues_nothing(db, world):
    def damage(body: bytes) -> bytes:
        data = json.loads(body)
        data["data"][0]["TOTAL_LIABILITIES"] += 5000.0
        return json.dumps(data).encode()

    await built(db, world, damage=damage)
    assert await sql(db, "SELECT status FROM security_dataset") == "quality_failed"
    assert await queued(db, REGISTER) == 0


async def test_without_a_configured_knowledge_service_nothing_is_queued(db, world):
    world.registrar.configured = False
    await built(db, world)
    assert await sql(db, "SELECT status FROM security_dataset") == "published"
    assert await queued(db, REGISTER) == 0


async def test_a_batch_that_cannot_be_built_is_not_retried(db, world):
    ingestion_id = await ingested(
        db, world.storage, world.reader, status=IngestionStatus.FAILED
    )
    message_id = await queue_build(db, ingestion_id)
    assert await runtime(db).consume(message_id)  # 任务完成，不进重试
    assert await sql(db, "SELECT count(*) FROM security_dataset") == 0
    assert await queued(db, REGISTER) == 0


async def test_a_storage_fault_while_building_is_retried(db, world):
    ingestion_id = await ingested(db, world.storage, world.reader)
    message_id = await queue_build(db, ingestion_id)
    key = next(k for k in world.storage.objects if k.endswith("cash_flow-2.json"))
    original = world.storage.objects[key]
    world.storage.objects[key] = original.replace(b"600009", b"600008")
    with pytest.raises(RuntimeError, match="stored_object_digest_mismatch"):
        await runtime(db).consume(message_id)
    assert await sql(db, "SELECT count(*) FROM inbox_message") == 0
    assert await queued(db, REGISTER) == 0
    world.storage.objects[key] = original
    assert await runtime(db).consume(message_id)
    assert await sql(db, "SELECT status FROM security_dataset") == "published"
    assert await queued(db, REGISTER) == 1


# ---------------------------------------------------------------- 登记


async def test_registration_marks_the_dataset(db, world):
    record_id = await built(db, world)
    assert await runtime(db).consume(await message(db, REGISTER))
    (sent,) = world.registrar.calls
    assert sent.record_id == record_id and sent.status == "published"
    assert sent.sha256 == await sql(db, "SELECT sha256 FROM security_dataset")
    assert (
        await sql(
            db,
            "SELECT knowledge_registered_at >= built_at "
            "AND knowledge_registration_error IS NULL FROM security_dataset",
        )
        is True
    )


async def test_an_outage_is_retried_until_the_knowledge_service_answers(db, world):
    world.registrar.errors = [
        RegistrationError("knowledge_unreachable", retryable=True),
        RegistrationError(
            "knowledge_request_failed", retryable=True, detail="HTTP 503"
        ),
    ]
    await built(db, world)
    message_id = await message(db, REGISTER)
    with pytest.raises(RuntimeError, match="knowledge_unreachable"):
        await runtime(db).consume(message_id)
    assert (
        await sql(db, "SELECT knowledge_registration_error FROM security_dataset")
        == "knowledge_unreachable"
    )
    with pytest.raises(RuntimeError, match="knowledge_request_failed"):
        await runtime(db).consume(message_id)
    assert (
        await sql(
            db, "SELECT count(*) FROM inbox_message WHERE consumer=:t", t=REGISTER
        )
        == 0
    )
    assert await runtime(db).consume(message_id)
    assert len(world.registrar.calls) == 3
    assert (
        await sql(
            db,
            "SELECT knowledge_registered_at IS NOT NULL "
            "AND knowledge_registration_error IS NULL FROM security_dataset",
        )
        is True
    )
    assert await runtime(db).consume(message_id) is False  # 已完成的不再执行
    assert len(world.registrar.calls) == 3


async def test_a_refusal_is_recorded_and_not_retried(db, world):
    world.registrar.errors = [
        RegistrationError("knowledge_version_conflict", retryable=False)
    ]
    await built(db, world)
    assert await runtime(db).consume(await message(db, REGISTER))
    assert (
        await sql(
            db,
            "SELECT knowledge_registration_error || '/' || "
            "(knowledge_registered_at IS NULL)::text FROM security_dataset",
        )
        == "knowledge_version_conflict/true"
    )
    assert len(world.registrar.calls) == 1


async def test_the_whole_chain_from_a_code_to_a_registered_dataset(db, world):
    """采集用的夹具建不出数据集，所以链条在第二步换成建库夹具的批次；
    三个任务各自的衔接在上面分别验过，这里验的是它们能按顺序一口气跑完。"""
    sessions = production_like(db)
    async with sessions() as s:
        await wiring.request_security_ingestion(s, sessions, "600009")
    assert await runtime(db).consume(await message(db, "info.security.ingest.v1"))
    assert await queued(db, BUILD) == 1
    record_id = await built_from_second_batch(db, world)
    assert await runtime(db).consume(await message(db, REGISTER))
    assert [c.record_id for c in world.registrar.calls] == [record_id]
    assert (
        await sql(
            db,
            "SELECT count(*) FROM security_dataset WHERE knowledge_registered_at "
            "IS NOT NULL",
        )
        == 1
    )


async def built_from_second_batch(db, world) -> str:
    ingestion_id = await ingested(db, world.storage, world.reader)
    async with db() as s:
        await wiring.enqueue_dataset_build(s, ingestion_id)
        await s.commit()
    message_id = await sql(
        db,
        "SELECT id FROM outbox_message WHERE topic=:t AND aggregate_key=:k",
        t=BUILD,
        k=ingestion_id,
    )
    assert await runtime(db).consume(message_id)
    return str(await sql(db, "SELECT id FROM security_dataset"))
