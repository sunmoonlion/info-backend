"""Ingestion requests, their requesters, the watch list, and a record of refused builds."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260929_0013"
down_revision = "20260927_0012"
branch_labels = None
depends_on = None

# 第一批关注清单：所有者 2026-09-28 定的十家。
FIRST_WATCHLIST = (
    "600009",
    "600519",
    "000858",
    "600276",
    "002415",
    "600900",
    "601888",
    "300750",
    "601899",
    "920185",
)
SEEDED_BY = "system:seed-2026-09-28"


def _stamps():
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


def _id():
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("uuid_generate_v4()"),
    )


def upgrade():
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
    # 同一家公司同时只有一个进行中的申请
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
    # 增减都留痕：清单表只有现状，这张表是流水，只追加
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
    for code in FIRST_WATCHLIST:
        op.execute(
            sa.text(
                "INSERT INTO security_watchlist (security_code, added_at, added_by) "
                "VALUES (:code, now(), :by)"
            ).bindparams(code=code, by=SEEDED_BY)
        )
    op.execute(
        sa.text(
            "INSERT INTO security_watchlist_log (security_code, action, actor, at) "
            "SELECT security_code, 'add', added_by, added_at FROM security_watchlist"
        )
    )

    # 建库被拒绝（原文不足以建库）时留个记录：申请的进度要靠它知道「不会再有数据集了」
    op.add_column(
        "security_ingestion", sa.Column("dataset_build_error", sa.String(80))
    )
    op.add_column(
        "security_ingestion",
        sa.Column("dataset_build_refused_at", sa.DateTime(timezone=True)),
    )


def downgrade():
    op.drop_column("security_ingestion", "dataset_build_refused_at")
    op.drop_column("security_ingestion", "dataset_build_error")
    op.drop_index("ix_security_watchlist_log_code_at", "security_watchlist_log")
    op.drop_table("security_watchlist_log")
    op.drop_table("security_watchlist")
    op.drop_index("ix_security_request_requester_actor", "security_request_requester")
    op.drop_table("security_request_requester")
    op.drop_index("ix_security_request_status_created", "security_request")
    op.drop_index("uq_security_request_open_code", "security_request")
    op.drop_table("security_request")
