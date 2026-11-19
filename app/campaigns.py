from hashlib import sha256
from typing import Annotated

import pycountry
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, PositiveInt, StringConstraints, UUID4, field_validator
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .keitaro import KeitaroClient, KeitaroError, KeitaroTimeout, StreamFilter, StreamOffer, TrackerCampaign
from .models import CampaignCreation, ManagedCampaign, OfferAssignment


GOOGLE_URL = "https://www.google.com/"


class CampaignInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID4
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
    country: str
    domain_id: PositiveInt
    group_id: PositiveInt
    traffic_source_id: PositiveInt
    offer_id: PositiveInt

    @field_validator("country")
    @classmethod
    def normalize_country(cls, country: str) -> str:
        country = country.strip().upper()
        if pycountry.countries.get(alpha_2=country) is None:
            raise ValueError("Укажи двухбуквенный код страны ISO 3166-1")
        return country


class CreatedCampaign(BaseModel):
    id: int
    alias: str
    geo_stream_id: int
    offer_stream_id: int


def _campaign_result(campaign: ManagedCampaign, alias: str) -> CreatedCampaign:
    return CreatedCampaign(
        id=campaign.keitaro_id,
        alias=alias,
        geo_stream_id=campaign.geo_stream_id,
        offer_stream_id=campaign.offer_stream_id,
    )


def _previous_result(
    db: Session, previous: CampaignCreation, fingerprint: str, alias: str
) -> CreatedCampaign:
    if previous.fingerprint != fingerprint:
        raise HTTPException(409, "Идентификатор запроса уже использован с другими параметрами")
    if previous.stage == "complete":
        campaign = db.get(ManagedCampaign, previous.campaign_id)
        if campaign:
            return _campaign_result(campaign, alias)
    raise HTTPException(
        409,
        {
            "message": "Этот запрос уже отправлен. Проверь кампанию в Keitaro перед новой попыткой.",
            "stage": previous.stage,
            "campaign_id": previous.campaign_id,
            "alias": alias,
        },
    )


def _check_previous(
    engine: Engine, key: str, fingerprint: str, alias: str
) -> CreatedCampaign | None:
    with Session(engine) as db:
        previous = db.get(CampaignCreation, key)
        if previous:
            return _previous_result(db, previous, fingerprint, alias)
    return None


def _reserve(engine: Engine, key: str, fingerprint: str, alias: str) -> CreatedCampaign | None:
    with Session(engine) as db:
        db.add(CampaignCreation(request_id=key, fingerprint=fingerprint, stage="creating"))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            previous = db.get(CampaignCreation, key)
            if previous is None:
                raise
            return _previous_result(db, previous, fingerprint, alias)
    return None


def _update_attempt(engine: Engine, key: str, campaign_id: int, stage: str) -> None:
    with Session(engine) as db:
        attempt = db.get_one(CampaignCreation, key)
        attempt.campaign_id = campaign_id
        attempt.stage = stage
        db.commit()


def _register(
    engine: Engine, result: CreatedCampaign, payload: CampaignInput, offer_name: str, key: str
) -> None:
    with Session(engine) as db, db.begin():
        db.add(ManagedCampaign(
            keitaro_id=result.id,
            geo_stream_id=result.geo_stream_id,
            offer_stream_id=result.offer_stream_id,
            name=payload.name,
        ))
        db.flush()
        db.add(OfferAssignment(
            campaign_id=result.id, offer_id=payload.offer_id, offer_name=offer_name,
            published_share=100, published_state="active",
        ))
        db.get_one(CampaignCreation, key).stage = "enabling"


def _check_campaign(
    saved: TrackerCampaign, payload: CampaignInput, alias: str, state: str, campaign_id: int
) -> None:
    if (
        saved.id != campaign_id or saved.alias != alias or saved.name != payload.name
        or saved.state != state or saved.type != "position"
        or saved.domain_id != payload.domain_id or saved.group_id != payload.group_id
        or saved.traffic_source_id != payload.traffic_source_id
    ):
        raise KeitaroError("Keitaro сохранил кампанию с неожиданными настройками")


async def create_managed_campaign(
    engine: Engine, keitaro: KeitaroClient, payload: CampaignInput
) -> CreatedCampaign:
    key = str(payload.request_id)
    alias = f"kc-{payload.request_id.hex}"
    fingerprint = sha256(payload.model_dump_json(exclude={"request_id"}).encode()).hexdigest()
    campaign_id = None
    stage = "validation"
    try:
        previous = await run_in_threadpool(_check_previous, engine, key, fingerprint, alias)
        if previous:
            return previous

        catalogs = await keitaro.catalogs()
        choices = (
            ("domain_id", catalogs.domains, payload.domain_id),
            ("group_id", catalogs.groups, payload.group_id),
            ("traffic_source_id", catalogs.traffic_sources, payload.traffic_source_id),
            ("offer_id", catalogs.offers, payload.offer_id),
        )
        selected = {}
        for field, items, chosen_id in choices:
            item = next((item for item in items if item.id == chosen_id and item.state in (None, "active")), None)
            if item is None:
                raise HTTPException(422, [{"loc": ["body", field], "msg": "Значение недоступно в Keitaro"}])
            selected[field] = item

        previous = await run_in_threadpool(_reserve, engine, key, fingerprint, alias)
        if previous:
            return previous
        stage = "campaign"
        campaign_id = await keitaro.create_campaign({
            "alias": alias, "name": payload.name, "state": "disabled", "type": "position",
            "domain_id": payload.domain_id, "group_id": payload.group_id,
            "traffic_source_id": payload.traffic_source_id,
        })
        await run_in_threadpool(_update_attempt, engine, key, campaign_id, "configuring")
        _check_campaign(await keitaro.get_campaign(campaign_id), payload, alias, "disabled", campaign_id)

        geo_filter = StreamFilter(name="country", mode="accept", payload=[payload.country])
        initial_offer = StreamOffer(offer_id=payload.offer_id, share=100, state="active")
        stage = "geo_stream"
        geo_stream_id = await keitaro.create_stream({
            "campaign_id": campaign_id, "name": f"{payload.country} → Google",
            "type": "regular", "schema": "redirect", "action_type": "http",
            "action_payload": GOOGLE_URL, "position": 1, "state": "active",
            "filters": [geo_filter.model_dump()],
        })
        stage = "offer_stream"
        offer_stream_id = await keitaro.create_stream({
            "campaign_id": campaign_id, "name": "Остальные GEO → офферы",
            "type": "default", "schema": "landings", "action_type": "http",
            "position": 2, "state": "active", "offers": [initial_offer.model_dump()],
        })

        stage = "stream_activation"
        # Keitaro can report new flows as active before the traffic engine uses them.
        await keitaro.enable_stream(geo_stream_id)
        await keitaro.enable_stream(offer_stream_id)

        stage = "verification"
        streams = {stream.id: stream for stream in await keitaro.get_streams(campaign_id)}
        if geo_stream_id == offer_stream_id or set(streams) != {geo_stream_id, offer_stream_id}:
            raise KeitaroError("Keitaro не вернул два ожидаемых потока")
        geo, offer = streams[geo_stream_id], streams[offer_stream_id]
        if (
            geo.campaign_id != campaign_id or offer.campaign_id != campaign_id
            or geo.state != "active" or offer.state != "active"
            or (geo.type, geo.flow_schema, geo.position) != ("regular", "redirect", 1)
            or (offer.type, offer.flow_schema, offer.position) != ("default", "landings", 2)
            or geo.action_type != "http" or geo.action_payload != GOOGLE_URL
            or geo.filters != [geo_filter] or geo.offers or offer.filters
            or offer.landings or offer.offers != [initial_offer]
        ):
            raise KeitaroError("Keitaro сохранил потоки с неожиданными настройками")

        result = CreatedCampaign(
            id=campaign_id, alias=alias, geo_stream_id=geo_stream_id, offer_stream_id=offer_stream_id,
        )
        stage = "registration"
        await run_in_threadpool(_register, engine, result, payload, selected["offer_id"].name, key)
        stage = "activation"
        await keitaro.enable_campaign(campaign_id)
        _check_campaign(await keitaro.get_campaign(campaign_id), payload, alias, "active", campaign_id)
        await run_in_threadpool(_update_attempt, engine, key, campaign_id, "complete")
        return result
    except (KeitaroError, SQLAlchemyError) as exc:
        if isinstance(exc, SQLAlchemyError):
            status, message = 503, "Не удалось сохранить состояние в локальной БД"
        else:
            status = 504 if isinstance(exc, KeitaroTimeout) else 502
            message = str(exc)
        raise HTTPException(status, {
            "message": message, "stage": stage, "campaign_id": campaign_id, "alias": alias,
        }) from None
