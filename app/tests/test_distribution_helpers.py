import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import jsonschema
import pytest

from app.application.services.info_crawl_service import (
    ArtifactNotDistributableError,
    _artifact_contract_payload,
    _distribution_retry_payload,
    _distribution_status_payload,
    _knowledge_ingestion_payload,
    _select_distribution_artifact,
)
from app.infrastructure.external.knowledge_app import (
    KnowledgeAppClient,
    KnowledgeAppNotConfiguredError,
)


def _provider_contract_path() -> Path:
    configured = os.environ.get("KNOWLEDGE_ARTIFACT_CONTRACT_PATH")
    if configured:
        return Path(configured)
    return (
        Path(__file__).resolve().parents[4]
        / "knowledge-app/contracts/artifact/v1/info-knowledge-artifact.schema.json"
    )


def _consumer_contract_lock() -> dict[str, Any]:
    lock_path = Path(__file__).resolve().parents[3] / "contracts/knowledge-provider-lock.json"
    return json.loads(lock_path.read_text())


def test_distribution_status_payload_appends_history() -> None:
    payload = _distribution_status_payload(
        existing={"document_id": "doc-1"},
        status="failed",
        last_error="timeout",
        metadata={"remote_id": "remote-1"},
    )

    assert payload["document_id"] == "doc-1"
    assert len(payload["status_history"]) == 1
    assert payload["last_status_update"]["status"] == "failed"
    assert payload["last_status_update"]["last_error"] == "timeout"
    assert payload["last_status_update"]["metadata"]["remote_id"] == "remote-1"


def test_distribution_retry_payload_preserves_existing_history() -> None:
    payload = _distribution_retry_payload(
        {
            "retry_history": [
                {
                    "previous_error": "bad gateway",
                    "retried_at": "2026-07-06T00:00:00+00:00",
                }
            ]
        },
        previous_error="timeout",
    )

    assert len(payload["retry_history"]) == 2
    assert payload["retry_history"][0]["previous_error"] == "bad gateway"
    assert payload["last_retry"]["previous_error"] == "timeout"


def test_knowledge_ingestion_payload_excludes_internal_distribution_history() -> None:
    distribution_id = UUID("00000000-0000-0000-0000-000000000001")
    record = SimpleNamespace(
        id=distribution_id,
        target_dataset="default",
        payload={
            "contract_version": 1,
            "operation": "upsert",
            "distribution_id": str(distribution_id),
            "dataset_key": "default",
            "status_history": [{"status": "running"}],
            "last_status_update": {"status": "running"},
            "retry_history": [{"previous_error": "timeout"}],
            "last_retry": {"previous_error": "timeout"},
        },
    )

    payload = _knowledge_ingestion_payload(cast(Any, record))

    assert payload["contract_version"] == 1
    assert payload["operation"] == "upsert"
    assert payload["distribution_id"] == str(record.id)
    assert payload["dataset_key"] == "default"
    assert "document_id" not in payload
    assert "version_id" not in payload
    assert "source_url" not in payload
    assert "status_history" not in payload
    assert "retry_history" not in payload


def test_artifact_contract_payload_validates_against_provider_schema() -> None:
    distribution_id = UUID("00000000-0000-0000-0000-000000000001")
    document_id = UUID("00000000-0000-0000-0000-000000000002")
    version_id = UUID("00000000-0000-0000-0000-000000000003")
    payload = _artifact_contract_payload(
        distribution_id=distribution_id,
        document=cast(
            Any,
            SimpleNamespace(
                id=document_id,
                source_name="Example",
                published_at=None,
                metadata_json={},
            ),
        ),
        version=cast(
            Any,
            SimpleNamespace(
                id=version_id,
                title="Example title",
                source_url="https://example.com/news",
                content_hash="b" * 64,
            ),
        ),
        artifact=cast(
            Any,
            SimpleNamespace(
                artifact_type="clean_markdown",
                bucket="development-info-originals",
                object_key="info/original/doc/clean file.md",
                version_id="version-1",
                sha256="a" * 64,
                size_bytes=12,
                content_type="text/markdown; charset=utf-8",
            ),
        ),
        dataset_key="market-news",
    )

    schema_bytes = _provider_contract_path().read_bytes()
    contract_lock = _consumer_contract_lock()
    assert contract_lock["major_version"] == 1
    assert hashlib.sha256(schema_bytes).hexdigest() == contract_lock["sha256"]
    schema = json.loads(schema_bytes)
    jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.FormatChecker(),
    ).validate(payload)
    assert payload["artifact"]["uri"].endswith("clean%20file.md")
    assert payload["correlation_id"] == str(distribution_id)


@pytest.mark.asyncio
async def test_distribution_rejects_unversioned_artifact() -> None:
    version_id = UUID("00000000-0000-0000-0000-000000000003")
    artifact = SimpleNamespace(
        document_version_id=version_id,
        storage_state="available",
        version_id=None,
        artifact_type="clean_markdown",
        size_bytes=12,
        sha256="a" * 64,
        content_type="text/markdown",
    )

    class Session:
        async def get(self, model, artifact_id):
            return artifact

    with pytest.raises(ArtifactNotDistributableError, match="versioned S3"):
        await _select_distribution_artifact(
            cast(Any, Session()),
            cast(Any, SimpleNamespace(id=version_id, clean_artifact_id=UUID(int=4), text_artifact_id=None)),
        )


@pytest.mark.asyncio
async def test_knowledge_app_client_requires_ingest_url() -> None:
    client = KnowledgeAppClient(
        ingest_url=None,
        token_provider=None,
        timeout_seconds=1.0,
    )

    with pytest.raises(KnowledgeAppNotConfiguredError):
        await client.ingest_document({"source_document_id": "doc-1"})
