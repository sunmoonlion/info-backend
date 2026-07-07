from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import httpx

from core.config import get_settings

INFO_INFORMATION_INDEX_MAPPING: dict[str, Any] = {
    "mappings": {
        "dynamic": "false",
        "properties": {
            "document_id": {"type": "keyword"},
            "document_version_id": {"type": "keyword"},
            "source_id": {"type": "keyword"},
            "source_name": {"type": "keyword"},
            "canonical_url": {"type": "keyword"},
            "source_url": {"type": "keyword"},
            "title": {
                "type": "text",
                "fields": {"keyword": {"type": "keyword", "ignore_above": 256}},
            },
            "status": {"type": "keyword"},
            "extraction_status": {"type": "keyword"},
            "content_hash": {"type": "keyword"},
            "version_no": {"type": "integer"},
            "published_at": {"type": "date"},
            "created_at": {"type": "date"},
            "updated_at": {"type": "date"},
            "artifacts": {
                "type": "nested",
                "properties": {
                    "artifact_type": {"type": "keyword"},
                    "bucket": {"type": "keyword"},
                    "object_key": {"type": "keyword"},
                    "sha256": {"type": "keyword"},
                    "content_type": {"type": "keyword"},
                },
            },
            "extracted_contents": {
                "type": "nested",
                "properties": {
                    "content_format": {"type": "keyword"},
                    "bucket": {"type": "keyword"},
                    "object_key": {"type": "keyword"},
                    "sha256": {"type": "keyword"},
                    "extractor_name": {"type": "keyword"},
                },
            },
            "metadata": {"type": "object", "enabled": False},
        },
    }
}


def _iso(value: Any) -> str | None:
    return value.isoformat() if value else None


def build_info_index_document(
    *,
    document: Any,
    version: Any,
    artifacts: Iterable[Any],
    extracted_contents: Iterable[Any],
) -> dict[str, Any]:
    return {
        "document_id": str(document.id),
        "document_version_id": str(version.id),
        "source_id": str(document.source_id) if document.source_id else None,
        "source_name": document.source_name,
        "canonical_url": document.canonical_url,
        "source_url": version.source_url,
        "title": version.title or document.title,
        "status": document.status,
        "extraction_status": version.extraction_status,
        "content_hash": version.content_hash,
        "version_no": version.version_no,
        "published_at": _iso(document.published_at),
        "created_at": _iso(version.created_at),
        "updated_at": _iso(version.updated_at),
        "artifacts": [
            {
                "artifact_type": artifact.artifact_type,
                "bucket": artifact.bucket,
                "object_key": artifact.object_key,
                "sha256": artifact.sha256,
                "content_type": artifact.content_type,
            }
            for artifact in artifacts
        ],
        "extracted_contents": [
            {
                "content_format": item.content_format,
                "bucket": item.bucket,
                "object_key": item.object_key,
                "sha256": item.sha256,
                "extractor_name": item.extractor_name,
            }
            for item in extracted_contents
        ],
        "metadata": {
            "document": document.metadata_json or {},
            "version": version.metadata_json or {},
        },
    }


@dataclass(frozen=True)
class InfoSearchIndex:
    base_url: str | None
    index_name: str
    timeout_seconds: float
    enabled: bool
    username: str | None = None
    password: str | None = None
    ca_cert_path: str | None = None

    def _client_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"timeout": self.timeout_seconds}
        if self.username and self.password:
            kwargs["auth"] = (self.username, self.password)
        if self.ca_cert_path:
            kwargs["verify"] = self.ca_cert_path
        return kwargs

    async def ensure_index(self) -> bool:
        if not self.enabled or not self.base_url:
            return False
        async with httpx.AsyncClient(**self._client_kwargs()) as client:
            response = await client.head(f"{self.base_url.rstrip('/')}/{self.index_name}")
            if response.status_code == 404:
                create_response = await client.put(
                    f"{self.base_url.rstrip('/')}/{self.index_name}",
                    json=INFO_INFORMATION_INDEX_MAPPING,
                )
                create_response.raise_for_status()
                return True
            response.raise_for_status()
            return False

    async def index_document(self, *, document_id: str, payload: dict[str, Any]) -> bool:
        if not self.enabled or not self.base_url:
            return False
        async with httpx.AsyncClient(**self._client_kwargs()) as client:
            response = await client.put(
                f"{self.base_url.rstrip('/')}/{self.index_name}/_doc/{document_id}",
                json=payload,
            )
            response.raise_for_status()
            return True


def get_info_search_index() -> InfoSearchIndex:
    settings = get_settings()
    return InfoSearchIndex(
        base_url=settings.elasticsearch_url,
        index_name=settings.elasticsearch_write_target,
        timeout_seconds=settings.elasticsearch_timeout_seconds,
        enabled=settings.search_enabled,
        username=settings.elasticsearch_username,
        password=settings.elasticsearch_password,
        ca_cert_path=settings.elasticsearch_ca_cert_path,
    )
