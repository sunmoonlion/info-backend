"""给网址、订阅、按主题找（SDD 0045 第 2.2、2.5 节，第 3.2 节，第七节块 3、9、10；
发版前补齐 F1、F4）。

- 需求单（`supply_demand`）记「最晚什么时候要」与说明（0045 第 2.2 节公共部分）。
- 需求项（`supply_demand_item`）记给网址的那一项：网址、要不要连它直接链到的文件、用户
  预填的描述；「再试一次」挂在哪一项下。
- 订阅（`supply_subscription`）、每轮记录（`supply_subscription_round`）、各订阅自己的进度
  （`supply_subscription_take`：哪一份取了、本月超额没取、跳过）。
- 渠道的发现（`supply_discovery`）与发现游标（`supply_watch_cursor`）：订同一个对象的人
  共用，各订阅各记进度。
- 按主题找（`supply_search`）：正在找 → 候选已出（7 天）→ 已结束。
- 渠道（`supply_channel`）记审核定的「区」：`disclosure`（法定披露，进公开原件层）、
  `collected`（为用户采集，每位受益人各得一份自己名下的资料）；巨潮两条是法定披露，其余
  已登记的默认为用户采集（0045 第 3.5 节、0043 第 2.1、2.4 节）。
- 作业（`supply_job`）记派发代次：同一作业只让拿着这一次派发代次的 worker 跑。
- 需求项记用户的「公开页面、不用登录」声明（0045 第 2.2 节）。
- web端提交（`supply_web_submission`）：按（用户，幂等钥匙）先占位再去 knowbase 记确认，
  并发重复提交不留孤儿确认；内容不同的拒绝。

Revision ID: 20261030_0022
Revises: 20261030_0021
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261030_0022"
down_revision = "20261030_0021"
branch_labels = None
depends_on = None

_JSON_LIST = sa.text("'[]'::jsonb")
_JSON_OBJECT = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.add_column(
        "supply_channel",
        sa.Column("zone", sa.String(16), nullable=False, server_default="collected"),
    )
    op.create_check_constraint(
        "ck_supply_channel_zone",
        "supply_channel",
        "zone IN ('disclosure', 'collected')",
    )
    op.execute(
        "UPDATE supply_channel SET zone = 'disclosure' "
        "WHERE id IN ('cninfo', 'cninfo-irm')"
    )
    op.add_column(
        "supply_job", sa.Column("dispatch_token", postgresql.UUID(as_uuid=True))
    )
    op.add_column(
        "supply_demand_item",
        sa.Column(
            "declared_public", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.create_table(
        "supply_web_submission",
        sa.Column("owner_subject", sa.String(255), primary_key=True),
        sa.Column("idempotency_key", sa.String(128), primary_key=True),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("demand_id", postgresql.UUID(as_uuid=True)),
        sa.Column("result", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column("supply_demand", sa.Column("deadline", sa.DateTime(timezone=True)))
    op.add_column("supply_demand", sa.Column("note", sa.String(500)))
    op.create_index(
        "ix_supply_demand_deadline",
        "supply_demand",
        ["deadline"],
        postgresql_where=sa.text("deadline IS NOT NULL"),
    )
    op.add_column("supply_demand_item", sa.Column("url", sa.Text()))
    op.add_column(
        "supply_demand_item",
        sa.Column(
            "follow_links", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "supply_demand_item",
        sa.Column(
            "prefill", postgresql.JSONB(), nullable=False, server_default=_JSON_OBJECT
        ),
    )
    op.add_column(
        "supply_demand_item", sa.Column("retry_of", postgresql.UUID(as_uuid=True))
    )
    op.create_index(
        "ix_supply_demand_item_channel_open",
        "supply_demand_item",
        ["channel_id", "status"],
    )

    op.create_table(
        "supply_subscription",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_subject", sa.String(255), nullable=False),
        sa.Column("by", sa.String(16), nullable=False),
        sa.Column("subjects", postgresql.JSONB(), nullable=False),
        sa.Column("doc_types", postgresql.JSONB(), nullable=False),
        sa.Column("urls", postgresql.JSONB(), nullable=False),
        sa.Column("topic", sa.String(200)),
        sa.Column("readings", postgresql.JSONB(), nullable=False),
        sa.Column("every_hours", sa.Integer(), nullable=False),
        sa.Column("starts_on", sa.Date(), nullable=False),
        sa.Column("ends_on", sa.Date(), nullable=False),
        sa.Column("monthly_cap", sa.Integer(), nullable=False),
        sa.Column("purpose", sa.String(200), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("status_reason", sa.String(200)),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "skip_backlog", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "channels", postgresql.JSONB(), nullable=False, server_default=_JSON_OBJECT
        ),
        sa.Column("next_check_at", sa.DateTime(timezone=True)),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("round_id", postgresql.UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "by IN ('description', 'url', 'topic')", name="ck_supply_subscription_by"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'expired', 'cancelled', 'disabled')",
            name="ck_supply_subscription_status",
        ),
    )
    op.create_index(
        "ix_supply_subscription_owner",
        "supply_subscription",
        ["owner_subject", "created_at"],
    )
    op.create_index(
        "ix_supply_subscription_due",
        "supply_subscription",
        ["next_check_at"],
        postgresql_where=sa.text("status IN ('active', 'paused')"),
    )
    op.create_table(
        "supply_subscription_round",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "subscription_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("supply_subscription.id"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("taken", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("over_cap", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("demand_id", postgresql.UUID(as_uuid=True)),
        sa.Column("note", sa.String(200)),
    )
    op.create_index(
        "ix_supply_subscription_round_subscription",
        "supply_subscription_round",
        ["subscription_id", "started_at"],
    )
    op.create_table(
        "supply_subscription_take",
        sa.Column(
            "subscription_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("supply_subscription.id"),
            primary_key=True,
        ),
        sa.Column("channel_id", sa.String(40), primary_key=True),
        sa.Column("channel_doc_id", sa.String(200), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("entry", postgresql.JSONB(), nullable=False),
        sa.Column("target", sa.String(400), nullable=False),
        sa.Column("round_id", postgresql.UUID(as_uuid=True)),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('taken', 'over_cap', 'skipped')",
            name="ck_supply_subscription_take_status",
        ),
    )
    op.create_index(
        "ix_supply_subscription_take_month",
        "supply_subscription_take",
        ["subscription_id", "status", "at"],
    )
    op.create_table(
        "supply_discovery",
        sa.Column("channel_id", sa.String(40), primary_key=True),
        sa.Column("target", sa.String(400), primary_key=True),
        sa.Column("channel_doc_id", sa.String(200), primary_key=True),
        sa.Column("entry", postgresql.JSONB(), nullable=False),
        sa.Column("disclosed_on", sa.Date()),
        sa.Column("found_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "supply_watch_cursor",
        sa.Column("channel_id", sa.String(40), primary_key=True),
        sa.Column("target", sa.String(400), primary_key=True),
        sa.Column(
            "cursor", postgresql.JSONB(), nullable=False, server_default=_JSON_OBJECT
        ),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "supply_search",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_subject", sa.String(255), nullable=False),
        sa.Column("topic", sa.String(200), nullable=False),
        sa.Column(
            "sites", postgresql.JSONB(), nullable=False, server_default=_JSON_LIST
        ),
        sa.Column("wanted", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "candidates", postgresql.JSONB(), nullable=False, server_default=_JSON_LIST
        ),
        sa.Column("note", sa.String(200)),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('searching', 'ready', 'ended')", name="ck_supply_search_status"
        ),
    )
    op.create_index(
        "ix_supply_search_owner", "supply_search", ["owner_subject", "created_at"]
    )
    op.create_table(
        "supply_original_delivery",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "original_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("supply_original.id"),
            nullable=False,
        ),
        sa.Column("channel_id", sa.String(40), nullable=False),
        sa.Column("channel_doc_id", sa.String(200), nullable=False),
        sa.Column("zone", sa.String(16), nullable=False),
        sa.Column("owner_subject", sa.String(255), nullable=False),
        sa.Column("knowbase_material_id", sa.String(64), nullable=False),
        sa.Column("knowbase_version_no", sa.Integer(), nullable=False),
        sa.Column(
            "readings",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text('\'["original"]\'::jsonb'),
        ),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "zone IN ('disclosure', 'collected')",
            name="ck_supply_original_delivery_zone",
        ),
        sa.UniqueConstraint(
            "channel_id",
            "channel_doc_id",
            "zone",
            "owner_subject",
            name="uq_supply_original_delivery",
        ),
    )
    op.execute(
        """
        INSERT INTO supply_original_delivery (
            id, original_id, channel_id, channel_doc_id, zone, owner_subject,
            knowbase_material_id, knowbase_version_no, readings, delivered_at
        )
        SELECT gen_random_uuid(), o.id, o.channel_id, o.channel_doc_id,
            coalesce(c.zone, 'disclosure'),
            CASE
                WHEN coalesce(c.zone, 'disclosure') = 'disclosure'
                THEN 'public:disclosure'
                ELSE 'legacy'
            END,
            o.knowbase_material_id,
            coalesce(o.knowbase_version_no, 1),
            coalesce(o.meta->'readings', '["original"]'::jsonb),
            coalesce(o.delivered_at, o.fetched_at)
        FROM supply_original o
        LEFT JOIN supply_channel c ON c.id = o.channel_id
        WHERE o.knowbase_material_id IS NOT NULL
        ON CONFLICT (channel_id, channel_doc_id, zone, owner_subject) DO NOTHING
        """
    )
    op.execute("UPDATE supply_original SET meta = meta - 'readings'")
    op.drop_column("supply_original", "knowbase_version_no")
    op.drop_column("supply_original", "knowbase_material_id")


def downgrade() -> None:
    # 回退之前收拾这一版才有的活（照 0021 的标准，二审第 14 条、三审第 17 条）：没办完的
    # 「看新的」、「搜」、给网址的、订阅按条目取数的作业取消；挂在这些作业上或给网址/订阅
    # 的没结束需求项一并转「无法满足」；给网址的项把 key 补成旧版 DescriptionKey 能读的形状。
    op.execute(
        """
        UPDATE supply_demand_item SET status = 'unsatisfiable',
            reason = 'feature_removed', updated_at = now()
        WHERE (
            url IS NOT NULL
            OR job_id IN (
                SELECT id FROM supply_job
                WHERE kind IN ('watch', 'search') OR payload ? 'url' OR payload ? 'entry'
            )
            OR demand_id IN (
                SELECT id FROM supply_demand WHERE proposer = 'subscription'
            )
        )
        AND status NOT IN ('ready', 'partial', 'failed', 'unsatisfiable',
            'cancelled', 'timeout')
        """
    )
    op.execute(
        """
        UPDATE supply_job SET status = 'failed', last_error = 'feature_removed',
            finished_at = now()
        WHERE status IN ('queued', 'running', 'waiting')
            AND (kind IN ('watch', 'search') OR payload ? 'url' OR payload ? 'entry')
        """
    )
    op.execute(
        """
        UPDATE supply_demand_item
        SET key = jsonb_build_object(
            'subject',
            CASE
                WHEN coalesce(prefill->>'subject', '') ~ '^[0-9]{6}\\.(SH|SZ|BJ)$'
                THEN prefill->>'subject'
                ELSE '000001.SZ'
            END,
            'doc_type', coalesce(nullif(prefill->>'doc_type', ''), 'webpage'),
            'period_start', null,
            'period_end', null
        )
        WHERE url IS NOT NULL OR NOT (key ? 'subject' AND key ? 'doc_type')
        """
    )
    op.add_column("supply_original", sa.Column("knowbase_material_id", sa.String(64)))
    op.add_column("supply_original", sa.Column("knowbase_version_no", sa.Integer()))
    op.execute(
        """
        UPDATE supply_original o
        SET knowbase_material_id = d.knowbase_material_id,
            knowbase_version_no = d.knowbase_version_no,
            meta = jsonb_set(o.meta, '{readings}', d.readings)
        FROM supply_original_delivery d
        WHERE d.original_id = o.id AND d.zone = 'disclosure'
        """
    )
    op.drop_table("supply_original_delivery")
    op.drop_table("supply_web_submission")
    op.drop_column("supply_demand_item", "declared_public")
    op.drop_column("supply_job", "dispatch_token")
    op.drop_constraint("ck_supply_channel_zone", "supply_channel", type_="check")
    op.drop_column("supply_channel", "zone")
    op.drop_index("ix_supply_search_owner", table_name="supply_search")
    op.drop_table("supply_search")
    op.drop_table("supply_watch_cursor")
    op.drop_table("supply_discovery")
    op.drop_index(
        "ix_supply_subscription_take_month", table_name="supply_subscription_take"
    )
    op.drop_table("supply_subscription_take")
    op.drop_index(
        "ix_supply_subscription_round_subscription",
        table_name="supply_subscription_round",
    )
    op.drop_table("supply_subscription_round")
    op.drop_index("ix_supply_subscription_due", table_name="supply_subscription")
    op.drop_index("ix_supply_subscription_owner", table_name="supply_subscription")
    op.drop_table("supply_subscription")
    op.drop_index("ix_supply_demand_item_channel_open", table_name="supply_demand_item")
    op.drop_column("supply_demand_item", "retry_of")
    op.drop_column("supply_demand_item", "prefill")
    op.drop_column("supply_demand_item", "follow_links")
    op.drop_column("supply_demand_item", "url")
    op.drop_index("ix_supply_demand_deadline", table_name="supply_demand")
    op.drop_column("supply_demand", "note")
    op.drop_column("supply_demand", "deadline")
