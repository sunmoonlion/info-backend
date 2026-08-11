"""make the delivery outbox UUID default explicit

Revision ID: 20260811_0006
Revises: 20260809_0005
Create Date: 2026-08-11
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260811_0006"
down_revision = "20260809_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The ORM has always declared a server-generated UUID, but the original
    # business-outbox migration omitted that default.  Keep both a client-side
    # UUID (for unit-of-work ordering) and this database fallback so direct SQL
    # and future publishers obey the same invariant.
    op.alter_column(
        "delivery_outbox_message",
        "id",
        server_default=sa.text("uuid_generate_v4()"),
    )


def downgrade() -> None:
    op.alter_column(
        "delivery_outbox_message",
        "id",
        server_default=None,
    )
