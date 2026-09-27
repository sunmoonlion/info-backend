"""Record the outcome of registering a security dataset with the knowledge service."""

import sqlalchemy as sa
from alembic import op

revision = "20260927_0012"
down_revision = "20260927_0011"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "security_dataset",
        sa.Column("knowledge_registered_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "security_dataset",
        sa.Column("knowledge_registration_error", sa.String(80)),
    )


def downgrade():
    op.drop_column("security_dataset", "knowledge_registration_error")
    op.drop_column("security_dataset", "knowledge_registered_at")
