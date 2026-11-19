import asyncio
import json
from hashlib import sha256

import httpx
from pydantic import BaseModel, Field, JsonValue, TypeAdapter, ValidationError, model_validator


class CatalogItem(BaseModel):
    id: int
    name: str
    state: str | None = Field(default=None, exclude=True)


class Catalogs(BaseModel):
    domains: list[CatalogItem]
    groups: list[CatalogItem]
    traffic_sources: list[CatalogItem]
    offers: list[CatalogItem]


class KeitaroError(Exception):
    pass


class KeitaroTimeout(KeitaroError):
    pass


class EntityId(BaseModel):
    id: int


class TrackerCampaign(EntityId):
    alias: str
    name: str
    state: str
    type: str
    domain_id: int
    group_id: int
    traffic_source_id: int


class StreamFilter(BaseModel):
    name: str
    mode: str
    payload: JsonValue


class StreamOffer(BaseModel):
    offer_id: int
    share: int
    state: str


class TrackerStream(EntityId):
    campaign_id: int
    type: str
    flow_schema: str = Field(validation_alias="schema")
    position: int
    state: str
    action_type: str
    action_payload: JsonValue = None
    filters: list[StreamFilter]
    landings: list[dict]
    offers: list[StreamOffer]
    updated_at: str | None = None
    configuration_hash: str

    @model_validator(mode="before")
    @classmethod
    def fingerprint_configuration(cls, data):
        if not isinstance(data, dict):
            return data
        configuration = {
            key: value for key, value in data.items()
            if key not in {"offers", "updated_at", "configuration_hash"}
        }
        fingerprint = sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest()
        return {**data, "configuration_hash": fingerprint}


catalog_items = TypeAdapter(list[CatalogItem])
entity_id = TypeAdapter(EntityId)
tracker_campaign = TypeAdapter(TrackerCampaign)
tracker_streams = TypeAdapter(list[TrackerStream])


class KeitaroClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def catalogs(self) -> Catalogs:
        domains, groups, sources, offers = await asyncio.gather(
            self._get_catalog("domains"),
            self._get_catalog("groups", params={"type": "campaigns"}),
            self._get_catalog("traffic_sources"),
            self.get_offers(),
        )
        return Catalogs(domains=domains, groups=groups, traffic_sources=sources, offers=offers)

    async def get_offers(self) -> list[CatalogItem]:
        return await self._get_catalog("offers")

    async def _get_catalog(
        self, endpoint: str, *, params: dict[str, str] | None = None
    ) -> list[CatalogItem]:
        return await self._request("GET", endpoint, catalog_items, params=params)

    async def create_campaign(self, payload: dict) -> int:
        return (await self._request("POST", "campaigns", entity_id, payload=payload)).id

    async def get_campaign(self, campaign_id: int) -> TrackerCampaign:
        return await self._request("GET", f"campaigns/{campaign_id}", tracker_campaign)

    async def create_stream(self, payload: dict) -> int:
        return (await self._request("POST", "streams", entity_id, payload=payload)).id

    async def get_streams(self, campaign_id: int) -> list[TrackerStream]:
        return await self._request("GET", f"campaigns/{campaign_id}/streams", tracker_streams)

    async def replace_stream_offers(self, stream_id: int, offers: list[StreamOffer]) -> None:
        await self._send(
            "PUT", f"streams/{stream_id}", payload={"offers": [offer.model_dump() for offer in offers]},
        )

    async def enable_campaign(self, campaign_id: int) -> None:
        await self._send("POST", f"campaigns/{campaign_id}/enable")

    async def enable_stream(self, stream_id: int) -> None:
        await self._send("POST", f"streams/{stream_id}/enable")

    async def _request[T](
        self,
        method: str,
        endpoint: str,
        parser: TypeAdapter[T],
        *,
        params: dict[str, str] | None = None,
        payload: dict | None = None,
    ) -> T:
        response = await self._send(method, endpoint, params=params, payload=payload)
        try:
            return parser.validate_json(response.content)
        except ValidationError as exc:
            raise KeitaroError("Keitaro returned an unexpected response") from exc

    async def _send(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, str] | None = None,
        payload: dict | None = None,
    ) -> httpx.Response:
        try:
            response = await self.client.request(method, endpoint, params=params, json=payload)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise KeitaroTimeout("Keitaro did not respond in time") from exc
        except httpx.HTTPStatusError as exc:
            raise KeitaroError(f"Keitaro returned HTTP {exc.response.status_code}") from exc
        except httpx.RequestError as exc:
            raise KeitaroError("Could not reach Keitaro") from exc
        return response
