from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from app.application.services.info_crawl_service import create_knowledge_distribution


class _CreateDistributionSession:
    def __init__(self, values: dict[tuple[type[Any], UUID], Any]):
        self.values = values
        self.events: list[str] = []

    async def get(self, model: type[Any], identifier: UUID) -> Any:
        return self.values[(model, identifier)]

    def add(self, _value: Any) -> None:
        self.events.append("add")

    async def flush(self) -> None:
        self.events.append("flush")

    async def commit(self) -> None:
        self.events.append("commit")

    async def refresh(self, _value: Any) -> None:
        self.events.append("refresh")


@pytest.mark.asyncio
async def test_requested_distribution_writes_outbox_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.application.services import info_crawl_service
    from app.infrastructure.models.info import (
        InfoDocument,
        InfoDocumentVersion,
        RawArtifact,
    )

    document_id = UUID(int=10)
    version_id = UUID(int=11)
    artifact_id = UUID(int=12)
    session = _CreateDistributionSession(
        {
            (InfoDocumentVersion, version_id): SimpleNamespace(
                id=version_id,
                document_id=document_id,
                content_hash="b" * 64,
                clean_artifact_id=artifact_id,
                text_artifact_id=None,
                title="Example",
                source_url="https://example.com/a",
            ),
            (InfoDocument, document_id): SimpleNamespace(
                id=document_id,
                source_name="Example",
                published_at=None,
                metadata_json={},
            ),
            (RawArtifact, artifact_id): SimpleNamespace(
                document_version_id=version_id,
                storage_state="available",
                version_id="s3-version-1",
                artifact_type="clean_markdown",
                size_bytes=10,
                sha256="a" * 64,
                content_type="text/markdown",
                bucket="bucket",
                object_key="info/original/a.md",
            ),
        }
    )

    async def fake_ensure(_session: Any, *, distribution_id: UUID) -> Any:
        assert distribution_id
        assert session.events == ["add", "flush"]
        session.events.append("outbox")
        return SimpleNamespace()

    monkeypatch.setattr(
        info_crawl_service, "ensure_distribution_dispatch_outbox", fake_ensure
    )
    await create_knowledge_distribution(
        cast(Any, session), document_version_id=version_id, dispatch=True
    )

    assert session.events == ["add", "flush", "outbox", "commit", "refresh"]


@pytest.mark.asyncio
async def test_create_only_distribution_does_not_silently_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.application.services import info_crawl_service
    from app.infrastructure.models.info import (
        InfoDocument,
        InfoDocumentVersion,
        RawArtifact,
    )

    document_id = UUID(int=20)
    version_id = UUID(int=21)
    artifact_id = UUID(int=22)
    session = _CreateDistributionSession(
        {
            (InfoDocumentVersion, version_id): SimpleNamespace(
                id=version_id,
                document_id=document_id,
                content_hash="b" * 64,
                clean_artifact_id=artifact_id,
                text_artifact_id=None,
                title="Example",
                source_url="https://example.com/a",
            ),
            (InfoDocument, document_id): SimpleNamespace(
                id=document_id,
                source_name="Example",
                published_at=None,
                metadata_json={},
            ),
            (RawArtifact, artifact_id): SimpleNamespace(
                document_version_id=version_id,
                storage_state="available",
                version_id="s3-version-1",
                artifact_type="clean_markdown",
                size_bytes=10,
                sha256="a" * 64,
                content_type="text/markdown",
                bucket="bucket",
                object_key="info/original/a.md",
            ),
        }
    )

    async def fail_if_called(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("create-only distribution must not enqueue")

    monkeypatch.setattr(
        info_crawl_service, "ensure_distribution_dispatch_outbox", fail_if_called
    )
    await create_knowledge_distribution(
        cast(Any, session), document_version_id=version_id, dispatch=False
    )

    assert session.events == ["add", "commit", "refresh"]
