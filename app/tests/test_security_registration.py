"""向知识服务登记数据集（0008-info 段二 → 段三）。不访问网络，不需要数据库。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from app.application.securities.registration_service import (
    DatasetRegistrationService,
)
from app.domain.securities import InvalidSecurityCode
from app.domain.securities.registration import DatasetRecord, RegistrationError
from app.infrastructure.securities import KnowledgeDatasetRegistrar

URL = "http://knowledge.test/api/internal/v1/knowledge/datasets"
VERSION = "sh600009-financials-39a395bfa6f16b67"
SHA256 = "51900174" + "a" * 56
TOKEN = "service-token-value"  # noqa: S105

RECORD = DatasetRecord(
    record_id="7f3c0f1e-0000-4000-8000-000000000001",
    dataset_id="sh600009-financials",
    data_version=VERSION,
    security_code="600009",
    status="published",
    ingestion_id="7f3c0f1e-0000-4000-8000-000000000002",
    bucket="development-info-originals",
    object_key=f"info/securities/code=600009/datasets/{VERSION}/x.sqlite",
    version_id="v7",
    sha256=SHA256,
    size_bytes=143360,
    start_date="1994-12-31",
    end_date="2026-06-30",
    registered_at=None,
)


class Tokens:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def get_token(self) -> str:
        if self.fail:
            raise RuntimeError("client secret s3cr3t rejected by https://idp.internal")
        return TOKEN


def accepted(record: DatasetRecord = RECORD, **changes) -> dict:
    return {
        "dataset_id": record.dataset_id,
        "data_version": record.data_version,
        "sha256": record.sha256,
        "status": "active",
    } | changes


def registrar(handler, *, url: str | None = URL, tokens=None):
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return (
        KnowledgeDatasetRegistrar(
            url=url,
            tokens=Tokens() if tokens is None else tokens,
            timeout_seconds=5,
            transport=httpx.MockTransport(record),
        ),
        seen,
    )


# ---------------------------------------------------------------- 发出的请求


async def test_the_request_carries_exactly_what_the_knowledge_service_expects():
    target, seen = registrar(lambda r: httpx.Response(201, json=accepted()))
    await target.register(RECORD)
    (request,) = seen
    assert request.method == "POST" and str(request.url) == URL
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    # 与 knowledge-backend 的 DatasetRegister 逐项对应；它不收多余的字段
    assert json.loads(request.content) == {
        "dataset_id": "sh600009-financials",
        "data_version": VERSION,
        "title": "600009 财务报表",
        "security_code": "600009",
        "object": f"s3://development-info-originals/{RECORD.object_key}",
        "object_version_id": "v7",
        "sha256": SHA256,
        "size_bytes": 143360,
        "start_date": "1994-12-31",
        "end_date": "2026-06-30",
        "source_app": "info",
        "source_ref": RECORD.ingestion_id,
        "quality_passed": True,
    }


async def test_a_repeated_registration_answered_with_200_is_also_success():
    target, _ = registrar(lambda r: httpx.Response(200, json=accepted()))
    await target.register(RECORD)


# ---------------------------------------------------------------- 失败的分类


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, "knowledge_rejected_identity", False),
        (403, "knowledge_rejected_identity", False),
        (404, "knowledge_registry_disabled", False),
        (409, "knowledge_version_conflict", False),
        (422, "knowledge_refused_registration", False),
        (400, "knowledge_request_failed", False),
        (429, "knowledge_request_failed", True),
        (500, "knowledge_request_failed", True),
        (502, "knowledge_request_failed", True),
        (503, "knowledge_request_failed", True),
        (504, "knowledge_request_failed", True),
    ],
)
async def test_refusals_and_outages_are_told_apart(status, code, retryable):
    target, _ = registrar(
        lambda r: httpx.Response(status, text="internal detail: db=10.0.0.5")
    )
    with pytest.raises(RegistrationError) as caught:
        await target.register(RECORD)
    error = caught.value
    assert (error.code, error.retryable) == (code, retryable)
    assert error.detail == f"HTTP {status}"
    assert "10.0.0.5" not in str(error) and TOKEN not in str(error)


async def test_a_network_failure_can_be_retried_and_says_nothing_about_the_address():
    def handler(request):
        raise httpx.ConnectError("cannot reach knowledge.test:80", request=request)

    target, _ = registrar(handler)
    with pytest.raises(RegistrationError) as caught:
        await target.register(RECORD)
    assert caught.value.code == "knowledge_unreachable" and caught.value.retryable
    assert "knowledge.test" not in str(caught.value)


async def test_a_token_failure_can_be_retried_and_does_not_leak_the_reason():
    target, seen = registrar(
        lambda r: httpx.Response(201, json=accepted()), tokens=Tokens(fail=True)
    )
    with pytest.raises(RegistrationError) as caught:
        await target.register(RECORD)
    assert caught.value.code == "service_token_unavailable" and caught.value.retryable
    assert "s3cr3t" not in str(caught.value) and "idp" not in str(caught.value)
    assert seen == []
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


@pytest.mark.parametrize(
    "reply",
    [
        accepted(status="superseded"),
        accepted(data_version="sh600009-financials-0000000000000000"),
        accepted(sha256="b" * 64),
        ["not", "an", "object"],
        "plain text",
    ],
)
async def test_a_reply_about_something_else_is_not_taken_as_success(reply):
    def handler(request):
        if isinstance(reply, str):
            return httpx.Response(201, text=reply)
        return httpx.Response(201, json=reply)

    target, _ = registrar(handler)
    with pytest.raises(RegistrationError) as caught:
        await target.register(RECORD)
    assert caught.value.code == "knowledge_reply_unexpected"
    assert not caught.value.retryable


@pytest.mark.parametrize(
    ("url", "tokens"), [(None, Tokens()), (URL, None), ("", Tokens()), (None, None)]
)
async def test_without_address_or_identity_nothing_is_sent(url, tokens):
    seen: list[httpx.Request] = []
    target = KnowledgeDatasetRegistrar(
        url=url,
        tokens=tokens,
        timeout_seconds=5,
        transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(201)),
    )
    assert target.configured is False
    with pytest.raises(RegistrationError) as caught:
        await target.register(RECORD)
    assert caught.value.code == "registrar_not_configured" and seen == []


# ---------------------------------------------------------------- 应用服务


class Records:
    def __init__(self, *records: DatasetRecord) -> None:
        self.records = {r.record_id: r for r in records}
        self.registered: list[str] = []
        self.failed: list[tuple[str, str]] = []

    async def get(self, record_id):
        return self.records.get(record_id)

    async def latest_published(self, code):
        found = [
            r
            for r in self.records.values()
            if r.security_code == code.code and r.status == "published"
        ]
        return found[-1] if found else None

    async def mark_registered(self, record_id):
        self.registered.append(record_id)

    async def mark_registration_failed(self, record_id, code):
        self.failed.append((record_id, code))


class Registrar:
    def __init__(self, *, configured=True, error: RegistrationError | None = None):
        self.configured = configured
        self.error = error
        self.calls: list[DatasetRecord] = []

    async def register(self, dataset):
        self.calls.append(dataset)
        if self.error:
            raise self.error


def service(records: Records, target: Registrar) -> DatasetRegistrationService:
    return DatasetRegistrationService(records=records, registrar=target)


async def test_a_published_dataset_is_registered_and_marked():
    records, target = Records(RECORD), Registrar()
    summary = await service(records, target).register(RECORD.record_id)
    assert summary.registered and summary.data_version == VERSION
    assert summary.sha256 == SHA256 and summary.security_code == "600009"
    assert target.calls == [RECORD] and records.registered == [RECORD.record_id]
    assert records.failed == []


async def test_registering_again_asks_the_knowledge_service_again():
    # 知识服务那边是幂等的；重新登记一个旧版本就是回退，所以这里不自作主张跳过
    done = replace(RECORD, registered_at=datetime(2026, 9, 27, tzinfo=UTC))
    records, target = Records(done), Registrar()
    await service(records, target).register(done.record_id)
    assert len(target.calls) == 1 and records.registered == [done.record_id]


async def test_a_dataset_that_failed_quality_checks_is_never_registered():
    blocked = replace(RECORD, status="quality_failed")
    records, target = Records(blocked), Registrar()
    with pytest.raises(RegistrationError) as caught:
        await service(records, target).register(blocked.record_id)
    assert caught.value.code == "dataset_not_published"
    assert not caught.value.retryable
    assert target.calls == [] and records.registered == [] and records.failed == []


async def test_the_latest_published_version_of_a_code_is_used():
    older = replace(RECORD, record_id="r-1", data_version=VERSION + "-old")
    blocked = replace(RECORD, record_id="r-3", status="quality_failed")
    newer = replace(RECORD, record_id="r-2")
    records, target = Records(older, newer, blocked), Registrar()
    summary = await service(records, target).register_latest("600009")
    assert summary.record_id == "r-2" and target.calls == [newer]


async def test_nothing_to_register():
    records, target = Records(), Registrar()
    with pytest.raises(RegistrationError, match="no_published_dataset"):
        await service(records, target).register_latest("600009")
    with pytest.raises(RegistrationError, match="dataset_not_found"):
        await service(records, target).register("missing")
    with pytest.raises(InvalidSecurityCode):
        await service(records, target).register_latest("6000")
    assert target.calls == []


async def test_a_failed_registration_is_recorded_and_not_marked_as_done():
    error = RegistrationError(
        "knowledge_version_conflict", retryable=False, detail="HTTP 409"
    )
    records, target = Records(RECORD), Registrar(error=error)
    with pytest.raises(RegistrationError) as caught:
        await service(records, target).register(RECORD.record_id)
    assert caught.value is error
    assert records.registered == []
    assert records.failed == [(RECORD.record_id, "knowledge_version_conflict")]


async def test_an_unconfigured_registrar_is_reported_without_calling_it():
    records, target = Records(RECORD), Registrar(configured=False)
    target_service = service(records, target)
    assert target_service.configured is False
    with pytest.raises(RegistrationError, match="registrar_not_configured"):
        await target_service.register(RECORD.record_id)
    assert target.calls == [] and records.failed == []
