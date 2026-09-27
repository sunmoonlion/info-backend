"""证券采集批次（0008-info 段一）。

一个批次由若干请求组成。每个请求对应一条 crawl_job（请求记录）；响应原文对应一条
raw_artifact。内容与已有原文相同时不新增 raw_artifact，批次条目引用已有的那一条。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.models.base import Base, TimestampMixin, UUIDMixin


class SecurityIngestion(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "security_ingestion"

    security_code: Mapped[str] = mapped_column(String(6), nullable=False)
    market: Mapped[str] = mapped_column(String(2), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    sources: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(120))
    error_detail: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name="ck_security_ingestion_status",
        ),
        CheckConstraint(
            "security_code ~ '^[0-9]{6}$'", name="ck_security_ingestion_code"
        ),
        Index("ix_security_ingestion_code_requested", "security_code", "requested_at"),
    )


class SecurityIngestionItem(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "security_ingestion_item"

    ingestion_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("security_ingestion.id", ondelete="CASCADE"),
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    source_code: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    crawl_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("crawl_job.id"), nullable=False
    )
    raw_artifact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("raw_artifact.id"), nullable=False
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    http_status: Mapped[int] = mapped_column(Integer, nullable=False)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("ingestion_id", "seq", name="uq_security_ingestion_item_seq"),
        CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'", name="ck_security_ingestion_item_sha256"
        ),
        Index("ix_security_ingestion_item_sha256", "sha256"),
    )


class SecurityDataset(UUIDMixin, TimestampMixin, Base):
    """从一个采集批次建出的数据集（0008-info 段二）。版本由内容决定。"""

    __tablename__ = "security_dataset"

    security_code: Mapped[str] = mapped_column(String(6), nullable=False)
    dataset_id: Mapped[str] = mapped_column(String(120), nullable=False)
    data_version: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    ingestion_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("security_ingestion.id"), nullable=False
    )
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    version_id: Mapped[str | None] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    row_counts: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    quality: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    start_date: Mapped[str] = mapped_column(String(10), nullable=False)
    end_date: Mapped[str] = mapped_column(String(10), nullable=False)
    built_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    knowledge_registered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    knowledge_registration_error: Mapped[str | None] = mapped_column(String(80))

    __table_args__ = (
        UniqueConstraint(
            "security_code", "data_version", name="uq_security_dataset_version"
        ),
        CheckConstraint(
            "status IN ('published', 'quality_failed')",
            name="ck_security_dataset_status",
        ),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_security_dataset_sha256"),
        Index("ix_security_dataset_code_built", "security_code", "built_at"),
    )
