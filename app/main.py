from asyncio import Lock
from contextlib import asynccontextmanager
from pathlib import Path
from weakref import WeakValueDictionary

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from .campaigns import CampaignInput, CreatedCampaign, create_managed_campaign
from .config import get_settings
from .database import open_database
from .keitaro import Catalogs, KeitaroClient, KeitaroError, KeitaroTimeout
from .models import ManagedCampaign
from .offer_editor import (
    CampaignChoice, DraftAction, DraftRevision, DraftView, EditorView, campaign_choices,
    edit_draft, load_editor, publish_draft,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    engine = open_database(settings.database_url)
    app.state.database = engine
    app.state.keitaro = None
    app.state.editor_locks = WeakValueDictionary()
    try:
        if settings.keitaro_base_url and settings.keitaro_api_key:
            base_url = str(settings.keitaro_base_url).rstrip("/")
            async with httpx.AsyncClient(
                base_url=f"{base_url}/admin_api/v1/",
                headers={
                    "Accept": "application/json",
                    "Api-Key": settings.keitaro_api_key.get_secret_value(),
                },
                timeout=10,
                follow_redirects=False,
            ) as client:
                app.state.keitaro = KeitaroClient(client)
                yield
        else:
            yield
    finally:
        engine.dispose()


app = FastAPI(title="Keitaro Campaign Manager", lifespan=lifespan)
static_dir = Path(__file__).with_name("static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    return FileResponse(static_dir / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/editor", include_in_schema=False)
def editor_page() -> FileResponse:
    return FileResponse(static_dir / "editor.html", headers={"Cache-Control": "no-cache"})


@app.exception_handler(KeitaroError)
async def handle_keitaro_error(_request: Request, error: KeitaroError) -> JSONResponse:
    return JSONResponse(
        {"detail": str(error)}, status_code=504 if isinstance(error, KeitaroTimeout) else 502,
    )


@app.exception_handler(SQLAlchemyError)
async def handle_database_error(_request: Request, _error: SQLAlchemyError) -> JSONResponse:
    return JSONResponse({"detail": "Локальная база недоступна. Проверь миграции."}, status_code=503)


@app.get("/health")
def health(request: Request) -> dict[str, str]:
    try:
        with request.app.state.database.connect() as connection:
            connection.execute(select(ManagedCampaign.keitaro_id).limit(1))
    except SQLAlchemyError:
        raise HTTPException(503, "Database unavailable or migrations not applied") from None
    return {"status": "ok"}


@app.get("/api/keitaro/catalogs")
async def keitaro_catalogs(request: Request) -> Catalogs:
    keitaro = request.app.state.keitaro
    if keitaro is None:
        raise HTTPException(503, "Keitaro API is not configured")
    return await keitaro.catalogs()


@app.post("/api/campaigns", status_code=201)
async def create_campaign(payload: CampaignInput, request: Request) -> CreatedCampaign:
    keitaro = request.app.state.keitaro
    if keitaro is None:
        raise HTTPException(503, "Keitaro API is not configured")
    return await create_managed_campaign(request.app.state.database, keitaro, payload)


@app.get("/api/editor/campaigns")
def editor_campaigns(request: Request) -> list[CampaignChoice]:
    return campaign_choices(request.app.state.database)


@app.get("/api/editor/campaigns/{campaign_id}")
async def campaign_offers(campaign_id: int, request: Request) -> EditorView:
    keitaro = request.app.state.keitaro
    if keitaro is None:
        raise HTTPException(503, "Keitaro API не настроен")
    async with request.app.state.editor_locks.setdefault(campaign_id, Lock()):
        return await load_editor(request.app.state.database, keitaro, campaign_id)


@app.post("/api/editor/campaigns/{campaign_id}/draft")
async def change_offer_draft(campaign_id: int, command: DraftAction, request: Request) -> DraftView:
    async with request.app.state.editor_locks.setdefault(campaign_id, Lock()):
        return await edit_draft(request.app.state.database, request.app.state.keitaro, campaign_id, command)


@app.post("/api/editor/campaigns/{campaign_id}/publish")
async def publish_offer_draft(campaign_id: int, command: DraftRevision, request: Request) -> DraftView:
    keitaro = request.app.state.keitaro
    if keitaro is None:
        raise HTTPException(503, "Keitaro API не настроен")
    async with request.app.state.editor_locks.setdefault(campaign_id, Lock()):
        return await publish_draft(request.app.state.database, keitaro, campaign_id, command.revision)
