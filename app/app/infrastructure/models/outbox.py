from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.models.base import Base, TimestampMixin


class OutboxMessage(TimestampMixin, Base):
    __tablename__ = "outbox_message"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    topic: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregate_key: Mapped[str] = mapped_column(String(512), nullable=False)
    deduplication_key: Mapped[str] = mapped_column(
        String(512), nullable=False, unique=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    headers: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending"
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'delivering', 'published')",
            name="ck_outbox_message_status",
        ),
        Index(
            "ix_outbox_message_claim",
            "status",
            "available_at",
            "lease_expires_at",
        ),
    )


class InboxMessage(Base):
    """Idempotency marker committed with a consumer's local side effects."""

    __tablename__ = "inbox_message"

    consumer: Mapped[str] = mapped_column(String(128), primary_key=True)
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
