"""Security ingestion batches: one row per batch, one row per archived request."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260927_0010"
down_revision = "20260913_0009"
branch_labels = None
depends_on = None


def _common():
    return (
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuid_generate_v4()"),
        ),
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
    )


def upgrade():
    op.create_table(
        "security_ingestion",
        *_common(),
        sa.Column("security_code", sa.String(6), nullable=False),
        sa.Column("market", sa.String(2), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "sources",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column(
            "summary",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("error_code", sa.String(120)),
        sa.Column("error_detail", sa.Text()),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name="ck_security_ingestion_status",
        ),
        sa.CheckConstraint(
            "security_code ~ '^[0-9]{6}$'", name="ck_security_ingestion_code"
        ),
    )
    op.create_index(
        "ix_security_ingestion_code_requested",
        "security_ingestion",
        ["security_code", "requested_at"],
    )
    op.create_table(
        "security_ingestion_item",
        *_common(),
        sa.Column(
            "ingestion_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("security_ingestion.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("source_code", sa.String(120), nullable=False),
        sa.Column("kind", sa.String(50), nullable=False),
        sa.Column(
            "crawl_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("crawl_job.id"),
            nullable=False,
        ),
        sa.Column(
            "raw_artifact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("raw_artifact.id"),
            nullable=False,
        ),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "reused", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("http_status", sa.Integer(), nullable=False),
        sa.Column(
            "meta",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.UniqueConstraint(
            "ingestion_id", "seq", name="uq_security_ingestion_item_seq"
        ),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'", name="ck_security_ingestion_item_sha256"
        ),
    )
    op.create_index(
        "ix_security_ingestion_item_sha256", "security_ingestion_item", ["sha256"]
    )


def downgrade():
    op.drop_index(
        "ix_security_ingestion_item_sha256", table_name="security_ingestion_item"
    )
    op.drop_table("security_ingestion_item")
    op.drop_index(
        "ix_security_ingestion_code_requested", table_name="security_ingestion"
    )
    op.drop_table("security_ingestion")
