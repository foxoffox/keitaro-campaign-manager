"""Store campaign names and distinguish disabled offer links from removed links."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("managed_campaigns", sa.Column("name", sa.String(), nullable=True))
    op.add_column("offer_assignments", sa.Column("published_state", sa.String(8), nullable=True))
    op.execute("UPDATE offer_assignments SET published_state = 'active' WHERE published_share IS NOT NULL")


def downgrade() -> None:
    op.drop_column("offer_assignments", "published_state")
    op.drop_column("managed_campaigns", "name")
