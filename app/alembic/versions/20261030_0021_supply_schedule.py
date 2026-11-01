"""调度、加工、切合（SDD 0045 第 3.3、3.4 节、第四节；发版前补齐 F6、F7、F9）。

- 渠道（`supply_channel`）补「同时最多跑几个作业」（登记的并发，0045 第 3.3 节「按登记的
  限速与并发」）。
- 作业（`supply_job`）补「不早于」（等重试、日上限用完等到第二天）与「排出去的时间」：
  作业不再一受理就投给 worker，由调度按渠道、四档、按用户轮流排出去。
- 取数记录（`supply_channel_request`）：每问一次渠道记一行，限速按它数（每分钟几次按渠道、
  也按出站地址全局；每天几份按渠道）。只留两天。
- 轮流记录（`supply_turn`）：每条队列里每个用户上次轮到的时间。
- 需求单（`supply_demand`）记确认时的供给条件（许可档下限、只用哪几条渠道）；需求项
  （`supply_demand_item`）记试过哪几条渠道（取不到换下一个）。
- 来源目录多登记一条待审的渠道「巨潮互动易」（互动问答，0049 第三节），平台人员试采、
  放行以后才用得上。

Revision ID: 20261030_0021
Revises: 20261029_0020
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261030_0021"
down_revision = "20261029_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "supply_channel",
        sa.Column("max_running", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("supply_job", sa.Column("not_before", sa.DateTime(timezone=True)))
    op.add_column("supply_job", sa.Column("dispatched_at", sa.DateTime(timezone=True)))
    op.create_index(
        "ix_supply_job_queue",
        "supply_job",
        ["channel_id", "tier", "created_at"],
        postgresql_where=sa.text("status IN ('queued', 'running', 'waiting')"),
    )
    op.add_column(
        "supply_demand",
        sa.Column(
            "conditions",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "supply_demand_item",
        sa.Column(
            "tried_channels",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_table(
        "supply_channel_request",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("channel_id", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('request', 'fetch')", name="ck_supply_channel_request_kind"
        ),
    )
    op.create_index(
        "ix_supply_channel_request_channel",
        "supply_channel_request",
        ["channel_id", "kind", "at"],
    )
    op.create_index("ix_supply_channel_request_at", "supply_channel_request", ["at"])
    op.create_table(
        "supply_turn",
        sa.Column("queue", sa.String(40), primary_key=True),
        sa.Column("owner_subject", sa.String(255), primary_key=True),
        sa.Column("served_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute(
        """
        INSERT INTO supply_channel (id, name, kind, plugin, doc_types, readings,
            license, review, rate_per_minute, daily_cap, priority, status,
            consecutive_failures, domain, terms_url, render_allowed, obey_robots,
            config, max_running, created_at, updated_at)
        VALUES ('cninfo-irm', '巨潮互动易（深市公司的投资者问答）', 'catalog',
            'cninfo-irm', '["ir_qa"]', '["original", "text"]', 'via_expert',
            'pending', 10, 500, 20, 'active', 0, 'irm.cninfo.com.cn',
            'https://irm.cninfo.com.cn/', false, true, '{"markets": ["SZ"]}', 1,
            now(), now())
        ON CONFLICT (id) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO supply_channel_event (id, channel_id, action, actor, reason,
            detail, at)
        SELECT gen_random_uuid(), 'cninfo-irm', 'register', 'system',
            '发版前补齐 F9：互动问答渠道，待平台人员试采、放行', '{}'::jsonb, now()
        WHERE NOT EXISTS (
            SELECT 1 FROM supply_channel_event WHERE channel_id = 'cninfo-irm'
        )
        """
    )


def downgrade() -> None:
    # 删渠道之前先收拾挂在它上面的：没办完的作业取消，在等的需求项转「无法满足」（渠道停用），
    # 免得回退后作业、需求项指着一条不存在的渠道（独立审第 16 条）。回退在迁移里跑，状态
    # 通知不经发件箱推，应用按单号查得到。
    op.execute(
        """
        UPDATE supply_job SET status = 'failed', last_error = 'channel_removed',
            finished_at = now()
        WHERE channel_id = 'cninfo-irm'
            AND status IN ('queued', 'running', 'waiting')
        """
    )
    op.execute(
        """
        UPDATE supply_demand_item SET status = 'unsatisfiable',
            reason = 'channel_disabled', updated_at = now()
        WHERE channel_id = 'cninfo-irm'
            AND status NOT IN ('ready', 'partial', 'failed', 'unsatisfiable',
                'cancelled', 'timeout')
        """
    )
    op.execute("DELETE FROM supply_channel_trial WHERE channel_id = 'cninfo-irm'")
    op.execute("DELETE FROM supply_channel_event WHERE channel_id = 'cninfo-irm'")
    op.execute("DELETE FROM supply_channel WHERE id = 'cninfo-irm'")
    op.drop_table("supply_turn")
    op.drop_index("ix_supply_channel_request_at", table_name="supply_channel_request")
    op.drop_index(
        "ix_supply_channel_request_channel", table_name="supply_channel_request"
    )
    op.drop_table("supply_channel_request")
    op.drop_column("supply_demand_item", "tried_channels")
    op.drop_column("supply_demand", "conditions")
    op.drop_index("ix_supply_job_queue", table_name="supply_job")
    op.drop_column("supply_job", "dispatched_at")
    op.drop_column("supply_job", "not_before")
    op.drop_column("supply_channel", "max_running")
