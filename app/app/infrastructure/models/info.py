from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.infrastructure.models.base import Base, TimestampMixin, UUIDMixin


class InfoSource(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "info_source"

    code: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False, default="website")
    base_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")
    trust_level: Mapped[str] = mapped_column(String(30), nullable=False, default="unknown")
    copyright_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="unknown"
    )
    license_url: Mapped[str | None] = mapped_column(Text)
    terms_url: Mapped[str | None] = mapped_column(Text)
    crawl_policy: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    description: Mapped[str | None] = mapped_column(Text)


class InfoCollector(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "info_collector"

    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("info_source.id"), nullable=True
    )
    code: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    collector_type: Mapped[str] = mapped_column(String(50), nullable=False, default="http")
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")


class CrawlJob(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "crawl_job"

    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("info_source.id"), nullable=True
    )
    collector_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("info_collector.id"), nullable=True
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("info_document.id"), nullable=True
    )
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("info_document_version.id"), nullable=True
    )
    job_type: Mapped[str] = mapped_column(String(50), nullable=False, default="url")
    target_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    http_status: Mapped[int | None] = mapped_column(Integer)
    final_url: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(120))
    error_message: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    response_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    artifacts: Mapped[list[RawArtifact]] = relationship(back_populates="crawl_job")


class RawArtifact(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "raw_artifact"

    crawl_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("crawl_job.id"), nullable=False
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("info_document.id"), nullable=True
    )
    document_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("info_document_version.id"), nullable=True
    )
    artifact_type: Mapped[str] = mapped_column(String(50), nullable=False)
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    version_id: Mapped[str | None] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_type: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_state: Mapped[str] = mapped_column(String(30), nullable=False, default="available")
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    crawl_job: Mapped[CrawlJob] = relationship(back_populates="artifacts")


class InfoDocument(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "info_document"

    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("info_source.id"), nullable=True
    )
    canonical_url: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    source_name: Mapped[str | None] = mapped_column(String(255))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    content_hash: Mapped[str | None] = mapped_column(String(64))
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class InfoDocumentVersion(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "info_document_version"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("info_document.id"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_artifact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    clean_artifact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    text_artifact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    extraction_status: Mapped[str] = mapped_column(String(30), nullable=False, default="succeeded")
    extractor_name: Mapped[str | None] = mapped_column(String(120))
    extractor_version: Mapped[str | None] = mapped_column(String(120))
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("document_id", "version_no", name="uq_info_document_version_no"),
        Index("ix_info_document_version_hash", "content_hash"),
    )


class ExtractedContent(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "extracted_content"

    document_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("info_document_version.id"), nullable=False
    )
    content_format: Mapped[str] = mapped_column(String(30), nullable=False)
    bucket: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    extractor_name: Mapped[str | None] = mapped_column(String(120))
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class DistributionRecord(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "distribution_record"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("info_document.id"), nullable=False
    )
    document_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("info_document_version.id"), nullable=False
    )
    target_app: Mapped[str] = mapped_column(String(80), nullable=False)
    target_dataset: Mapped[str | None] = mapped_column(String(255))
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    last_error: Mapped[str | None] = mapped_column(Text)


class DeliveryOutboxMessage(UUIDMixin, TimestampMixin, Base):
    """A durable request to deliver one Info domain operation asynchronously.

    The row is the local source of truth until the downstream worker confirms
    completion.  RabbitMQ/Celery only transports an at-least-once notification;
    it is deliberately not used as the recovery store.
    """

    __tablename__ = "delivery_outbox_message"

    topic: Mapped[str] = mapped_column(String(120), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(80), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("distribution_record.id"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    state: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    broker_message_id: Mapped[str | None] = mapped_column(String(255))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint(
            "topic", "idempotency_key", name="uq_delivery_outbox_topic_idempotency"
        ),
        Index("ix_delivery_outbox_due", "state", "available_at"),
        Index("ix_delivery_outbox_aggregate", "aggregate_type", "aggregate_id"),
    )
