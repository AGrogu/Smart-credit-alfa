"""Track real-time session inactivity separately from demonstration clocks."""

from alembic import op
import sqlalchemy as sa

revision = "003_session_idle"
down_revision = "002_credit_cycle"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sessions", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE sessions SET last_seen_at = CURRENT_TIMESTAMP")


def downgrade():
    op.drop_column("sessions", "last_seen_at")
