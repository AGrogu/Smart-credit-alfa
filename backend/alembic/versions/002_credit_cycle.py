"""Add independent versioned credit-cycle aggregates without modifying legacy data."""

from alembic import op
import sqlalchemy as sa

revision = "002_credit_cycle"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "credit_v2_records",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("organization_id", sa.String(100)),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_credit_v2_records_kind", "credit_v2_records", ["kind"])
    op.create_index("ix_credit_v2_records_organization_id", "credit_v2_records", ["organization_id"])
    op.create_index("ix_credit_v2_org_kind", "credit_v2_records", ["organization_id", "kind"])
    op.create_table(
        "credit_v2_blobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("application_id", sa.String(100), sa.ForeignKey("credit_v2_records.id"), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
    )
    op.create_index("ix_credit_v2_blobs_application_id", "credit_v2_blobs", ["application_id"])


def downgrade():
    op.drop_table("credit_v2_blobs")
    op.drop_table("credit_v2_records")
