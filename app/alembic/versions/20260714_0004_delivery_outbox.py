"""durable delivery outbox

Revision ID: 20260714_0004
Revises: 20260712_0003
Create Date: 2026-07-14
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260714_0004"
down_revision = "20260712_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "delivery_outbox_message",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("topic", sa.String(length=120), nullable=False),
        sa.Column("aggregate_type", sa.String(length=80), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "state", sa.String(length=30), nullable=False, server_default="pending"
        ),
        sa.Column(
            "attempt_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("lease_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("broker_message_id", sa.String(length=255), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(["aggregate_id"], ["distribution_record.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "topic", "idempotency_key", name="uq_delivery_outbox_topic_idempotency"
        ),
    )
    op.create_index(
        "ix_delivery_outbox_due",
        "delivery_outbox_message",
        ["state", "available_at"],
    )
    op.create_index(
        "ix_delivery_outbox_aggregate",
        "delivery_outbox_message",
        ["aggregate_type", "aggregate_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_delivery_outbox_aggregate", table_name="delivery_outbox_message")
    op.drop_index("ix_delivery_outbox_due", table_name="delivery_outbox_message")
    op.drop_table("delivery_outbox_message")
