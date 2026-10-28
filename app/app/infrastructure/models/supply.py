"""供给的表（SDD 0045 第五节）：渠道、需求单与需求项、作业与尝试、留 30 天的原件。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.models.base import Base


class SupplySecurity(Base):
    """证券名录（查主体用，SDD 0043 第 3.1 节）：巨潮的证券列表，整份换、一天至多一次。"""

    __tablename__ = "supply_security"

    subject: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    pinyin: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class SupplyChannel(Base):
    __tablename__ = "supply_channel"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    plugin: Mapped[str] = mapped_column(String(40), nullable=False)
    doc_types: Mapped[list] = mapped_column(JSONB, nullable=False)
    readings: Mapped[list] = mapped_column(JSONB, nullable=False)
    license: Mapped[str] = mapped_column(String(16), nullable=False)
    review: Mapped[str] = mapped_column(String(16), nullable=False)
    rate_per_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    daily_cap: Mapped[int] = mapped_column(Integer, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('catalog', 'feed', 'web', 'search')",
            name="ck_supply_channel_kind",
        ),
        CheckConstraint(
            "review IN ('approved', 'pending', 'rejected')",
            name="ck_supply_channel_review",
        ),
        CheckConstraint(
            "status IN ('active', 'paused')", name="ck_supply_channel_status"
        ),
    )


class SupplyDemand(Base):
    __tablename__ = "supply_demand"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    owner_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    proposer: Mapped[str] = mapped_column(String(16), nullable=False)
    chain: Mapped[list] = mapped_column(JSONB, nullable=False)
    app: Mapped[str] = mapped_column(String(32), nullable=False)
    task_ref: Mapped[str | None] = mapped_column(String(255))
    confirmation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("app", "idempotency_key", name="uq_supply_demand_key"),
    )


class SupplyDemandItem(Base):
    __tablename__ = "supply_demand_item"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    demand_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("supply_demand.id"), nullable=False
    )
    key: Mapped[dict] = mapped_column(JSONB, nullable=False)
    key_text: Mapped[str] = mapped_column(String(200), nullable=False)
    readings: Mapped[list] = mapped_column(JSONB, nullable=False)
    confirmation_item: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    channel_id: Mapped[str | None] = mapped_column(String(40))
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reason: Mapped[str | None] = mapped_column(String(200))
    receipt: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (Index("ix_supply_demand_item_job", "job_id", "status"),)


class SupplyJob(Base):
    __tablename__ = "supply_job"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(40), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed', 'waiting')",
            name="ck_supply_job_status",
        ),
        Index(
            "uq_supply_job_open",
            "dedup_key",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running', 'waiting')"),
        ),
    )


class SupplyAttempt(Base):
    __tablename__ = "supply_attempt"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("supply_job.id"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(200))


class SupplyOriginal(Base):
    __tablename__ = "supply_original"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    channel_id: Mapped[str] = mapped_column(String(40), nullable=False)
    channel_doc_id: Mapped[str] = mapped_column(String(200), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    knowbase_material_id: Mapped[str | None] = mapped_column(String(64))
    knowbase_version_no: Mapped[int | None] = mapped_column(Integer)
