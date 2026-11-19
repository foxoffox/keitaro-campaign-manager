"""Record the stream snapshot and unconfirmed publication attempts."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("managed_campaigns", sa.Column("offer_stream_snapshot", sa.JSON(), nullable=True))
    op.add_column("offer_drafts", sa.Column("base_stream_snapshot", sa.JSON(), nullable=True))
    op.add_column(
        "offer_drafts", sa.Column("publication_started", sa.Boolean(), nullable=False, server_default=sa.false())
    )


def downgrade() -> None:
    op.drop_column("offer_drafts", "publication_started")
    op.drop_column("offer_drafts", "base_stream_snapshot")
    op.drop_column("managed_campaigns", "offer_stream_snapshot")
