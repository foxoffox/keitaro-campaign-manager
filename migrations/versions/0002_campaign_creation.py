"""Track campaign creation requests to avoid duplicate remote writes."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "campaign_creations",
        sa.Column("request_id", sa.String(36), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=True),
        sa.Column("stage", sa.String(32), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("campaign_creations")
