from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class SourceCreate(BaseModel):
    code: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=255)
    source_type: str = "website"
    base_url: str | None = None
    description: str | None = None


class SourceRead(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    source_type: str
    base_url: str | None
    status: str
    description: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CrawlJobCreate(BaseModel):
    target_url: HttpUrl
    source_id: uuid.UUID | None = None
    enqueue: bool = True


class CollectorCreate(BaseModel):
    code: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=255)
    collector_type: str = "rss"
    source_id: uuid.UUID | None = None
    config: dict = Field(default_factory=dict)


class CollectorRead(BaseModel):
    id: uuid.UUID
    source_id: uuid.UUID | None
    code: str
    name: str
    collector_type: str
    config: dict
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CollectorDiscoverRequest(BaseModel):
    url: HttpUrl | None = None


class CrawlJobRead(BaseModel):
    id: uuid.UUID
    source_id: uuid.UUID | None
    document_id: uuid.UUID | None
    document_version_id: uuid.UUID | None
    job_type: str
    target_url: str
    status: str
    http_status: int | None
    final_url: str | None
    error_code: str | None
    error_message: str | None
    attempt_count: int
    duration_ms: int | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class DocumentRead(BaseModel):
    id: uuid.UUID
    source_id: uuid.UUID | None
    canonical_url: str | None
    title: str
    source_name: str | None
    published_at: datetime | None
    status: str
    current_version_id: uuid.UUID | None
    content_hash: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DocumentReviewRequest(BaseModel):
    status: str = Field(min_length=1, max_length=30)
    reviewer: str | None = Field(default=None, max_length=120)
    reason: str | None = None


class RawArtifactRead(BaseModel):
    id: uuid.UUID
    crawl_job_id: uuid.UUID
    document_id: uuid.UUID | None
    document_version_id: uuid.UUID | None
    artifact_type: str
    bucket: str
    object_key: str
    version_id: str | None
    sha256: str
    size_bytes: int
    content_type: str
    storage_state: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DocumentVersionRead(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    version_no: int
    source_url: str
    title: str
    content_hash: str
    raw_artifact_id: uuid.UUID | None
    clean_artifact_id: uuid.UUID | None
    text_artifact_id: uuid.UUID | None
    extraction_status: str
    extractor_name: str | None
    extractor_version: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DocumentVersionReviewRequest(BaseModel):
    extraction_status: str = Field(min_length=1, max_length=30)
    reviewer: str | None = Field(default=None, max_length=120)
    reason: str | None = None


class DistributionCreate(BaseModel):
    document_version_id: uuid.UUID
    target_dataset: str | None = None
    dispatch: bool = False


class DistributionStatusUpdate(BaseModel):
    status: str = Field(min_length=1, max_length=30)
    last_error: str | None = None
    metadata: dict = Field(default_factory=dict)


class DistributionRead(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    document_version_id: uuid.UUID
    target_app: str
    target_dataset: str | None
    content_hash: str
    status: str
    payload: dict
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SearchIndexRebuildRead(BaseModel):
    enabled: bool
    index_name: str
    index_created: bool = False
    indexed: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = Field(default_factory=list)


class UploadIngestRead(BaseModel):
    document_version_id: uuid.UUID
    document_id: uuid.UUID
    extraction_status: str
    raw_artifact_id: uuid.UUID | None
    clean_artifact_id: uuid.UUID | None
    text_artifact_id: uuid.UUID | None
