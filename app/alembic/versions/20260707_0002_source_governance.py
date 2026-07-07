"""source governance metadata

Revision ID: 20260707_0002
Revises: 20260706_0001
Create Date: 2026-07-07
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260707_0002"
down_revision = "20260706_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "info_source",
        sa.Column(
            "trust_level",
            sa.String(length=30),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.add_column(
        "info_source",
        sa.Column(
            "copyright_status",
            sa.String(length=30),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.add_column("info_source", sa.Column("license_url", sa.Text(), nullable=True))
    op.add_column("info_source", sa.Column("terms_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("info_source", "terms_url")
    op.drop_column("info_source", "license_url")
    op.drop_column("info_source", "copyright_status")
    op.drop_column("info_source", "trust_level")
