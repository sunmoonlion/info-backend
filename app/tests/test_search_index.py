from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from app.application.services.info_crawl_service import index_document_version
from app.infrastructure.search import (
    INFO_INFORMATION_INDEX_MAPPING,
    build_info_index_document,
)
from core.config import get_settings


def test_info_index_mapping_declares_information_fields() -> None:
    properties = INFO_INFORMATION_INDEX_MAPPING["mappings"]["properties"]

    assert properties["document_id"]["type"] == "keyword"
    assert properties["title"]["type"] == "text"
    assert properties["artifacts"]["type"] == "nested"
    assert properties["extracted_contents"]["type"] == "nested"


def test_build_info_index_document_includes_rebuildable_artifact_refs() -> None:
    document = SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        source_id=UUID("00000000-0000-0000-0000-000000000002"),
        source_name="Example",
        canonical_url="https://example.com/news/1",
        title="Document Title",
        status="reviewed",
        published_at=datetime(2026, 7, 6, tzinfo=UTC),
        metadata_json={"topic": "market"},
    )
    version = SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000003"),
        source_url="https://example.com/news/1",
        title="Version Title",
        extraction_status="succeeded",
        content_hash="abc123",
        version_no=2,
        created_at=datetime(2026, 7, 6, 1, tzinfo=UTC),
        updated_at=datetime(2026, 7, 6, 2, tzinfo=UTC),
        metadata_json={"extractor": "trafilatura"},
    )
    artifact = SimpleNamespace(
        artifact_type="clean_markdown",
        bucket="info",
        object_key="clean.md",
        sha256="sha256-clean",
        content_type="text/markdown",
    )
    extracted = SimpleNamespace(
        content_format="markdown",
        bucket="info",
        object_key="clean.md",
        sha256="sha256-clean",
        extractor_name="trafilatura",
    )

    payload = build_info_index_document(
        document=document,
        version=version,
        artifacts=[artifact],
        extracted_contents=[extracted],
    )

    assert payload["document_id"] == str(document.id)
    assert payload["document_version_id"] == str(version.id)
    assert payload["title"] == "Version Title"
    assert payload["published_at"] == "2026-07-06T00:00:00+00:00"
    assert payload["artifacts"][0]["object_key"] == "clean.md"
    assert payload["extracted_contents"][0]["content_format"] == "markdown"
    assert payload["metadata"]["document"]["topic"] == "market"


@pytest.mark.asyncio
async def test_index_document_version_skips_when_search_disabled(monkeypatch) -> None:
    monkeypatch.setenv("SEARCH_BACKEND", "disabled")
    monkeypatch.delenv("ELASTICSEARCH_URL", raising=False)
    get_settings.cache_clear()

    class SessionShouldNotBeUsed:
        async def get(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("disabled search must not query the database")

    result = await index_document_version(
        cast(Any, SessionShouldNotBeUsed()),
        document_version_id=UUID("00000000-0000-0000-0000-000000000003"),
    )

    assert result["enabled"] is False
    assert result["indexed"] == 0
    assert result["skipped"] == 1
    assert result["failed"] == 0
    get_settings.cache_clear()
