"""Add a derived canonical identity without merging/rekeying historical documents."""

import json
from itertools import batched

import sqlalchemy as sa
from alembic import op

from app.domain.info_identity_v1 import (
    audit_identity_rows,
    identity_key_v1,
)
from app.infrastructure.repositories.info_identity_v1 import iter_identity_rows_v1

revision = "20260912_0008"
down_revision = "20260911_0007"
branch_labels = None
depends_on = None


def upgrade():
    if op.get_context().as_sql:
        raise RuntimeError("canonical identity migration requires an online data audit")
    connection = op.get_bind()
    # Audit and backfill see a fixed writer set. Deployment must drain writers;
    # the migration lock is a correctness guard, not an online cutover strategy.
    connection.execute(sa.text("LOCK TABLE info_document IN SHARE ROW EXCLUSIVE MODE"))
    report = audit_identity_rows(iter_identity_rows_v1(connection))
    if not report["ready"]:
        raise RuntimeError("canonical_identity_preflight_failed: " + json.dumps(report))
    op.add_column("info_document", sa.Column("canonical_identity", sa.String(64), nullable=True))
    for batch in batched(iter_identity_rows_v1(connection), 500):
        values = [{"id": row.id, "key": identity_key_v1(row.canonical_url)} for row in batch]
        connection.execute(sa.text(
            "UPDATE info_document SET canonical_identity=:key WHERE id=:id"
        ), values)
    op.create_unique_constraint("uq_info_document_canonical_identity", "info_document", ["canonical_identity"])
    op.create_check_constraint(
        "ck_info_document_canonical_identity", "info_document",
        "(canonical_url IS NULL AND canonical_identity IS NULL) OR "
        "(canonical_url IS NOT NULL AND canonical_identity IS NOT NULL "
        "AND canonical_identity ~ '^[0-9a-f]{64}$')",
    )
    op.execute("""
        CREATE FUNCTION info_guard_canonical_identity_v1() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            IF NEW.canonical_url IS DISTINCT FROM OLD.canonical_url
               OR NEW.canonical_identity IS DISTINCT FROM OLD.canonical_identity THEN
                RAISE EXCEPTION 'canonical_identity_is_immutable' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER info_document_identity_immutable
        BEFORE UPDATE OF canonical_url,canonical_identity ON info_document
        FOR EACH ROW EXECUTE FUNCTION info_guard_canonical_identity_v1()
    """)
    # A new-only Python default plus a required storage protocol field fences
    # old version INSERTs, including those for an already existing document.
    op.add_column("info_document_version", sa.Column(
        "write_protocol_version", sa.SmallInteger(), nullable=False, server_default="1"
    ))
    op.alter_column("info_document_version", "write_protocol_version", server_default=None)
    op.create_check_constraint("ck_info_version_write_protocol", "info_document_version", "write_protocol_version=1")


def downgrade():
    op.drop_constraint("ck_info_version_write_protocol", "info_document_version", type_="check")
    op.drop_column("info_document_version", "write_protocol_version")
    op.execute("DROP TRIGGER info_document_identity_immutable ON info_document")
    op.execute("DROP FUNCTION info_guard_canonical_identity_v1()")
    op.drop_constraint("ck_info_document_canonical_identity", "info_document", type_="check")
    op.drop_constraint("uq_info_document_canonical_identity", "info_document", type_="unique")
    op.drop_column("info_document", "canonical_identity")
