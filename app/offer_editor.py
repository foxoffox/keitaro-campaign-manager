import asyncio
import json
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, TypeAdapter, model_validator
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .keitaro import CatalogItem, KeitaroClient, KeitaroError, StreamOffer, TrackerStream
from .models import ManagedCampaign, OfferAssignment, OfferDraft


class CampaignChoice(BaseModel):
    id: int
    name: str | None


class OfferRow(BaseModel):
    offer_id: PositiveInt
    name: str
    state: Literal["active", "disabled", "removed"]
    share: int = Field(ge=0)
    pinned_share: int | None = Field(default=None, ge=0, le=100)


class DraftView(BaseModel):
    revision: str
    rows: list[OfferRow]
    published_rows: list[OfferRow]
    has_draft: bool
    upstream_changed: bool
    total: int
    warning: str | None
    can_publish: bool
    publication_pending: bool


class EditorView(BaseModel):
    campaign_name: str
    campaign_state: str
    geo_stream_id: int
    offer_stream_id: int
    countries: list[str]
    catalog: list[CatalogItem]
    draft: DraftView


class DraftRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


class DraftAction(DraftRevision):
    action: Literal["add", "remove", "restore", "pin", "unpin", "cancel"]
    offer_id: PositiveInt | None = None

    @model_validator(mode="after")
    def check_target(self):
        if (self.action == "cancel") != (self.offer_id is None):
            raise ValueError("Укажи оффер для действия со строкой")
        return self


offer_rows = TypeAdapter(list[OfferRow])


@dataclass(frozen=True)
class Publication:
    stream_id: int
    base_snapshot: dict
    base_weights: list[tuple[int, str, int]]
    offers: list[StreamOffer]


@contextmanager
def _transaction(engine: Engine):
    with Session(engine) as db, db.begin():
        if engine.dialect.name == "sqlite":
            # Lock before loading the revision so two tabs cannot overwrite each other.
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        yield db


def _managed(db: Session, campaign_id: int, *, lock: bool = False) -> ManagedCampaign:
    campaign = db.get(ManagedCampaign, campaign_id, with_for_update=lock)
    if campaign is None:
        raise HTTPException(404, "Эта кампания не создана через обёртку")
    return campaign


def campaign_choices(engine: Engine) -> list[CampaignChoice]:
    with Session(engine) as db:
        rows = db.execute(
            select(ManagedCampaign.keitaro_id, ManagedCampaign.name)
            .order_by(ManagedCampaign.created_at.desc(), ManagedCampaign.keitaro_id.desc())
        )
        return [CampaignChoice(id=campaign_id, name=name) for campaign_id, name in rows]


def _registered_streams(engine: Engine, campaign_id: int) -> tuple[int, int]:
    with Session(engine) as db:
        campaign = _managed(db, campaign_id)
        return campaign.geo_stream_id, campaign.offer_stream_id


def _assignments(db: Session, campaign_id: int) -> dict[int, OfferAssignment]:
    rows = db.scalars(select(OfferAssignment).where(OfferAssignment.campaign_id == campaign_id))
    return {row.offer_id: row for row in rows}


def _published(assignments: dict[int, OfferAssignment]) -> list[OfferRow]:
    return [
        OfferRow(
            offer_id=row.offer_id,
            name=row.offer_name,
            share=row.published_share or 0,
            state=row.published_state or ("removed" if row.published_share is None else "active"),
            pinned_share=row.pinned_share,
        )
        for _, row in sorted(assignments.items())
    ]


def _traffic_weights(rows: list[OfferRow]) -> list[tuple[int, str, int]]:
    return [(row.offer_id, row.state, row.share) for row in rows if row.state != "removed"]


def _serialize(rows: list[OfferRow]) -> list[dict]:
    return [row.model_dump() for row in rows]


def _view(published: list[OfferRow], draft: OfferDraft | None, stream_snapshot: dict | None) -> DraftView:
    rows = offer_rows.validate_python(draft.desired_offers) if draft is not None else published
    upstream_changed = (
        draft is not None
        and (
            draft.base_stream_snapshot is None or draft.base_stream_snapshot != stream_snapshot
            or _traffic_weights(offer_rows.validate_python(draft.base_offers)) != _traffic_weights(published)
        )
    )
    pending = draft is not None and draft.publication_started
    active = [row for row in rows if row.state == "active"]
    total = sum(row.share for row in active)
    warning = None
    if not active:
        warning = "В потоке нет активных офферов."
    elif total != 100:
        warning = "Сумма долей должна быть 100%. Проверь закреплённые доли."
    elif any(row.share == 0 for row in active):
        warning = "Офферы с долей 0% не получат трафик."
    snapshot = [
        _serialize(published), stream_snapshot, draft.base_offers if draft is not None else None,
        draft.base_stream_snapshot if draft is not None else None, _serialize(rows), pending,
    ]
    revision = sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    return DraftView(
        revision=revision, rows=rows, published_rows=published, has_draft=draft is not None,
        upstream_changed=upstream_changed, total=total, warning=warning,
        can_publish=draft is not None and not pending and not upstream_changed and total == 100,
        publication_pending=pending,
    )


def _recalculate(rows: dict[int, OfferRow]) -> list[OfferRow]:
    active = sorted(offer_id for offer_id, row in rows.items() if row.state == "active")
    free = [offer_id for offer_id in active if rows[offer_id].pinned_share is None]
    pinned = sum(rows[offer_id].pinned_share or 0 for offer_id in active)
    shares = {}
    if free:
        share, remainder = divmod(max(0, 100 - pinned), len(free))
        shares = {offer_id: share + (index < remainder) for index, offer_id in enumerate(free)}
    for offer_id in active:
        row = rows[offer_id]
        share = shares[offer_id] if row.pinned_share is None else row.pinned_share
        rows[offer_id] = row.model_copy(update={"share": share})
    return [rows[offer_id] for offer_id in sorted(rows)]


def _remote_offers(stream: TrackerStream) -> dict[int, StreamOffer]:
    remote = {offer.offer_id: offer for offer in stream.offers}
    if len(remote) != len(stream.offers) or any(
        offer.state not in ("active", "disabled") or not 0 <= offer.share <= 100
        for offer in remote.values()
    ):
        raise KeitaroError("Keitaro вернул неожиданные настройки офферов")
    return remote


def _stream_snapshot(stream: TrackerStream) -> dict:
    return {"configuration": stream.configuration_hash, "updated_at": stream.updated_at}


def _stream_weights(offers: list[StreamOffer]) -> list[tuple[int, str, int]]:
    return sorted((offer.offer_id, offer.state, offer.share) for offer in offers)


def _sync(
    engine: Engine,
    campaign_id: int,
    stream: TrackerStream,
    catalog: list[CatalogItem],
    campaign_name: str | None = None,
    confirm_unchanged_offers: bool = False,
) -> DraftView:
    titles = {offer.id: offer.name for offer in catalog}
    remote = _remote_offers(stream)
    snapshot = _stream_snapshot(stream)
    with _transaction(engine) as db:
        managed = _managed(db, campaign_id, lock=True)
        if stream.id != managed.offer_stream_id:
            raise HTTPException(409, "Поток офферов изменился. Обнови страницу.")
        if campaign_name is not None:
            managed.name = campaign_name
        managed.offer_stream_snapshot = snapshot
        assignments = _assignments(db, campaign_id)
        for offer_id, row in assignments.items():
            if offer_id not in remote:
                row.published_share = row.published_state = None
        for offer_id, saved in remote.items():
            row = assignments.get(offer_id)
            if row is None:
                row = OfferAssignment(
                    campaign_id=campaign_id, offer_id=offer_id,
                    offer_name=titles.get(offer_id, f"Оффер №{offer_id}"),
                )
                assignments[offer_id] = row
                db.add(row)
            row.offer_name = titles.get(offer_id, row.offer_name)
            row.published_share, row.published_state = saved.share, saved.state
            if saved.state == "active" and row.pinned_share != saved.share:
                row.pinned_share = None

        draft = db.get(OfferDraft, campaign_id)
        if draft is not None and draft.publication_started:
            wanted = offer_rows.validate_python(draft.desired_offers)
            base = offer_rows.validate_python(draft.base_offers)
            if (
                draft.base_stream_snapshot
                and draft.base_stream_snapshot["configuration"] == stream.configuration_hash
                and _traffic_weights(wanted) == _stream_weights(stream.offers)
                and (confirm_unchanged_offers or _traffic_weights(base) != _traffic_weights(wanted))
            ):
                for entry in wanted:
                    row = assignments.get(entry.offer_id)
                    if row is None:
                        row = OfferAssignment(
                            campaign_id=campaign_id, offer_id=entry.offer_id, offer_name=entry.name,
                        )
                        assignments[entry.offer_id] = row
                        db.add(row)
                    if entry.offer_id not in titles:
                        row.offer_name = entry.name
                    row.pinned_share = entry.pinned_share
                db.delete(draft)
                draft = None
            else:
                draft.publication_started = False
        return _view(_published(assignments), draft, snapshot)


async def load_editor(engine: Engine, keitaro: KeitaroClient, campaign_id: int) -> EditorView:
    geo_id, offer_id = await run_in_threadpool(_registered_streams, engine, campaign_id)
    campaign, flows, catalog = await asyncio.gather(
        keitaro.get_campaign(campaign_id), keitaro.get_streams(campaign_id), keitaro.get_offers(),
    )
    streams = {flow.id: flow for flow in flows if flow.campaign_id == campaign_id}
    geo, offer = streams.get(geo_id), streams.get(offer_id)
    if campaign.id != campaign_id or geo is None or offer is None or offer.flow_schema != "landings":
        raise HTTPException(409, "Ожидаемые потоки кампании не найдены в Keitaro")
    draft = await run_in_threadpool(_sync, engine, campaign_id, offer, catalog, campaign.name)
    countries = [
        country
        for rule in geo.filters if rule.name == "country" and rule.mode == "accept" and isinstance(rule.payload, list)
        for country in rule.payload if isinstance(country, str)
    ]
    return EditorView(
        campaign_name=campaign.name, campaign_state=campaign.state,
        geo_stream_id=geo_id, offer_stream_id=offer_id, countries=countries,
        catalog=[item for item in catalog if item.state in (None, "active")], draft=draft,
    )


def _edit(
    engine: Engine, campaign_id: int, command: DraftAction, available: CatalogItem | None,
) -> DraftView:
    with _transaction(engine) as db:
        managed = _managed(db, campaign_id, lock=True)
        snapshot = managed.offer_stream_snapshot
        published = _published(_assignments(db, campaign_id))
        draft = db.get(OfferDraft, campaign_id)
        current = _view(published, draft, snapshot)
        if command.revision != current.revision:
            raise HTTPException(409, "Черновик изменился. Обнови редактор и повтори действие.")
        if current.publication_pending:
            raise HTTPException(409, "Сначала обнови состояние последней публикации из Keitaro.")
        if command.action == "cancel":
            if draft is not None:
                db.delete(draft)
            return _view(published, None, snapshot)
        if current.upstream_changed:
            raise HTTPException(409, "Поток уже изменился в Keitaro. Отмени черновик или обнови редактор.")

        targets = {row.offer_id: row for row in current.rows}
        offer_id = command.offer_id
        row = targets.get(offer_id)
        match command.action:
            case "add" if row is None and available is not None:
                targets[offer_id] = OfferRow(offer_id=offer_id, name=available.name, state="active", share=0)
            case "restore" if row is not None and row.state == "removed" and available is not None:
                targets[offer_id] = row.model_copy(update={"state": "active", "name": available.name})
            case "remove" if row is not None and row.state != "removed":
                targets[offer_id] = row.model_copy(update={"state": "removed", "share": 0})
            case "pin" if row is not None and row.state == "active" and row.pinned_share is None:
                targets[offer_id] = row.model_copy(update={"pinned_share": row.share})
            case "unpin" if row is not None and row.state == "active" and row.pinned_share is not None:
                targets[offer_id] = row.model_copy(update={"pinned_share": None})
            case _:
                raise HTTPException(422, "Действие недоступно для этого оффера")

        updated = list(targets.values()) if command.action == "pin" else _recalculate(targets)
        if updated == published:
            if draft is not None:
                db.delete(draft)
            return _view(published, None, snapshot)
        if draft is None:
            draft = OfferDraft(
                campaign_id=campaign_id, base_offers=_serialize(published),
                base_stream_snapshot=snapshot, publication_started=False,
            )
            db.add(draft)
        draft.desired_offers = _serialize(updated)
        return _view(published, draft, snapshot)


async def edit_draft(
    engine: Engine, keitaro: KeitaroClient | None, campaign_id: int, command: DraftAction,
) -> DraftView:
    available = None
    if command.action in ("add", "restore"):
        await run_in_threadpool(_registered_streams, engine, campaign_id)
        if keitaro is None:
            raise HTTPException(503, "Keitaro API не настроен")
        available = next((
            item for item in await keitaro.get_offers()
            if item.id == command.offer_id and item.state in (None, "active")
        ), None)
        if available is None:
            raise HTTPException(422, "Оффер недоступен в справочнике Keitaro")
    return await run_in_threadpool(_edit, engine, campaign_id, command, available)


def _publication(
    engine: Engine, campaign_id: int, revision: str, *, start: bool = False,
) -> Publication:
    with _transaction(engine) as db:
        managed = _managed(db, campaign_id, lock=True)
        draft = db.get(OfferDraft, campaign_id)
        current = _view(_published(_assignments(db, campaign_id)), draft, managed.offer_stream_snapshot)
        if current.revision != revision:
            raise HTTPException(409, "Черновик изменился. Обнови редактор и повтори действие.")
        if draft is None:
            raise HTTPException(409, "Нет черновика для публикации")
        if current.publication_pending:
            raise HTTPException(409, "Сначала обнови состояние последней публикации из Keitaro.")
        if current.upstream_changed:
            raise HTTPException(409, "Исходное состояние потока изменилось. Обнови редактор.")
        if not current.can_publish:
            raise HTTPException(422, "Для публикации нужны активные офферы с суммой долей 100%.")
        if start:
            draft.publication_started = True
        return Publication(
            stream_id=managed.offer_stream_id,
            base_snapshot=draft.base_stream_snapshot,
            base_weights=_traffic_weights(offer_rows.validate_python(draft.base_offers)),
            offers=[
                StreamOffer(offer_id=row.offer_id, state=row.state, share=row.share)
                for row in current.rows if row.state != "removed"
            ],
        )


async def _read_offer_stream(keitaro: KeitaroClient, campaign_id: int, stream_id: int) -> TrackerStream:
    streams = await keitaro.get_streams(campaign_id)
    stream = next((row for row in streams if row.id == stream_id and row.campaign_id == campaign_id), None)
    if stream is None or stream.flow_schema != "landings":
        raise HTTPException(409, "Ожидаемый поток офферов не найден в Keitaro")
    _remote_offers(stream)
    return stream


async def publish_draft(
    engine: Engine, keitaro: KeitaroClient, campaign_id: int, revision: str,
) -> DraftView:
    publication = await run_in_threadpool(_publication, engine, campaign_id, revision)
    base_active = {offer_id for offer_id, state, _ in publication.base_weights if state == "active"}
    new_active = {
        offer.offer_id for offer in publication.offers if offer.state == "active"
    } - base_active
    catalog = await keitaro.get_offers() if new_active else []
    if new_active - {offer.id for offer in catalog if offer.state in (None, "active")}:
        raise HTTPException(422, "Один из офферов черновика недоступен в справочнике Keitaro.")

    current = await _read_offer_stream(keitaro, campaign_id, publication.stream_id)
    if (
        _stream_snapshot(current) != publication.base_snapshot
        or _stream_weights(current.offers) != publication.base_weights
    ):
        await run_in_threadpool(_sync, engine, campaign_id, current, catalog)
        raise HTTPException(409, "Поток изменился в Keitaro. Черновик сохранён. Обнови редактор.")

    await run_in_threadpool(_publication, engine, campaign_id, revision, start=True)
    write_error = None
    try:
        await keitaro.replace_stream_offers(publication.stream_id, publication.offers)
    except KeitaroError as exc:
        write_error = exc

    try:
        saved = await _read_offer_stream(keitaro, campaign_id, publication.stream_id)
    except (KeitaroError, HTTPException) as exc:
        raise KeitaroError(
            "Keitaro не подтвердил публикацию. Черновик сохранён. Обнови состояние из Keitaro."
        ) from exc

    view = await run_in_threadpool(
        _sync, engine, campaign_id, saved, catalog, confirm_unchanged_offers=write_error is None,
    )
    if view.has_draft:
        reason = str(write_error) if write_error is not None else "Состав, доли или настройки потока отличаются от ожидаемых."
        raise KeitaroError(f"{reason} Черновик сохранён. Обнови состояние из Keitaro.")
    return view
