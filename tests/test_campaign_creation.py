import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from uuid import uuid4

import httpx
from sqlalchemy.orm import Session

from app.campaigns import CampaignInput, create_managed_campaign
from app.database import Base, open_database
from app.keitaro import KeitaroClient
from app.models import ManagedCampaign


class CampaignCreationTest(IsolatedAsyncioTestCase):
    async def test_enables_each_stream_before_enabling_campaign(self):
        campaign = {}
        streams = {}
        enabled_streams = set()
        mutations = []

        def handle(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if request.method == "GET":
                if path == "/campaigns/42":
                    return httpx.Response(200, json=campaign)
                if path == "/campaigns/42/streams":
                    return httpx.Response(200, json=list(streams.values()))
                self.assertIn(path, {"/domains", "/groups", "/traffic_sources", "/offers"})
                return httpx.Response(200, json=[{"id": 1, "name": path}])

            mutations.append(path)
            if path == "/campaigns":
                campaign.update(json.loads(request.content), id=42)
            elif path == "/streams":
                stream_id = 100 + len(streams)
                streams[stream_id] = {"landings": [], "offers": [], "filters": [], **json.loads(request.content)}
                streams[stream_id]["id"] = stream_id
                return httpx.Response(200, json=streams[stream_id])
            elif path.startswith("/streams/"):
                self.assertEqual(campaign["state"], "disabled")
                stream_id = int(path.split("/")[2])
                enabled_streams.add(stream_id)
                return httpx.Response(200, json=[streams[stream_id]])
            else:
                self.assertEqual(path, "/campaigns/42/enable")
                self.assertEqual(enabled_streams, {100, 101})
                campaign["state"] = "active"
                return httpx.Response(200, json=[campaign])
            return httpx.Response(200, json=campaign)

        with TemporaryDirectory(dir=Path.cwd()) as temporary:
            database_path = Path(temporary, "test.sqlite3").as_posix()
            engine = open_database(f"sqlite:///{database_path}")
            try:
                Base.metadata.create_all(engine)
                async with httpx.AsyncClient(
                    base_url="https://tracker.example", transport=httpx.MockTransport(handle)
                ) as client:
                    keitaro = KeitaroClient(client)
                    payload = CampaignInput(
                        request_id=uuid4(), name="Test", country="au",
                        domain_id=1, group_id=1, traffic_source_id=1, offer_id=1,
                    )
                    result = await create_managed_campaign(engine, keitaro, payload)
                    replay = await create_managed_campaign(engine, keitaro, payload)
                with Session(engine) as db:
                    self.assertEqual(db.get_one(ManagedCampaign, result.id).offer_stream_id, 101)
            finally:
                engine.dispose()

        self.assertEqual(result, replay)
        self.assertEqual(mutations, [
            "/campaigns", "/streams", "/streams", "/streams/100/enable",
            "/streams/101/enable", "/campaigns/42/enable",
        ])
