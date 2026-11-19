import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase

import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.database import Base, open_database
from app.keitaro import KeitaroClient, KeitaroError
from app.models import ManagedCampaign
from app.offer_editor import DraftAction, campaign_choices, edit_draft, load_editor, publish_draft


class OfferEditorTest(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(directory.cleanup)
        database = Path(directory.name, "editor.sqlite3").as_posix()
        self.engine = open_database(f"sqlite:///{database}")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as db, db.begin():
            db.add(ManagedCampaign(keitaro_id=42, geo_stream_id=1, offer_stream_id=2))

        self.remote = [{"offer_id": 10, "share": 100, "state": "active"}]
        self.paths = []
        self.writes = []
        self.comments = ""
        self.fail_readback = False
        client = httpx.AsyncClient(
            base_url="https://tracker.example/", transport=httpx.MockTransport(self.keitaro_response),
        )
        self.addAsyncCleanup(client.aclose)
        self.tracker = KeitaroClient(client)
        self.state = (await load_editor(self.engine, self.tracker, 42)).draft

    def keitaro_response(self, request):
        path = request.url.path
        if request.method == "PUT":
            self.assertEqual(path, "/streams/2")
            payload = json.loads(request.content)
            self.assertEqual(set(payload), {"offers"})
            self.writes.append(payload["offers"])
            self.remote = payload["offers"]
            return httpx.Response(200)
        self.assertEqual(request.method, "GET")
        self.paths.append(path)
        if path == "/campaigns/42/streams" and self.writes and self.fail_readback:
            self.fail_readback = False
            return httpx.Response(503)
        campaign = {
            "id": 42, "alias": "test", "name": "Offer QA", "state": "disabled",
            "type": "position", "domain_id": 1, "group_id": 1, "traffic_source_id": 1,
        }
        shared = {
            "campaign_id": 42, "state": "active", "action_type": "http",
            "filters": [], "offers": [], "landings": [],
        }
        responses = {
            "/campaigns/42": campaign,
            "/campaigns/42/streams": [
                {**shared, "id": 1, "type": "regular", "schema": "redirect", "position": 1},
                {**shared, "id": 2, "type": "default", "schema": "landings", "position": 2,
                 "offers": self.remote, "comments": self.comments},
            ],
            "/offers": [{"id": id, "name": f"Offer {id}"} for id in (10, 20, 30, 40)],
        }
        return httpx.Response(200, json=responses[path])

    async def edit(self, action, offer_id=None, *, revision=None):
        command = DraftAction(
            action=action, offer_id=offer_id, revision=revision or self.state.revision,
        )
        self.state = await edit_draft(self.engine, self.tracker, 42, command)

    def shares(self):
        return {row.offer_id: row.share for row in self.state.rows}

    async def test_draft_add_remove_restore_pin_and_cancel(self):
        self.assertEqual(campaign_choices(self.engine)[0].name, "Offer QA")
        await self.edit("add", 20)
        self.assertEqual(self.shares(), {10: 50, 20: 50})
        self.assertEqual({row.offer_id: row.share for row in self.state.published_rows}, {10: 100})
        for offer_id in (30, 40):
            await self.edit("add", offer_id)
        self.assertEqual(self.shares(), {10: 25, 20: 25, 30: 25, 40: 25})
        await self.edit("pin", 10)
        stale = self.state.revision
        await self.edit("remove", 20)
        self.assertEqual(self.shares(), {10: 25, 20: 0, 30: 38, 40: 37})
        self.assertEqual(self.state.rows[1].state, "removed")
        self.assertEqual(self.state.rows[0].pinned_share, 25)
        self.assertEqual((await load_editor(self.engine, self.tracker, 42)).draft, self.state)
        with self.assertRaises(HTTPException) as conflict:
            await self.edit("remove", 40, revision=stale)
        self.assertEqual(conflict.exception.status_code, 409)
        await self.edit("restore", 20)
        self.assertEqual(self.shares(), {10: 25, 20: 25, 30: 25, 40: 25})
        await self.edit("cancel")
        self.assertEqual(self.shares(), {10: 100})
        self.assertFalse(self.state.has_draft)

    async def test_keep_archive_and_detect_external_change(self):
        await self.edit("pin", 10)
        await self.edit("add", 20)
        self.assertEqual(self.shares(), {10: 100, 20: 0})
        self.assertIsNotNone(self.state.warning)
        self.remote.clear()
        self.state = (await load_editor(self.engine, self.tracker, 42)).draft
        self.assertTrue(self.state.upstream_changed)
        with self.assertRaises(HTTPException) as conflict:
            await self.edit("remove", 20)
        self.assertEqual(conflict.exception.status_code, 409)
        await self.edit("cancel")
        self.assertEqual(self.state.rows[0].state, "removed")
        self.assertEqual(self.state.total, 0)
        await self.edit("restore", 10)
        self.assertEqual(self.shares(), {10: 100})

    async def test_refuse_unmanaged_campaign_without_contacting_keitaro(self):
        self.paths.clear()
        with self.assertRaises(HTTPException) as missing:
            await load_editor(self.engine, self.tracker, 50)
        self.assertEqual(missing.exception.status_code, 404)
        self.assertFalse(self.paths)

    async def test_publish_replaces_only_links_and_retains_pins_and_history(self):
        await self.edit("add", 20)
        await self.edit("pin", 10)
        self.state = await publish_draft(self.engine, self.tracker, 42, self.state.revision)
        self.assertFalse(self.state.has_draft)
        self.assertEqual(self.shares(), {10: 50, 20: 50})
        self.assertEqual(self.state.rows[0].pinned_share, 50)
        self.assertEqual(self.state.published_rows, self.state.rows)
        await self.edit("unpin", 10)
        await self.edit("remove", 20)
        self.state = await publish_draft(self.engine, self.tracker, 42, self.state.revision)
        self.assertEqual(self.remote, [{"offer_id": 10, "state": "active", "share": 100}])
        self.assertEqual(self.state.rows[1].state, "removed")
        self.assertFalse(self.state.has_draft)
        self.assertEqual((await load_editor(self.engine, self.tracker, 42)).draft, self.state)

    async def test_publish_refuses_an_external_stream_setting_change(self):
        await self.edit("add", 20)
        self.comments = "Edited manually"
        with self.assertRaises(HTTPException) as conflict:
            await publish_draft(self.engine, self.tracker, 42, self.state.revision)
        self.assertEqual(conflict.exception.status_code, 409)
        self.assertFalse(self.writes)
        changed = (await load_editor(self.engine, self.tracker, 42)).draft
        self.assertTrue(changed.has_draft)
        self.assertTrue(changed.upstream_changed)

    async def test_publish_refuses_an_external_offer_change_even_if_it_matches_the_draft(self):
        await self.edit("add", 20)
        self.remote = [
            {"offer_id": 10, "share": 50, "state": "active"},
            {"offer_id": 20, "share": 50, "state": "active"},
        ]
        with self.assertRaises(HTTPException) as conflict:
            await publish_draft(self.engine, self.tracker, 42, self.state.revision)
        self.assertEqual(conflict.exception.status_code, 409)
        self.assertFalse(self.writes)
        changed = (await load_editor(self.engine, self.tracker, 42)).draft
        self.assertTrue(changed.has_draft)
        self.assertTrue(changed.upstream_changed)

    async def test_refresh_recovers_after_unconfirmed_write_without_repeating_it(self):
        await self.edit("add", 20)
        self.fail_readback = True
        with self.assertRaises(KeitaroError):
            await publish_draft(self.engine, self.tracker, 42, self.state.revision)
        self.state = (await load_editor(self.engine, self.tracker, 42)).draft
        self.assertEqual(len(self.writes), 1)
        self.assertFalse(self.state.has_draft)
        self.assertEqual(self.shares(), {10: 50, 20: 50})

    async def test_publish_rejects_an_empty_flow(self):
        await self.edit("remove", 10)
        self.assertFalse(self.state.can_publish)
        with self.assertRaises(HTTPException) as empty:
            await publish_draft(self.engine, self.tracker, 42, self.state.revision)
        self.assertEqual(empty.exception.status_code, 422)
        self.assertFalse(self.writes)
