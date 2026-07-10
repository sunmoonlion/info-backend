from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from app.application.services.info_crawl_service import (
    _distribution_retry_payload,
    _distribution_status_payload,
    _knowledge_ingestion_payload,
)
from app.infrastructure.external.knowledge_app import (
    KnowledgeAppClient,
    KnowledgeAppNotConfiguredError,
)


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
    record = SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        target_dataset="default",
        payload={
            "source_document_id": "doc-1",
            "source_document_version_id": "version-1",
            "canonical_url": "https://example.com/news",
            "status_history": [{"status": "running"}],
            "last_status_update": {"status": "running"},
            "retry_history": [{"previous_error": "timeout"}],
            "last_retry": {"previous_error": "timeout"},
        },
    )

    payload = _knowledge_ingestion_payload(cast(Any, record))

    assert payload["source_document_id"] == "doc-1"
    assert payload["source_document_version_id"] == "version-1"
    assert payload["canonical_url"] == "https://example.com/news"
    assert payload["distribution_id"] == str(record.id)
    assert payload["target_dataset"] == "default"
    assert "document_id" not in payload
    assert "version_id" not in payload
    assert "source_url" not in payload
    assert "status_history" not in payload
    assert "retry_history" not in payload


@pytest.mark.asyncio
async def test_knowledge_app_client_requires_ingest_url() -> None:
    client = KnowledgeAppClient(
        ingest_url=None,
        api_key=None,
        timeout_seconds=1.0,
    )

    with pytest.raises(KnowledgeAppNotConfiguredError):
        await client.ingest_document({"source_document_id": "doc-1"})
