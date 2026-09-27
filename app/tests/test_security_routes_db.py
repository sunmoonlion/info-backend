"""证券采集与数据集的管理接口：真数据库；鉴权在路由挂载处加，另有测试。"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_durable_delivery_db import db as db
from test_durable_delivery_db import sql
from test_security_dataset_db import MemoryStorage, Reports, clock, ingested
from test_security_dataset_db import service as dataset_service
from test_security_registration_db import Registrar

from app.application.securities.registration_service import (
    DatasetRegistrationService,
)
from app.bootstrap import securities as wiring
from app.domain.securities.registration import RegistrationError
from app.infrastructure.securities import SqlDatasetRecords
from app.infrastructure.storage.postgres import get_db_session
from app.interfaces.http.admin import securities as routes


@pytest.fixture
async def api(db, monkeypatch):
    sessions = async_sessionmaker(db.kw["bind"], autocommit=False, autoflush=False)
    parts = SimpleNamespace(
        storage=MemoryStorage(), reader=Reports(), registrar=Registrar(), db=db
    )

    async def session():
        async with sessions() as s:
            yield s

    monkeypatch.setattr(wiring, "get_object_storage", lambda: parts.storage)
    monkeypatch.setattr(
        routes, "get_postgres", lambda: SimpleNamespace(session_factory=sessions)
    )
    monkeypatch.setattr(
        routes,
        "build_dataset_registration_service",
        lambda factory: DatasetRegistrationService(
            records=SqlDatasetRecords(session_factory=factory, clock=clock),
            registrar=parts.registrar,
        ),
    )
    app = FastAPI()
    app.include_router(routes.router, prefix="/api")
    app.dependency_overrides[get_db_session] = session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://info"
    ) as client:
        parts.client = client
        yield parts


async def dataset(api, **kwargs) -> str:
    await ingested(api.db, api.storage, api.reader, **kwargs)
    summary = await dataset_service(api.db, api.storage, api.reader).build_latest(
        "600009"
    )
    return summary.record_id


async def test_requesting_an_ingestion_registers_the_batch_and_queues_it(api):
    response = await api.client.post("/api/admin/securities/600009/ingestions")
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending" and body["security_code"] == "600009"
    assert body["market"] == "SH" and body["sources"] == ["cninfo", "eastmoney-f10"]
    assert (
        await sql(
            api.db,
            "SELECT aggregate_key FROM outbox_message "
            "WHERE topic='info.security.ingest.v1'",
        )
        == body["id"]
    )
    listed = await api.client.get("/api/admin/securities/600009/ingestions")
    assert [b["id"] for b in listed.json()] == [body["id"]]
    detail = await api.client.get(f"/api/admin/security-ingestions/{body['id']}")
    assert detail.status_code == 200 and detail.json()["items"] == []


@pytest.mark.parametrize("code", ["60000", "abc123", "900901", "6000090"])
async def test_codes_that_are_not_a_share_codes_are_refused(api, code):
    for method, path in [
        ("POST", f"/api/admin/securities/{code}/ingestions"),
        ("GET", f"/api/admin/securities/{code}/ingestions"),
        ("GET", f"/api/admin/securities/{code}/datasets"),
    ]:
        response = await api.client.request(method, path)
        assert response.status_code == 422, (method, path)
    assert await sql(api.db, "SELECT count(*) FROM security_ingestion") == 0
    assert await sql(api.db, "SELECT count(*) FROM outbox_message") == 0


async def test_unknown_batches_and_datasets_are_not_found(api):
    unknown = uuid.uuid4()
    response = await api.client.get(f"/api/admin/security-ingestions/{unknown}")
    assert response.status_code == 404
    response = await api.client.post(
        f"/api/admin/security-datasets/{unknown}/registration"
    )
    assert response.status_code == 404
    response = await api.client.post(
        "/api/admin/security-datasets/not-a-uuid/registration"
    )
    assert response.status_code == 422
    assert api.registrar.calls == []


async def test_datasets_are_listed_with_quality_and_registration_outcome(api):
    record_id = await dataset(api)
    response = await api.client.get("/api/admin/securities/600009/datasets")
    assert response.status_code == 200
    (row,) = response.json()
    assert row["id"] == record_id and row["status"] == "published"
    assert row["failed_checks"] == [] and row["row_counts"]["cash_flow"] > 0
    assert row["knowledge_registered_at"] is None
    assert row["knowledge_registration_error"] is None
    assert "object_key" not in row and "bucket" not in row
    assert (await api.client.get("/api/admin/securities/600519/datasets")).json() == []


async def test_a_blocked_dataset_shows_which_checks_failed_and_cannot_be_registered(
    api,
):
    import json

    def damage(body: bytes) -> bytes:
        data = json.loads(body)
        data["data"][0]["TOTAL_LIABILITIES"] += 5000.0
        return json.dumps(data).encode()

    record_id = await dataset(api, damage=damage)
    (row,) = (await api.client.get("/api/admin/securities/600009/datasets")).json()
    assert row["status"] == "quality_failed" and row["failed_checks"] == [
        "Q-R01",  # 资产 = 负债 + 所有者权益
        "Q-R05",  # 负债合计 = 流动负债 + 非流动负债
    ]
    response = await api.client.post(
        f"/api/admin/security-datasets/{record_id}/registration"
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "dataset_not_published"
    assert api.registrar.calls == []


async def test_registering_by_hand(api):
    record_id = await dataset(api)
    response = await api.client.post(
        f"/api/admin/security-datasets/{record_id}/registration"
    )
    assert response.status_code == 200
    assert response.json()["knowledge_registered_at"] is not None
    assert response.json()["knowledge_registration_error"] is None
    assert [c.record_id for c in api.registrar.calls] == [record_id]


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (RegistrationError("knowledge_unreachable", retryable=True), 503),
        (RegistrationError("knowledge_version_conflict", retryable=False), 409),
        (
            RegistrationError(
                "knowledge_request_failed", retryable=True, detail="HTTP 502"
            ),
            503,
        ),
    ],
)
async def test_a_failed_registration_reports_the_code_only(api, error, status):
    record_id = await dataset(api)
    api.registrar.errors = [error]
    response = await api.client.post(
        f"/api/admin/security-datasets/{record_id}/registration"
    )
    assert response.status_code == status
    assert response.json() == {"detail": error.code}
    (row,) = (await api.client.get("/api/admin/securities/600009/datasets")).json()
    assert row["knowledge_registration_error"] == error.code
    assert row["knowledge_registered_at"] is None


async def test_without_a_configured_knowledge_service(api):
    record_id = await dataset(api)
    api.registrar.configured = False
    response = await api.client.post(
        f"/api/admin/security-datasets/{record_id}/registration"
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "registrar_not_configured"
