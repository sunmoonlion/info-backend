"""Supply v4: channels, demands, jobs, attempts, originals (SDD 0045, vertical slice).

New tables next to the old security line and generic crawl, which block 11
retires. The cninfo channel is seeded as an approved catalog channel.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261027_0016"
down_revision = "20261025_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "supply_channel",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("plugin", sa.String(40), nullable=False),
        sa.Column("doc_types", postgresql.JSONB(), nullable=False),
        sa.Column("readings", postgresql.JSONB(), nullable=False),
        sa.Column("license", sa.String(16), nullable=False),
        sa.Column("review", sa.String(16), nullable=False),
        sa.Column("rate_per_minute", sa.Integer(), nullable=False),
        sa.Column("daily_cap", sa.Integer(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "consecutive_failures", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('catalog', 'feed', 'web', 'search')",
            name="ck_supply_channel_kind",
        ),
        sa.CheckConstraint(
            "review IN ('approved', 'pending', 'rejected')",
            name="ck_supply_channel_review",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused')", name="ck_supply_channel_status"
        ),
    )
    op.execute(
        """
        INSERT INTO supply_channel (id, name, kind, plugin, doc_types, readings,
            license, review, rate_per_minute, daily_cap, priority, status, updated_at)
        VALUES ('cninfo', '巨潮资讯网（法定信息披露）', 'catalog', 'cninfo',
            '["annual_report", "semiannual_report", "q1_report", "q3_report"]',
            '["original", "pages", "table"]', 'via_expert', 'approved', 20, 3000, 10,
            'active', now())
        """
    )
    op.create_table(
        "supply_demand",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_subject", sa.String(255), nullable=False),
        sa.Column("proposer", sa.String(16), nullable=False),
        sa.Column("chain", postgresql.JSONB(), nullable=False),
        sa.Column("app", sa.String(32), nullable=False),
        sa.Column("task_ref", sa.String(255)),
        sa.Column("confirmation_id", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("app", "idempotency_key", name="uq_supply_demand_key"),
    )
    op.create_table(
        "supply_demand_item",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "demand_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("supply_demand.id"),
            nullable=False,
        ),
        sa.Column("key", postgresql.JSONB(), nullable=False),
        sa.Column("key_text", sa.String(200), nullable=False),
        sa.Column("readings", postgresql.JSONB(), nullable=False),
        sa.Column("confirmation_item", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("channel_id", sa.String(40)),
        sa.Column("job_id", postgresql.UUID(as_uuid=True)),
        sa.Column("reason", sa.String(200)),
        sa.Column(
            "receipt",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_supply_demand_item_job", "supply_demand_item", ["job_id", "status"]
    )
    op.create_table(
        "supply_job",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("channel_id", sa.String(40), nullable=False),
        sa.Column("dedup_key", sa.String(255), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("tier", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(200)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed', 'waiting')",
            name="ck_supply_job_status",
        ),
    )
    op.create_index(
        "uq_supply_job_open",
        "supply_job",
        ["dedup_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running', 'waiting')"),
    )
    op.create_table(
        "supply_attempt",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("supply_job.id"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("outcome", sa.String(16)),
        sa.Column("reason", sa.String(200)),
    )
    op.create_table(
        "supply_original",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("channel_id", sa.String(40), nullable=False),
        sa.Column("channel_doc_id", sa.String(200), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("media_type", sa.String(120), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("meta", postgresql.JSONB(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("purge_after", sa.DateTime(timezone=True)),
        sa.Column("purged_at", sa.DateTime(timezone=True)),
        sa.Column("knowbase_material_id", sa.String(64)),
        sa.Column("knowbase_version_no", sa.Integer()),
    )


def downgrade() -> None:
    op.drop_table("supply_original")
    op.drop_table("supply_attempt")
    op.drop_index("uq_supply_job_open", table_name="supply_job")
    op.drop_table("supply_job")
    op.drop_index("ix_supply_demand_item_job", table_name="supply_demand_item")
    op.drop_table("supply_demand_item")
    op.drop_table("supply_demand")
    op.drop_table("supply_channel")
