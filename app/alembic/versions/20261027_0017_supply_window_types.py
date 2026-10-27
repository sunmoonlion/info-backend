"""巨潮渠道能供的细类加上非定期的几样（SDD 0049 第四节）。

词表第 2 版：临时公告、招股书、上市公告书、投资者关系活动记录、监管函件按披露时间窗
列出、逐份交付；定期报告照旧。

Revision ID: 20261027_0017
Revises: 20261027_0016
"""

from __future__ import annotations

from alembic import op

revision = "20261027_0017"
down_revision = "20261027_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE supply_channel SET doc_types = '["annual_report", "semiannual_report",
            "q1_report", "q3_report", "interim_announcement", "prospectus",
            "listing_announcement", "ir_activity", "regulatory_letter"]',
            updated_at = now()
        WHERE id = 'cninfo'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE supply_channel SET doc_types = '["annual_report", "semiannual_report",
            "q1_report", "q3_report"]', updated_at = now()
        WHERE id = 'cninfo'
        """
    )
