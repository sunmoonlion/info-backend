"""Security datasets built from an ingestion batch; version is decided by content."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260927_0011"
down_revision = "20260927_0010"
branch_labels = None
depends_on = None


def _jsonb(name: str) -> sa.Column:
    return sa.Column(
        name, postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
    )


def upgrade():
    op.create_table(
        "security_dataset",
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
        sa.Column("security_code", sa.String(6), nullable=False),
        sa.Column("dataset_id", sa.String(120), nullable=False),
        sa.Column("data_version", sa.String(160), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column(
            "ingestion_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("security_ingestion.id"),
            nullable=False,
        ),
        sa.Column("bucket", sa.String(255), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("version_id", sa.String(255)),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        _jsonb("row_counts"),
        _jsonb("quality"),
        _jsonb("metadata_json"),
        sa.Column("start_date", sa.String(10), nullable=False),
        sa.Column("end_date", sa.String(10), nullable=False),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "security_code", "data_version", name="uq_security_dataset_version"
        ),
        sa.CheckConstraint(
            "status IN ('published', 'quality_failed')",
            name="ck_security_dataset_status",
        ),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'", name="ck_security_dataset_sha256"
        ),
    )
    op.create_index(
        "ix_security_dataset_code_built", "security_dataset", ["security_code", "built_at"]
    )


def downgrade():
    op.drop_index("ix_security_dataset_code_built", table_name="security_dataset")
    op.drop_table("security_dataset")
