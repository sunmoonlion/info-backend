"""证券名录（SDD 0043 第 3.1 节「查主体」，decisions ⑭）。

委托第 0 步模型按名字查证券代码：名录取自巨潮的证券列表（沪深京 A 股），整份换，
一天至多刷新一次。

Revision ID: 20261028_0018
Revises: 20261027_0017
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261028_0018"
down_revision = "20261027_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "supply_security",
        sa.Column("subject", sa.String(16), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("pinyin", sa.String(32), nullable=False, server_default=""),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_supply_security_name", "supply_security", ["name"])


def downgrade() -> None:
    op.drop_index("ix_supply_security_name", table_name="supply_security")
    op.drop_table("supply_security")
