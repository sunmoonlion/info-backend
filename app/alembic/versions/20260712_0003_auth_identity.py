"""auth identity boundary

Revision ID: 20260712_0003
Revises: 20260707_0002
Create Date: 2026-07-12
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260712_0003"
down_revision = "20260707_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_user",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("subject", sa.String(length=512), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("display_name", sa.String(length=256), nullable=True),
        sa.Column(
            "roles",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "scopes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("issuer", "subject", name="uq_auth_user_issuer_subject"),
    )
    op.create_index("ix_auth_user_subject", "auth_user", ["subject"])


def downgrade() -> None:
    op.drop_index("ix_auth_user_subject", table_name="auth_user")
    op.drop_table("auth_user")
