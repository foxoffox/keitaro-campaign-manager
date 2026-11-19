from datetime import datetime

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, Integer, String, false, func
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class ManagedCampaign(Base):
    __tablename__ = "managed_campaigns"
    __table_args__ = (
        CheckConstraint("geo_stream_id <> offer_stream_id", name="ck_distinct_streams"),
    )

    keitaro_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    geo_stream_id: Mapped[int]
    offer_stream_id: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    name: Mapped[str | None] = mapped_column(String)
    offer_stream_snapshot: Mapped[dict | None] = mapped_column(JSON)


class OfferAssignment(Base):
    __tablename__ = "offer_assignments"
    __table_args__ = (
        CheckConstraint("published_share BETWEEN 0 AND 100", name="ck_published_share"),
        CheckConstraint("pinned_share BETWEEN 0 AND 100", name="ck_pinned_share"),
    )

    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("managed_campaigns.keitaro_id", ondelete="CASCADE"), primary_key=True
    )
    offer_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    offer_name: Mapped[str] = mapped_column(String)
    # NULL preserves the local assignment history after unlinking the offer.
    published_share: Mapped[int | None]
    published_state: Mapped[str | None] = mapped_column(String(8))
    pinned_share: Mapped[int | None]


class OfferDraft(Base):
    __tablename__ = "offer_drafts"

    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("managed_campaigns.keitaro_id", ondelete="CASCADE"), primary_key=True
    )
    base_offers: Mapped[list[dict]] = mapped_column(JSON)
    desired_offers: Mapped[list[dict]] = mapped_column(JSON)
    base_stream_snapshot: Mapped[dict | None] = mapped_column(JSON)
    publication_started: Mapped[bool] = mapped_column(server_default=false())


class CampaignCreation(Base):
    __tablename__ = "campaign_creations"

    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    campaign_id: Mapped[int | None]
    stage: Mapped[str] = mapped_column(String(32))
