"""Additive shared schema; each App invokes this from its own linear migration."""

from alembic import op


def upgrade():
    op.execute("""
        CREATE TABLE outbox_dead_letter (
            message_id uuid PRIMARY KEY REFERENCES outbox_message(id),
            error_code varchar(256) NOT NULL,
            failed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            replayed_at timestamptz
        )
    """)
    op.execute("""
        CREATE TABLE outbox_execution (
            resource_key text PRIMARY KEY,
            message_id uuid NOT NULL REFERENCES outbox_message(id),
            owner uuid NOT NULL,
            epoch bigint NOT NULL CHECK (epoch>0),
            expires_at timestamptz NOT NULL
        )
    """)
    op.execute(
        "CREATE INDEX ix_outbox_execution_message ON outbox_execution(message_id)"
    )


def downgrade():
    op.execute("DROP TABLE outbox_execution")
    op.execute("DROP TABLE outbox_dead_letter")
