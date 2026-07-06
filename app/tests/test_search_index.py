from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from app.infrastructure.search import (
    INFO_INFORMATION_INDEX_MAPPING,
    build_info_index_document,
)


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
