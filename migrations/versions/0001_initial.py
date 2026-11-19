"""Create managed campaigns, offer assignments and drafts."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "managed_campaigns",
        sa.Column("keitaro_id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("geo_stream_id", sa.Integer(), nullable=False),
        sa.Column("offer_stream_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("geo_stream_id <> offer_stream_id", name="ck_distinct_streams"),
    )
    op.create_table(
        "offer_assignments",
        sa.Column(
            "campaign_id",
            sa.Integer(),
            sa.ForeignKey("managed_campaigns.keitaro_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("offer_id", sa.Integer(), primary_key=True),
        sa.Column("offer_name", sa.String(), nullable=False),
        sa.Column("published_share", sa.Integer(), nullable=True),
        sa.Column("pinned_share", sa.Integer(), nullable=True),
        sa.CheckConstraint("published_share BETWEEN 0 AND 100", name="ck_published_share"),
        sa.CheckConstraint("pinned_share BETWEEN 0 AND 100", name="ck_pinned_share"),
    )
    op.create_table(
        "offer_drafts",
        sa.Column(
            "campaign_id",
            sa.Integer(),
            sa.ForeignKey("managed_campaigns.keitaro_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("base_offers", sa.JSON(), nullable=False),
        sa.Column("desired_offers", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("offer_drafts")
    op.drop_table("offer_assignments")
    op.drop_table("managed_campaigns")
