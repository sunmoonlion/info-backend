"""One logical delivery per version/target/dataset, without rewriting old records."""

import json

import sqlalchemy as sa
from alembic import op

from app.infrastructure.repositories.distribution_identity_v1 import (
    audit_distribution_identity_v1,
)

revision = "20260913_0009"
down_revision = "20260912_0008"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_context().as_sql:
        raise RuntimeError(
            "distribution identity migration requires an online data audit"
        )
    connection = op.get_bind()
    connection.execute(
        sa.text("LOCK TABLE distribution_record IN SHARE ROW EXCLUSIVE MODE")
    )
    report = audit_distribution_identity_v1(connection)
    if not report["ready"]:
        raise RuntimeError(
            "distribution_identity_preflight_failed: " + json.dumps(report)
        )
    op.create_index(
        "uq_distribution_logical_delivery_v1",
        "distribution_record",
        [
            "document_version_id",
            "target_app",
            sa.text("coalesce(nullif(target_dataset, ''), 'default')"),
        ],
        unique=True,
    )
    # No server default remains: old INSERT code fails even on a previously
    # unused version/dataset. This marker is a write fence, not payload validation.
    op.add_column(
        "distribution_record",
        sa.Column(
            "write_protocol_version",
            sa.SmallInteger(),
            nullable=False,
            server_default="1",
        ),
    )
    op.alter_column(
        "distribution_record", "write_protocol_version", server_default=None
    )
    op.create_check_constraint(
        "ck_distribution_write_protocol",
        "distribution_record",
        "write_protocol_version=1",
    )
    op.execute("""
        CREATE FUNCTION info_guard_distribution_identity_v1() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            IF NEW.document_id IS DISTINCT FROM OLD.document_id
               OR NEW.document_version_id IS DISTINCT FROM OLD.document_version_id
               OR NEW.target_app IS DISTINCT FROM OLD.target_app
               OR NEW.target_dataset IS DISTINCT FROM OLD.target_dataset
               OR NEW.content_hash IS DISTINCT FROM OLD.content_hash THEN
                RAISE EXCEPTION 'distribution_identity_is_immutable' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER distribution_identity_immutable
        BEFORE UPDATE OF document_id,document_version_id,target_app,target_dataset,content_hash
        ON distribution_record FOR EACH ROW EXECUTE FUNCTION info_guard_distribution_identity_v1()
    """)


def downgrade():
    op.execute("DROP TRIGGER distribution_identity_immutable ON distribution_record")
    op.execute("DROP FUNCTION info_guard_distribution_identity_v1()")
    op.drop_constraint(
        "ck_distribution_write_protocol", "distribution_record", type_="check"
    )
    op.drop_column("distribution_record", "write_protocol_version")
    op.drop_index(
        "uq_distribution_logical_delivery_v1", table_name="distribution_record"
    )
