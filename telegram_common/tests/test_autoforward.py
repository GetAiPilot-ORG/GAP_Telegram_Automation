import asyncio
from datetime import datetime, timezone
import importlib.util
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from uuid import uuid4
from unittest.mock import AsyncMock, patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import httpx
from fastapi import FastAPI, Header, HTTPException
from telethon import types
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from telegram_common import SessionEncryption
from telegram_common.autoforward import AutoForwardLogin, AutoForwardClients, LeasedClient
from telegram_common.database_session import DatabaseSession, encrypt_snapshot, decrypt_snapshot
from telegram_common.encryption import EncryptionError
from telegram_common.login import LoginError, credential_context
from telegram_common.login_links import AutoForwardLoginLinks
from telegram_common.migrate_autoforward import migrate_one
from telegram_common.forward_delivery import forwarding_event, deliver, request_random_id, reconcile_once, initialize_sources
from telegram_common.tests.test_durable_login import PostgresStore, FakeTelegram, literal
from telegram_common.tests import test_login_links


class AutoStore(PostgresStore):
    async def rpc(self, name, params):
        name = name.replace("tg_login_", "tg_autoforward_login_").replace("tg_submanager_revoke", "tg_autoforward_revoke")
        if any(isinstance(value, list) for value in params.values()):
            def value_sql(value):
                return "ARRAY[" + ",".join(str(int(v)) for v in value) + "]::bigint[]" if isinstance(value, list) else literal(value)
            args = ",".join(f"{key}=>{value_sql(value)}" for key,value in params.items())
            return await asyncio.to_thread(self.sql, f"SELECT public.{name}({args})")
        return await super().rpc(name, params)

    async def credential(self, owner):
        return self.sql(f"SELECT to_jsonb(c) FROM public.tg_session_credentials c WHERE dashboard_owner_id={literal(owner)} AND purpose='autoforward'")

    async def telegram_credential(self, telegram):
        return self.sql(f"SELECT to_jsonb(c) FROM public.tg_session_credentials c WHERE telegram_owner_id={telegram} AND purpose='autoforward'")

    async def attempt(self, owner, attempt):
        return self.sql(f"SELECT to_jsonb(c) FROM public.tg_autoforward_login_attempts c WHERE dashboard_owner_id={literal(owner)} AND id={literal(attempt)}")

    async def checkpoint(self, credential):
        return self.sql(f"SELECT to_jsonb(c) FROM public.tg_autoforward_checkpoints c WHERE credential_id={literal(credential)}")

    async def pending_deliveries(self, credential):
        return self.sql(f"SELECT coalesce(jsonb_agg(to_jsonb(c)), '[]'::jsonb) FROM public.tg_autoforward_deliveries c WHERE credential_id={literal(credential)} AND state IN('pending','sending')")

    async def source_cursor(self, credential, source):
        return self.sql(f"SELECT to_json(last_message_id) FROM public.tg_autoforward_sources WHERE credential_id={literal(credential)} AND source_id={source} AND NOT needs_reset")

    async def source_replay_since(self, credential, source):
        return self.sql(f"SELECT to_json(replay_since) FROM public.tg_autoforward_sources WHERE credential_id={literal(credential)} AND source_id={source}")

    async def event_staged(self, credential, source, message):
        return self.sql(f"SELECT to_json(EXISTS(SELECT 1 FROM public.tg_autoforward_deliveries WHERE credential_id={literal(credential)} AND source_id={source} AND message_id={message}))")


class DatabaseSessionTests(unittest.TestCase):
    def row(self):
        return dict(id=str(uuid4()), principal_kind="user", purpose="autoforward", dashboard_owner_id=str(uuid4()), telegram_owner_id=123456789)

    def session(self):
        session = DatabaseSession()
        self.addCleanup(session.dispose)
        session.set_dc(2, "149.154.167.51", 443)
        session.auth_key = AuthKey(os.urandom(256))
        session.process_entities([types.User(id=1001, access_hash=987654321, first_name="Dummy"),
            types.Channel(id=2002, title="Private", photo=types.ChatPhotoEmpty(), date=datetime.now(timezone.utc), access_hash=7654321, broadcast=True)])
        session.set_update_state(0, types.updates.State(pts=25, qts=3, date=datetime.now(timezone.utc), seq=8, unread_count=0))
        return session

    def test_encrypted_checkpoint_preserves_auth_entities_and_updates(self):
        cipher = SessionEncryption({"v1": AESGCM.generate_key(bit_length=256)}, "v1")
        row, original = self.row(), self.session()
        encrypted = encrypt_snapshot(cipher, original.snapshot(), row)
        restored = DatabaseSession(snapshot=decrypt_snapshot(cipher, encrypted, row))
        self.addCleanup(restored.dispose)
        self.assertEqual(restored.auth_key.key, original.auth_key.key)
        self.assertEqual(restored.get_input_entity(1001).access_hash, 987654321)
        self.assertEqual(restored.get_input_entity(-1000000002002).access_hash, 7654321)
        self.assertEqual(restored.get_update_state(0).pts, 25)
        with self.assertRaises(EncryptionError):
            decrypt_snapshot(cipher, encrypted, {**row, "telegram_owner_id": 999})
        with self.assertRaises(EncryptionError):
            decrypt_snapshot(cipher, encrypted[:-8] + "AAAAAA==", row)

    def test_version_seven_upgrades_only_in_memory(self):
        original = self.session()
        original._conn.execute("ALTER TABLE sessions DROP COLUMN tmp_auth_key")
        original._conn.execute("UPDATE version SET version=7")
        data = original.snapshot()
        restored = DatabaseSession(snapshot=data)
        self.addCleanup(restored.dispose)
        self.assertEqual(restored.get_input_entity(1001).access_hash, 987654321)
        self.assertEqual(restored._conn.execute("SELECT version FROM version").fetchone()[0], 8)

    def test_expired_lease_blocks_rpc_before_network(self):
        client = LeasedClient(self.session(), 123, "dummy")
        with self.assertRaises(LoginError):
            client.assert_owned()


@unittest.skipUnless(os.environ.get("TELEGRAM_TEST_PG_SOCKET", "").startswith("/tmp/tg-session-test-"), "Requires disposable PostgreSQL")
class AutoForwardTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        test_login_links.LinkDatabaseTests.setUpClass()
        cls.store = AutoStore()
        migration = Path(__file__).resolve().parents[3] / "bot-dashboard/getaipilot.in/supabase/migrations/20261003003000_autoforward_durable_sessions.sql"
        if not cls.store.sql("SELECT to_json(to_regclass('public.tg_autoforward_login_attempts') IS NOT NULL)"):
            cls.store.sql(migration.read_text())

    async def asyncSetUp(self):
        self.owner, self.other = str(uuid4()), str(uuid4())
        self.tg = 123456789
        self.store.sql(f"INSERT INTO auth.users(id) VALUES ({literal(self.owner)}),({literal(self.other)}); INSERT INTO public.profiles VALUES ({literal(self.owner)},{self.tg});")
        self.cipher = SessionEncryption({"v1": AESGCM.generate_key(bit_length=256)}, "v1")
        self.telegram = FakeTelegram()
        self.links = AutoForwardLoginLinks(self.store, "https://getaipilot.in/autoforward/login")
        self.login = AutoForwardLogin(self.store, self.cipher, 123, "dummy", self.telegram.client)
        self.grant = await self.new_grant()

    async def new_grant(self):
        token = (await self.links.issue(self.tg))["url"].split("#login_token=")[1]
        return await self.links.redeem(self.owner, token)

    async def asyncTearDown(self):
        owner = literal(self.owner)
        for table in ('tg_autoforward_deliveries', 'tg_autoforward_sources', 'tg_autoforward_checkpoints', 'tg_autoforward_leases'):
            self.store.sql(f"DELETE FROM public.{table} WHERE credential_id IN(SELECT id FROM public.tg_session_credentials WHERE dashboard_owner_id={owner})")
        self.store.sql(f"DELETE FROM public.tg_autoforward_login_attempts WHERE dashboard_owner_id={owner}; DELETE FROM public.tg_session_credentials WHERE dashboard_owner_id={owner}; DELETE FROM public.tg_autoforward_login_links WHERE dashboard_owner_id={owner}; DELETE FROM public.profiles WHERE id={owner}; DELETE FROM auth.users WHERE id IN({owner},{literal(self.other)});")

    async def start(self):
        return await self.login.start(self.owner, "+919876543210", self.grant["id"])

    async def connected(self):
        start = await self.start()
        await self.login.verify(self.owner, start["attempt_id"], "otp", "12345")
        return await self.store.credential(self.owner)

    async def test_otp_password_restart_and_separate_purpose(self):
        self.telegram.needs_password = True
        start = await self.start()
        self.assertEqual((await self.login.verify(self.owner, start["attempt_id"], "otp", "12345"))["status"], "needs_password")
        restarted = AutoForwardLogin(self.store, self.cipher, 123, "dummy", self.telegram.client)
        self.assertEqual((await restarted.resume(self.owner, start["attempt_id"]))["step"], "password")
        self.assertEqual((await restarted.verify(self.owner, start["attempt_id"], "password", "dummy-password"))["status"], "connected")
        row = await self.store.credential(self.owner)
        self.assertEqual(row["telegram_owner_id"], self.tg)
        self.assertEqual(row["purpose"], "autoforward")
        self.assertTrue(self.cipher.decrypt(row["encrypted_session"], credential_context(row)))
        self.assertFalse(self.store.sql(f"SELECT to_json(EXISTS(SELECT 1 FROM public.tg_session_credentials WHERE dashboard_owner_id={literal(self.owner)} AND purpose='submanager'))"))

    async def test_grant_owner_expiry_and_supersession(self):
        with self.assertRaises(Exception):
            await self.login.start(self.other, "+919876543210", self.grant["id"])
        old = self.grant
        self.grant = await self.new_grant()
        with self.assertRaises(Exception):
            await self.login.start(self.owner, "+919876543210", old["id"])
        self.assertEqual(self.telegram.otp_calls, 0)
        await self.start()

    async def test_wrong_telegram_identity_never_saves_credential(self):
        start = await self.start()
        factory = self.telegram.client
        def wrong(value):
            client = factory(value)
            client.get_me = AsyncMock(return_value=SimpleNamespace(id=999, phone="919876543210", bot=False))
            return client
        self.login.client_factory = wrong
        with self.assertRaises(LoginError):
            await self.login.verify(self.owner, start["attempt_id"], "otp", "12345")
        self.assertIsNone(await self.store.credential(self.owner))

    async def test_leases_fence_checkpoint_and_relogin(self):
        row = await self.connected()
        first = await self.store.rpc("tg_autoforward_lease_claim", {"p_telegram": self.tg, "p_worker": str(uuid4())})
        with self.assertRaises(LoginError):
            await self.store.rpc("tg_autoforward_lease_claim", {"p_telegram": self.tg, "p_worker": str(uuid4())})
        with self.assertRaises(LoginError):
            await self.start()
        session = DatabaseSession(self.cipher.decrypt(row["encrypted_session"], credential_context(row)))
        try:
            payload = encrypt_snapshot(self.cipher, session.snapshot(), row)
        finally:
            session.dispose()
        params = {"p_credential": row["id"], "p_worker": first["lease"]["worker_id"], "p_fence": first["lease"]["fence"], "p_snapshot": payload, "p_key": "v1"}
        await self.store.rpc("tg_autoforward_checkpoint", params)
        self.assertIsNotNone(await self.store.checkpoint(row["id"]))
        await self.store.rpc("tg_autoforward_lease_release", {key:params[key] for key in ("p_credential","p_worker","p_fence")})
        second = await self.store.rpc("tg_autoforward_lease_claim", {"p_telegram": self.tg, "p_worker": str(uuid4())})
        self.assertGreater(second["lease"]["fence"], first["lease"]["fence"])
        with self.assertRaises(RuntimeError):
            await self.store.rpc("tg_autoforward_checkpoint", params)

    async def test_logout_preserves_credential_on_failure(self):
        await self.connected()
        self.telegram.logout_fails = True
        with self.assertRaises(LoginError):
            await self.login.logout(self.owner)
        row = await self.store.credential(self.owner)
        self.assertEqual(row["state"], "revoking")
        self.assertTrue(row["encrypted_session"])
        self.telegram.logout_fails = False
        await self.login.logout(self.owner)
        self.assertEqual((await self.store.credential(self.owner))["state"], "revoked")

    async def test_runner_disconnects_after_lease_loss_and_preserves_entities(self):
        await self.connected()
        def factory(session, *args, **kwargs):
            client = self.telegram.client(StringSession.save(session))
            client.session = session
            client._disconnect_underlying = client.disconnect
            client.assert_owned = lambda: LeasedClient.assert_owned(client)
            client.owner_manager = None
            return client
        loss = []
        manager = AutoForwardClients(self.store, self.cipher, 123, "dummy", factory, loss.append)
        async def disconnect(client):
            await client._disconnect_underlying()
        with patch("telegram_common.autoforward.TelegramClient.disconnect", new=disconnect):
            client = await manager.get(self.tg)
            self.assertIs(await manager.get(self.tg), client)
            state = manager.clients[self.tg]
            state["closing"] = True
            with self.assertRaises(LoginError):
                await manager.get(self.tg)
            state["closing"] = False
            state["task"].cancel()
            await asyncio.gather(state["task"], return_exceptions=True)
            state["task"] = None
            client.session.process_entities([types.User(id=1111, access_hash=5555, first_name="Dummy")])
            await manager._checkpoint(state)
            saved = await self.store.checkpoint(state["row"]["credential"]["id"])
            restored = DatabaseSession(snapshot=decrypt_snapshot(self.cipher, saved["encrypted_snapshot"], state["row"]["credential"]))
            try:
                self.assertEqual(restored.get_input_entity(1111).access_hash, 5555)
            finally:
                restored.dispose()
            manager._checkpoint = AsyncMock(side_effect=OSError("dummy database outage"))
            with patch("telegram_common.autoforward.asyncio.sleep", new=AsyncMock()):
                await manager._heartbeat(self.tg, state)
            self.assertFalse(client.is_connected())
            self.assertEqual(loss, [self.tg])
            self.assertFalse(manager.clients)
            with self.assertRaises(LoginError):
                client.assert_owned()

    async def test_file_import_is_verified_idempotent_and_non_destructive(self):
        seed = StringSession()
        seed.set_dc(2, "149.154.167.51", 443)
        seed.auth_key = AuthKey(os.urandom(256))
        session = DatabaseSession(StringSession.save(seed))
        try:
            data = session.snapshot()
        finally:
            session.dispose()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / f"{self.tg}_919876543210.session"
            path.write_bytes(data)
            def factory(snapshot):
                client = self.telegram.client(StringSession.save(snapshot))
                client.session = snapshot
                original = client.connect
                async def connect():
                    await original()
                    client.record.update(authorized=True, phone="919876543210")
                client.connect = connect
                return client
            result = await migrate_one(self.store, self.cipher, path, self.owner, self.tg, factory)
            self.assertFalse(result["already_imported"])
            self.assertEqual(path.read_bytes(), data)
            second = await migrate_one(self.store, self.cipher, path, self.owner, self.tg, factory)
            self.assertTrue(second["already_imported"])

    async def test_http_auth_owner_and_disabled_flag(self):
        from GetaipilotBackEnd1.autoforward_api import build_router
        async def auth(authorization=Header(None)):
            if not authorization:
                raise HTTPException(401)
            return {"user": SimpleNamespace(id=authorization)}
        app = FastAPI()
        app.include_router(build_router(auth, lambda:(self.links,self.login)))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            self.assertEqual((await client.get("/autoforward/status")).status_code,401)
            with patch.dict(os.environ,{"AUTOFORWARD_WEB_LOGIN":"false"}):
                self.assertEqual((await client.get("/autoforward/status",headers={"Authorization":self.owner})).status_code,404)
            with patch.dict(os.environ,{"AUTOFORWARD_WEB_LOGIN":"true"}):
                response=await client.post("/autoforward/login/start",headers={"Authorization":self.owner},json={"phone":"+919876543210","link_id":self.grant["id"]})
                self.assertEqual(response.status_code,200)
                attempt=response.json()["attempt_id"]
                self.assertEqual((await client.post("/autoforward/login/otp",headers={"Authorization":self.other},json={"attempt_id":attempt,"value":"12345"})).status_code,409)
                self.assertEqual((await client.post("/autoforward/login/otp",headers={"Authorization":self.owner},json={"attempt_id":attempt,"value":"12345"})).status_code,200)

    async def forwarding_client(self):
        row = await self.connected()
        worker = str(uuid4())
        lease = await self.store.rpc("tg_autoforward_lease_claim", {"p_telegram": self.tg, "p_worker": worker})
        session = DatabaseSession(self.cipher.decrypt(row["encrypted_session"], credential_context(row)))
        self.addCleanup(session.dispose)
        client = LeasedClient(session, 123, "dummy")
        client.lease_deadline = time.monotonic() + 40
        client.owner_manager = SimpleNamespace(store=self.store, worker_id=worker, clients={self.tg:{"client":client,"row":lease}})
        return client

    async def test_retry_uses_same_random_id_and_sent_receipts_skip_duplicate(self):
        client = await self.forwarding_client()
        sent = []
        async def send():
            sent.append(request_random_id())
            return "dummy-message"
        for _ in range(2):
            async with forwarding_event(client, -100111, 10, [9]):
                await deliver(client, 9, send)
                await deliver(client, 9, send)  # Poll + separate caption.
        self.assertEqual(len(sent), 2)
        self.assertNotEqual(sent[0], sent[1])

    async def test_delivery_commit_failure_replays_with_telegram_deduplication(self):
        client = await self.forwarding_client()
        row = client.owner_manager.clients[self.tg]["row"]
        params = dict(p_credential=row["credential"]["id"], p_worker=client.owner_manager.worker_id, p_fence=row["lease"]["fence"])
        await self.store.rpc("tg_autoforward_source_cursor", {**params,"p_source":-100111,"p_message":100})
        delivered = set()
        async def send():
            key = request_random_id()
            if key in delivered:
                from telethon import errors
                raise errors.RandomIdDuplicateError(request=None)
            delivered.add(key)
        original = self.store.rpc
        failed = False
        async def fail_completion(name, values):
            nonlocal failed
            if name == "tg_autoforward_delivery_end" and values.get("p_ok") and not failed:
                failed = True
                raise OSError("dummy completion failure")
            return await original(name, values)
        self.store.rpc = fail_completion
        with self.assertRaises(OSError):
            async with forwarding_event(client, -100111, 101, [9]):
                await deliver(client, 9, send)
        self.store.rpc = original
        await self.store.rpc("tg_autoforward_lease_release", params)
        client.owner_manager.worker_id = str(uuid4())
        new = await self.store.rpc("tg_autoforward_lease_claim", {"p_telegram":self.tg,"p_worker":client.owner_manager.worker_id})
        client.owner_manager.clients[self.tg]["row"] = new
        async with forwarding_event(client, -100111, 101, [9]):
            await deliver(client, 9, send)
        self.assertEqual(len(delivered), 1)
        self.assertFalse(await self.store.pending_deliveries(row["credential"]["id"]))

    async def test_source_scan_recovers_missed_messages_once(self):
        client = await self.forwarding_client()
        state = client.owner_manager.clients[self.tg]["row"]
        params = dict(p_credential=state["credential"]["id"],p_worker=client.owner_manager.worker_id,p_fence=state["lease"]["fence"])
        await self.store.rpc("tg_autoforward_source_cursor", {**params,"p_source":-100111,"p_message":100})
        messages = [SimpleNamespace(id=101,message="Dummy",media=None), SimpleNamespace(id=102,message="Dummy",media=None)]
        async def iterator(source, min_id, **kwargs):
            for message in messages:
                if message.id>min_id:
                    yield message
        client.iter_messages = iterator
        sent=[]
        async def handler(event):
            async with forwarding_event(client,event.chat_id,event.message.id,[9]):
                async def send():sent.append(event.message.id)
                await deliver(client,9,send)
        await reconcile_once(client,{-100111:[9]},handler)
        await reconcile_once(client,{-100111:[9]},handler)
        self.assertEqual(sent,[101,102])
        self.assertEqual(await self.store.source_cursor(state["credential"]["id"],-100111),102)

    async def test_pause_resets_baseline_and_cancels_filtered_jobs(self):
        client=await self.forwarding_client()
        state=client.owner_manager.clients[self.tg]["row"]
        params=dict(p_credential=state["credential"]["id"],p_worker=client.owner_manager.worker_id,p_fence=state["lease"]["fence"])
        await self.store.rpc("tg_autoforward_source_cursor",{**params,"p_source":-100111,"p_message":100})
        async with forwarding_event(client,-100111,101,[9]):
            pass  # Completely filtered text: no message must be sent later.
        self.assertFalse(await self.store.pending_deliveries(state["credential"]["id"]))
        await self.store.rpc("tg_autoforward_pause",params)
        client.get_messages=AsyncMock(return_value=[SimpleNamespace(id=200)])
        await initialize_sources(client,{-100111:[9]})
        self.assertEqual(await self.store.source_cursor(state["credential"]["id"],-100111),200)

    async def test_random_id_is_applied_to_native_telegram_request(self):
        client=await self.forwarding_client()
        from telethon.tl.functions.messages import SendMessageRequest
        request=SendMessageRequest(types.InputPeerUser(9,10),"Dummy",random_id=1)
        parent=AsyncMock(return_value="dummy")
        async with forwarding_event(client,-100111,101,[9]):
            async def send():
                with patch("telegram_common.autoforward.TelegramClient._call",new=parent):
                    await client._call(None,request)
                self.assertEqual(request.random_id,request_random_id())
            await deliver(client,9,send)
        self.assertNotEqual(request.random_id,1)


if __name__ == "__main__":
    unittest.main()
