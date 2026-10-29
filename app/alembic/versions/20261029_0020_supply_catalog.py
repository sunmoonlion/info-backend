"""来源目录与需求配额（SDD 0045 第 3.1 节、第七节块 2、块 4）。

- 渠道（`supply_channel`）补登记要写的：网站（通用网页按网站审，一个网站一条，唯一）、
  条款链接、能否渲染、守不守 robots、状态原因、适配器配置、登记时间；状态多一个「停用」
  （审核撤销），原来的「暂停」留给连续失败自动暂停与手动暂停。巨潮写上它的网站。
- 审核记录（`supply_channel_event`）：谁、何时、做了什么、凭什么。
- 试采记录（`supply_channel_trial`）：放行之前对样例描述跑一次「列」和「取」的结果。
- 需求单按归属用户与时间建索引：受理时数配额（请求频率、在途单数、每月新取份数）。
- 需求项的原因改成稳定的编码（缺口清单按它分组、应用按它认）。

Revision ID: 20261029_0020
Revises: 20261028_0019
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261029_0020"
down_revision = "20261028_0019"
branch_labels = None
depends_on = None

# 旧的原因句子 → 编码
REASONS = (
    ("no channel can supply this yet", "no_channel"),
    ("the channel has no such document", "channel_has_none"),
    ("some readings are not ready", "readings_incomplete"),
    ("cancelled by the app", "cancelled"),
)


def upgrade() -> None:
    op.add_column("supply_channel", sa.Column("domain", sa.String(253)))
    op.add_column("supply_channel", sa.Column("terms_url", sa.Text()))
    op.add_column(
        "supply_channel",
        sa.Column(
            "render_allowed", sa.Boolean(), nullable=False, server_default="false"
        ),
    )
    op.add_column(
        "supply_channel",
        sa.Column("obey_robots", sa.Boolean(), nullable=False, server_default="true"),
    )
    op.add_column("supply_channel", sa.Column("status_reason", sa.String(200)))
    op.add_column(
        "supply_channel",
        sa.Column(
            "config",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column("supply_channel", sa.Column("created_at", sa.DateTime(timezone=True)))
    op.execute("UPDATE supply_channel SET created_at = updated_at")
    op.alter_column("supply_channel", "created_at", nullable=False)
    op.execute("UPDATE supply_channel SET domain = 'cninfo.com.cn' WHERE id = 'cninfo'")
    op.drop_constraint("ck_supply_channel_status", "supply_channel", type_="check")
    op.create_check_constraint(
        "ck_supply_channel_status",
        "supply_channel",
        "status IN ('active', 'paused', 'disabled')",
    )
    op.create_index(
        "uq_supply_channel_domain",
        "supply_channel",
        ["domain"],
        unique=True,
        postgresql_where=sa.text("domain IS NOT NULL"),
    )

    op.create_table(
        "supply_channel_event",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "channel_id",
            sa.String(40),
            sa.ForeignKey("supply_channel.id"),
            nullable=False,
        ),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("reason", sa.String(500)),
        sa.Column(
            "detail",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_supply_channel_event_channel", "supply_channel_event", ["channel_id", "at"]
    )
    op.create_table(
        "supply_channel_trial",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "channel_id",
            sa.String(40),
            sa.ForeignKey("supply_channel.id"),
            nullable=False,
        ),
        sa.Column("key", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "listed",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("fetched", postgresql.JSONB()),
        sa.Column("error", sa.String(200)),
        sa.Column("requested_by", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'ok', 'failed')",
            name="ck_supply_channel_trial_status",
        ),
    )
    op.create_index(
        "ix_supply_channel_trial_channel",
        "supply_channel_trial",
        ["channel_id", "created_at"],
    )
    op.create_index(
        "ix_supply_demand_owner", "supply_demand", ["owner_subject", "created_at"]
    )
    op.create_index(
        "ix_supply_original_purge",
        "supply_original",
        ["purge_after"],
        postgresql_where=sa.text("purged_at IS NULL AND purge_after IS NOT NULL"),
    )
    for old, new in REASONS:
        op.execute(
            sa.text(
                "UPDATE supply_demand_item SET reason = :new WHERE reason = :old"
            ).bindparams(old=old, new=new)
        )


def downgrade() -> None:
    for old, new in REASONS:
        op.execute(
            sa.text(
                "UPDATE supply_demand_item SET reason = :old WHERE reason = :new"
            ).bindparams(old=old, new=new)
        )
    op.drop_index("ix_supply_original_purge", table_name="supply_original")
    op.drop_index("ix_supply_demand_owner", table_name="supply_demand")
    op.drop_index("ix_supply_channel_trial_channel", table_name="supply_channel_trial")
    op.drop_table("supply_channel_trial")
    op.drop_index("ix_supply_channel_event_channel", table_name="supply_channel_event")
    op.drop_table("supply_channel_event")
    op.drop_index("uq_supply_channel_domain", table_name="supply_channel")
    # 停用的回到暂停（0019 只认在用、暂停）
    op.execute("UPDATE supply_channel SET status = 'paused' WHERE status = 'disabled'")
    op.drop_constraint("ck_supply_channel_status", "supply_channel", type_="check")
    op.create_check_constraint(
        "ck_supply_channel_status",
        "supply_channel",
        "status IN ('active', 'paused')",
    )
    for column in (
        "created_at",
        "config",
        "status_reason",
        "obey_robots",
        "render_allowed",
        "terms_url",
        "domain",
    ):
        op.drop_column("supply_channel", column)
