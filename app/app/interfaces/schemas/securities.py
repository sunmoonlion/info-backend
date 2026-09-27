from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class SecurityIngestionRead(BaseModel):
    id: uuid.UUID
    security_code: str
    market: str
    status: str
    sources: list[str]
    requested_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    summary: dict[str, Any]
    error_code: str | None
    error_detail: str | None

    model_config = ConfigDict(from_attributes=True)


class SecurityIngestionItemRead(BaseModel):
    seq: int
    source_code: str
    kind: str
    sha256: str
    size_bytes: int
    reused: bool
    http_status: int
    raw_artifact_id: uuid.UUID
    meta: dict[str, Any]

    model_config = ConfigDict(from_attributes=True)


class SecurityIngestionDetail(SecurityIngestionRead):
    items: list[SecurityIngestionItemRead]


class SecurityDatasetRead(BaseModel):
    id: uuid.UUID
    security_code: str
    dataset_id: str
    data_version: str
    status: str
    ingestion_id: uuid.UUID
    sha256: str
    size_bytes: int
    row_counts: dict[str, Any]
    start_date: str
    end_date: str
    built_at: datetime
    failed_checks: list[str]
    knowledge_registered_at: datetime | None
    knowledge_registration_error: str | None
