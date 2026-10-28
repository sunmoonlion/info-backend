"""退证券专线（SDD 0045 第六节第 2 条；decisions ⑰：不留旧版兼容）。

供给（`supply_*`，0016 起）接手之后，旧的证券专线整条退掉：采集申请与申请人、关注清单
与它的流水、写死的采集器跑出的采集批次与条目、批次建出的 SQLite 数据集（连同向 knowbase
的登记）。生产上这几张表只有关注清单的 10 行（加 10 行流水），是 0013 种下的第一批，
名单仍在 0013 里；其余全是 0 行。

- 删表：`security_request_requester`、`security_request`、`security_watchlist_log`、
  `security_watchlist`、`security_dataset`、`security_ingestion_item`、`security_ingestion`
  （`security_ingestion.dataset_build_error` 等列随表而去）；
- 删行：可靠投递（`outbox_message`）里三类旧任务（采集、建库、登记）的记录：处理器
  已删，没跑的也不会再有人认领。

批次条目引用的通用采集记录（`crawl_job`、`raw_artifact`）是通用采集的表，不动。

downgrade 建回 0018 时的空表、索引与约束；删掉的行（含关注清单）不恢复。

Revision ID: 20261028_0019
Revises: 20261028_0018
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20261028_0019"
down_revision = "20261028_0018"
branch_labels = None
depends_on = None

OLD_TOPICS = (
    "knowfeed.security.ingest.v1",
    "knowfeed.security.dataset.build.v1",
    "knowfeed.security.dataset.register.v1",
)
# 先删引用别人的
TABLES = (
    "security_request_requester",
    "security_request",
    "security_watchlist_log",
    "security_watchlist",
    "security_dataset",
    "security_ingestion_item",
    "security_ingestion",
)


def upgrade() -> None:
    topics = ", ".join(f"'{topic}'" for topic in OLD_TOPICS)
    op.execute(f"DELETE FROM outbox_message WHERE topic IN ({topics})")
    for table in TABLES:
        op.drop_table(table)


def _id() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("uuid_generate_v4()"),
    )


def _stamps() -> tuple[sa.Column, sa.Column]:
    return (
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


def _jsonb(name: str, empty: str = "'{}'::jsonb") -> sa.Column:
    return sa.Column(
        name, postgresql.JSONB(), nullable=False, server_default=sa.text(empty)
    )


def downgrade() -> None:
    """建回 0018 时的空表（0010 至 0013 建的样子）；删掉的行不恢复。"""
    op.create_table(
        "security_ingestion",
        _id(),
        *_stamps(),
        sa.Column("security_code", sa.String(6), nullable=False),
        sa.Column("market", sa.String(2), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        _jsonb("sources", "'[]'::jsonb"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        _jsonb("summary"),
        sa.Column("error_code", sa.String(120)),
        sa.Column("error_detail", sa.Text()),
        sa.Column("dataset_build_error", sa.String(80)),
        sa.Column("dataset_build_refused_at", sa.DateTime(timezone=True)),
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
        _id(),
        *_stamps(),
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
        _jsonb("meta"),
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
    op.create_table(
        "security_dataset",
        _id(),
        *_stamps(),
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
        sa.Column("knowledge_registered_at", sa.DateTime(timezone=True)),
        sa.Column("knowledge_registration_error", sa.String(80)),
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
        "ix_security_dataset_code_built",
        "security_dataset",
        ["security_code", "built_at"],
    )
    op.create_table(
        "security_request",
        _id(),
        *_stamps(),
        sa.Column("security_code", sa.String(6), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("open", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "ingestion_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("security_ingestion.id"),
        ),
        sa.Column("decided_by", sa.String(64)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("decision_note", sa.Text()),
        sa.Column("outcome", sa.String(30)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "security_code ~ '^[0-9]{6}$'", name="ck_security_request_code"
        ),
        sa.CheckConstraint(
            "kind IN ('initial', 'refresh')", name="ck_security_request_kind"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'withdrawn')",
            name="ck_security_request_status",
        ),
        sa.CheckConstraint(
            "(open AND outcome IS NULL AND closed_at IS NULL) "
            "OR (NOT open AND outcome IS NOT NULL AND closed_at IS NOT NULL)",
            name="ck_security_request_closed_with_outcome",
        ),
        sa.CheckConstraint(
            "status <> 'approved' OR ingestion_id IS NOT NULL",
            name="ck_security_request_approved_has_ingestion",
        ),
    )
    op.create_index(
        "uq_security_request_open_code",
        "security_request",
        ["security_code"],
        unique=True,
        postgresql_where=sa.text("open"),
    )
    op.create_index(
        "ix_security_request_status_created",
        "security_request",
        ["status", "created_at"],
    )
    op.create_table(
        "security_request_requester",
        _id(),
        *_stamps(),
        sa.Column(
            "request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("security_request.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("source_app", sa.String(32)),
        sa.Column("source_ref", sa.String(128)),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "request_id", "actor_id", name="uq_security_request_requester_actor"
        ),
        sa.CheckConstraint(
            "reason IS NULL OR char_length(reason) <= 500",
            name="ck_security_request_requester_reason",
        ),
    )
    op.create_index(
        "ix_security_request_requester_actor",
        "security_request_requester",
        ["actor_id", "requested_at"],
    )
    op.create_table(
        "security_watchlist",
        sa.Column("security_code", sa.String(6), primary_key=True),
        *_stamps(),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("added_by", sa.String(64), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("removed_at", sa.DateTime(timezone=True)),
        sa.Column("removed_by", sa.String(64)),
        sa.Column("removal_note", sa.Text()),
        sa.CheckConstraint(
            "security_code ~ '^[0-9]{6}$'", name="ck_security_watchlist_code"
        ),
    )
    op.create_table(
        "security_watchlist_log",
        _id(),
        sa.Column("security_code", sa.String(6), nullable=False),
        sa.Column("action", sa.String(10), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("note", sa.Text()),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action IN ('add', 'remove')", name="ck_security_watchlist_log_action"
        ),
    )
    op.create_index(
        "ix_security_watchlist_log_code_at",
        "security_watchlist_log",
        ["security_code", "at"],
    )
