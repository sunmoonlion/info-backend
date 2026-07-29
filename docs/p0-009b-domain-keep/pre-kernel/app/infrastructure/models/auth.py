from __future__ import annotations

import uuid

from sqlalchemy import Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.models.base import Base, TimestampMixin, UUIDMixin


class AuthUser(UUIDMixin, TimestampMixin, Base):
    """Local identity binding; provider subject remains the external identity."""

    __tablename__ = "auth_user"

    issuer: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    username: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    display_name: Mapped[str | None] = mapped_column(String(256))
    roles: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="uq_auth_user_issuer_subject"),
        Index("ix_auth_user_subject", "subject"),
    )
