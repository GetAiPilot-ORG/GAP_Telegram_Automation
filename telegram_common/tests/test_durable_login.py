"""Real PostgreSQL claims/transactions, fake Telegram, dummy keys and identities.

Set TELEGRAM_TEST_PG_SOCKET to an isolated /tmp Unix socket and optionally
TELEGRAM_TEST_PG_PORT (55439 default). Apply both migrations first. Tests refuse
TCP/production URLs and never make Telegram or Supabase network requests.
"""

import asyncio
import base64
import json
import importlib.util
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from uuid import uuid4
from unittest.mock import AsyncMock, patch

import httpx

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from telethon import errors
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from telegram_common import SessionEncryption
from telegram_common.login import DurableLogin, LoginError, _STORE_ERRORS, attempt_context, credential_context


def literal(value):
    if value is None:
        return "NULL"
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


class PostgresStore:
    """Test-only adapter: execute the production SQL functions directly."""
    def sql(self, query):
        result = subprocess.run([
            "psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1",
            "-h", os.environ["TELEGRAM_TEST_PG_SOCKET"],
            "-p", os.environ.get("TELEGRAM_TEST_PG_PORT", "55439"),
            "-d", os.environ.get("TELEGRAM_TEST_PG_DATABASE", "postgres"), "-c", query,
        ], capture_output=True, text=True, check=False)
        if result.returncode:
            for marker, mapped in _STORE_ERRORS.items():
                if marker in result.stderr:
                    raise LoginError(**vars(mapped))
            raise RuntimeError(result.stderr)
        output = result.stdout.strip()
        return json.loads(output) if output else None

    async def rpc(self, name, params):
        assert name.startswith("tg_") and name.replace("_", "").isalnum()
        args = ",".join(f"{key} => {literal(value)}" for key, value in params.items())
        return await asyncio.to_thread(self.sql, f"SELECT public.{name}({args})")

    async def credential(self, owner):
        return await asyncio.to_thread(self.sql, "SELECT to_jsonb(t) FROM public.tg_session_credentials t "
            f"WHERE dashboard_owner_id={literal(owner)} AND purpose='submanager'")

    async def attempt(self, owner, attempt_id):
        return await asyncio.to_thread(self.sql, "SELECT to_jsonb(t) FROM public.tg_login_attempts t "
            f"WHERE dashboard_owner_id={literal(owner)} AND id={literal(attempt_id)}")


class FakeTelegram:
    def __init__(self):
        self.sessions = {}
        self.needs_password = False
        self.expired = False
        self.logout_fails = False
        self.otp_calls = 0
        self.live_clients = 0
        self.max_live_clients = 0
        self.pause = None

    def client(self, session):
        server = self

        class Client:
            def __init__(self):
                self.session = StringSession(session)
                self.connected = False

            async def connect(self):
                if not self.session.auth_key:
                    self.session.set_dc(2, "149.154.167.51", 443)
                    self.session.auth_key = AuthKey(os.urandom(256))
                key = base64.b64encode(self.session.auth_key.key).decode()
                self.record = server.sessions.setdefault(key, {"authorized": False})
                self.connected = True
                server.live_clients += 1
                server.max_live_clients = max(server.max_live_clients, server.live_clients)

            async def disconnect(self):
                if self.connected:
                    self.connected = False
                    server.live_clients -= 1

            def is_connected(self):
                return self.connected

            async def send_code_request(self, phone):
                self.record.update(phone=phone, code_hash=str(uuid4()))
                return SimpleNamespace(phone_code_hash=self.record["code_hash"])

            async def is_user_authorized(self):
                return self.record["authorized"]

            async def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
                if password is not None:
                    if not self.record.get("password_step") or password != "dummy-password":
                        raise errors.PasswordHashInvalidError(request=None)
                else:
                    server.otp_calls += 1
                    if server.pause:
                        await server.pause.wait()
                    if server.expired:
                        raise errors.PhoneCodeExpiredError(request=None)
                    if code != "12345" or phone_code_hash != self.record["code_hash"]:
                        raise errors.PhoneCodeInvalidError(request=None)
                    if server.needs_password:
                        self.record["password_step"] = True
                        raise errors.SessionPasswordNeededError(request=None)
                self.record["authorized"] = True

            async def get_me(self):
                if not self.record["authorized"]:
                    return None
                return SimpleNamespace(id=123456789, phone=self.record["phone"].lstrip("+"), bot=False)

            async def log_out(self):
                if server.logout_fails:
                    raise OSError("dummy network failure")
                self.record["authorized"] = False
                return True

        return Client()


@unittest.skipUnless(os.getenv("TELEGRAM_TEST_PG_SOCKET"), "isolated PostgreSQL socket not configured")
class DurableLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        socket = Path(os.environ["TELEGRAM_TEST_PG_SOCKET"]).resolve()
        if not str(socket).startswith("/tmp/tg-session-test-"):
            raise RuntimeError("Tests require a disposable /tmp/tg-session-test-* database")
        self.store = PostgresStore()
        self.owner = str(uuid4())
        self.other = str(uuid4())
        self.store.sql(f"INSERT INTO auth.users(id) VALUES ({literal(self.owner)}),({literal(self.other)})")
        self.encryption = SessionEncryption({"v1": AESGCM.generate_key(bit_length=256)}, "v1")
        self.telegram = FakeTelegram()
        self.manager = self.restart()

    def restart(self):
        return DurableLogin(self.store, self.encryption, self.telegram.client)

    async def asyncTearDown(self):
        self.store.sql(f"DELETE FROM public.tg_login_attempts WHERE dashboard_owner_id IN ({literal(self.owner)},{literal(self.other)})")
        self.store.sql(f"DELETE FROM public.tg_session_credentials WHERE dashboard_owner_id IN ({literal(self.owner)},{literal(self.other)})")
        self.store.sql(f"DELETE FROM auth.users WHERE id IN ({literal(self.owner)},{literal(self.other)})")
        self.assertEqual(self.telegram.live_clients, 0)

    async def start(self):
        return await self.manager.start(self.owner, "+91 98765 43210")

    async def test_restart_before_otp_preserves_auth_key_and_hash(self):
        start = await self.start()
        row = await self.store.attempt(self.owner, start["attempt_id"])
        self.assertNotIn("9876543210", row["encrypted_state"])
        result = await self.restart().verify(self.owner, row["id"], "otp", "12345")
        self.assertEqual(result["status"], "connected")
        credential = await self.store.credential(self.owner)
        decrypted = self.encryption.decrypt(credential["encrypted_session"], credential_context(credential))
        self.assertTrue(StringSession(decrypted).auth_key)
        consumed = await self.store.attempt(self.owner, row["id"])
        self.assertEqual(consumed["step"], "completed")
        self.assertIsNone(consumed["encrypted_state"])
        self.assertEqual(self.telegram.max_live_clients, 1)

    async def test_restart_between_otp_and_password(self):
        self.telegram.needs_password = True
        start = await self.start()
        response = await self.restart().verify(self.owner, start["attempt_id"], "otp", "12345")
        self.assertEqual(response["status"], "needs_password")
        resumed = await self.restart().resume(self.owner, start["attempt_id"])
        self.assertEqual(resumed["step"], "password")
        row = await self.store.attempt(self.owner, start["attempt_id"])
        state = self.encryption.decrypt(row["encrypted_state"], attempt_context(row))
        self.assertNotIn("12345", json.dumps(json.loads(state).get("otp")))
        self.assertNotIn("password", json.loads(state))
        done = await self.restart().verify(self.owner, start["attempt_id"], "password", "dummy-password")
        self.assertEqual(done["status"], "connected")

    async def test_owner_binding_and_stale_code_after_restart(self):
        first = await self.start()
        with self.assertRaises(LoginError) as error:
            await self.manager.verify(self.other, first["attempt_id"], "otp", "12345")
        self.assertEqual(error.exception.code, "attempt_stale")
        second = await self.start()
        self.assertNotEqual(first["attempt_id"], second["attempt_id"])
        with self.assertRaises(LoginError):
            await self.manager.verify(self.owner, first["attempt_id"], "otp", "12345")
        self.assertEqual(self.telegram.otp_calls, 0)

    async def test_invalid_retry_bound_and_expired_code(self):
        start = await self.start()
        for attempt in range(5):
            with self.assertRaises(LoginError) as error:
                await self.manager.verify(self.owner, start["attempt_id"], "otp", "99999")
            self.assertEqual(error.exception.code, "retry_limit" if attempt == 4 else "invalid_code")
        row = await self.store.attempt(self.owner, start["attempt_id"])
        self.assertIsNone(row["encrypted_state"])
        start = await self.start()
        self.telegram.expired = True
        with self.assertRaises(LoginError) as error:
            await self.manager.verify(self.owner, start["attempt_id"], "otp", "12345")
        self.assertTrue(error.exception.restart)
        self.assertEqual(error.exception.code, "code_expired")

    async def test_simultaneous_verification_claims(self):
        start = await self.start()
        claim = str(uuid4())
        await self.store.rpc("tg_login_claim", {"p_owner": self.owner,
            "p_attempt": start["attempt_id"], "p_claim": claim, "p_step": "otp"})
        with self.assertRaises(LoginError) as error:
            await self.restart().verify(self.owner, start["attempt_id"], "otp", "12345")
        self.assertEqual(error.exception.code, "attempt_busy")
        with self.assertRaises(LoginError):
            await self.start()
        self.assertEqual(self.telegram.otp_calls, 0)

    async def test_timeout_disconnects_and_retains_exclusive_claim(self):
        start = await self.start()
        self.telegram.pause = asyncio.Event()
        short_timeout = DurableLogin(self.store, self.encryption, self.telegram.client, timeout=0.1)
        with self.assertRaises(LoginError) as error:
            await short_timeout.verify(self.owner, start["attempt_id"], "otp", "12345")
        self.assertEqual(error.exception.code, "login_timeout")
        self.assertEqual(self.telegram.live_clients, 0)
        with self.assertRaises(LoginError) as error:
            await self.restart().verify(self.owner, start["attempt_id"], "otp", "12345")
        self.assertEqual(error.exception.code, "attempt_busy")

    async def test_incorrect_password_keeps_recoverable_password_step(self):
        self.telegram.needs_password = True
        start = await self.start()
        await self.manager.verify(self.owner, start["attempt_id"], "otp", "12345")
        with self.assertRaises(LoginError) as error:
            await self.restart().verify(self.owner, start["attempt_id"], "password", "wrong-dummy-password")
        self.assertEqual(error.exception.code, "invalid_password")
        self.assertEqual((await self.store.attempt(self.owner, start["attempt_id"]))["step"], "password")
        result = await self.restart().verify(self.owner, start["attempt_id"], "password", "dummy-password")
        self.assertEqual(result["status"], "connected")

    async def test_stale_claim_cannot_save_or_consume(self):
        start = await self.start()
        claim = str(uuid4())
        await self.store.rpc("tg_login_claim", {"p_owner": self.owner,
            "p_attempt": start["attempt_id"], "p_claim": claim, "p_step": "otp"})
        self.store.sql(f"UPDATE public.tg_login_attempts SET claim_expires_at=now()-interval '1 second' WHERE id={literal(start['attempt_id'])}")
        with self.assertRaises(LoginError):
            await self.store.rpc("tg_login_save", {"p_owner": self.owner,
                "p_attempt": start["attempt_id"], "p_claim": claim, "p_step": "cancelled",
                "p_state": None, "p_key": None})
        with self.assertRaises(LoginError):
            await self.store.rpc("tg_login_complete", {"p_owner": self.owner,
                "p_attempt": start["attempt_id"], "p_claim": claim, "p_session": "tg1.v1.dummy",
                "p_key": "v1", "p_telegram_id": 123456789})
        self.assertEqual((await self.store.attempt(self.owner, start["attempt_id"]))["step"], "otp")

    async def test_completion_failure_does_not_consume_attempt(self):
        start = await self.start()
        original_rpc = self.store.rpc

        async def failing_rpc(name, params):
            if name == "tg_login_complete":
                raise LoginError("storage_unavailable", "Dummy database outage", 503)
            return await original_rpc(name, params)

        self.store.rpc = failing_rpc
        with self.assertRaises(LoginError):
            await self.manager.verify(self.owner, start["attempt_id"], "otp", "12345")
        self.store.rpc = original_rpc
        row = await self.store.attempt(self.owner, start["attempt_id"])
        self.assertEqual(row["step"], "otp")
        self.assertIsNone(await self.store.credential(self.owner))
        self.store.sql(f"UPDATE public.tg_login_attempts SET claim_expires_at=now()-interval '1 second' WHERE id={literal(start['attempt_id'])}")
        result = await self.restart().verify(self.owner, start["attempt_id"], "otp", "12345")
        self.assertEqual(result["status"], "connected")
        self.assertEqual(self.telegram.otp_calls, 1)

    async def test_cancel_and_database_expiry(self):
        start = await self.start()
        await self.manager.cancel(self.owner, start["attempt_id"])
        with self.assertRaises(LoginError):
            await self.manager.resume(self.owner, start["attempt_id"])
        start = await self.start()
        self.store.sql(f"UPDATE public.tg_login_attempts SET expires_at=now()-interval '1 second' WHERE id={literal(start['attempt_id'])}")
        with self.assertRaises(LoginError) as error:
            await self.manager.verify(self.owner, start["attempt_id"], "otp", "12345")
        self.assertEqual(error.exception.code, "attempt_expired")
        await self.store.rpc("tg_login_cleanup", {})
        row = await self.store.attempt(self.owner, start["attempt_id"])
        self.assertEqual(row["step"], "expired")
        self.assertIsNone(row["encrypted_state"])

    async def test_logout_retains_credentials_until_remote_revocation(self):
        start = await self.start()
        await self.manager.verify(self.owner, start["attempt_id"], "otp", "12345")
        self.telegram.logout_fails = True
        with self.assertRaises(LoginError) as error:
            await self.manager.logout(self.owner)
        self.assertEqual(error.exception.code, "revocation_pending")
        row = await self.store.credential(self.owner)
        self.assertEqual(row["state"], "revoking")
        self.assertIsNotNone(row["encrypted_session"])
        with self.assertRaises(LoginError):
            await self.start()
        self.telegram.logout_fails = False
        await self.restart().logout(self.owner)
        row = await self.store.credential(self.owner)
        self.assertEqual(row["state"], "revoked")
        self.assertIsNone(row["encrypted_session"])

    async def test_begin_persistence_failure_does_not_send_code(self):
        original = self.store.rpc
        async def unavailable(name, params):
            if name == "tg_login_begin":
                raise LoginError("storage_unavailable", "Dummy outage", 503)
            return await original(name, params)
        self.store.rpc = unavailable
        with self.assertRaises(LoginError):
            await self.start()
        self.assertEqual(self.telegram.sessions, {})

    async def test_simultaneous_starts_only_one_claim_succeeds(self):
        # Keep the winner's preparing claim open to model a slow sendCode call.
        results = await asyncio.gather(*[
            self.store.rpc("tg_login_begin", {
                "p_owner": self.owner, "p_attempt": str(uuid4()), "p_claim": str(uuid4()),
            }) for _ in range(2)
        ], return_exceptions=True)
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        loser = next(result for result in results if isinstance(result, LoginError))
        self.assertEqual(loser.code, "attempt_busy")

    async def test_null_claim_cannot_consume_unclaimed_attempt(self):
        start = await self.start()
        with self.assertRaises(LoginError):
            await self.store.rpc("tg_login_complete", {
                "p_owner": self.owner, "p_attempt": start["attempt_id"], "p_claim": None,
                "p_session": "tg1.v1.dummy", "p_key": "v1", "p_telegram_id": 123456789,
            })
        self.assertIsNone(await self.store.credential(self.owner))

    async def test_flood_wait_blocks_new_start_and_verification(self):
        start = await self.start()
        claim = str(uuid4())
        row = await self.store.rpc("tg_login_claim", {"p_owner": self.owner,
            "p_attempt": start["attempt_id"], "p_claim": claim, "p_step": "otp"})
        await self.store.rpc("tg_login_save", {"p_owner": self.owner,
            "p_attempt": row["id"], "p_claim": claim, "p_step": "otp",
            "p_state": row["encrypted_state"], "p_key": row["key_version"], "p_wait": 30})
        for operation in (self.start(), self.manager.verify(self.owner, row["id"], "otp", "12345")):
            with self.assertRaises(LoginError) as error:
                await operation
            self.assertEqual(error.exception.code, "login_cooldown")

    async def test_attempt_grants_and_rpc_permissions(self):
        for role in ("anon", "authenticated"):
            checks = self.store.sql("SELECT jsonb_build_object("
                f"'read',has_table_privilege('{role}','public.tg_login_attempts','SELECT'),"
                f"'write',has_table_privilege('{role}','public.tg_login_attempts','INSERT'),"
                f"'rpc',has_function_privilege('{role}','public.tg_login_begin(uuid,uuid,uuid)','EXECUTE'))")
            self.assertEqual(checks, {"read": False, "write": False, "rpc": False})

    async def test_service_role_can_execute_invoker_rpc(self):
        attempt_id, claim = str(uuid4()), str(uuid4())
        row = self.store.sql("SET ROLE service_role; SELECT public.tg_login_begin("
            f"{literal(self.owner)}, {literal(attempt_id)}, {literal(claim)})")
        self.assertEqual(row["dashboard_owner_id"], self.owner)
        self.assertEqual(row["claim_token"], claim)

    async def test_http_login_auth_ownership_and_metadata_failure(self):
        path = Path(__file__).resolve().parents[2] / "GetaipilotBackEnd1" / "Telesub.py"
        spec = importlib.util.spec_from_file_location("telesub_http_test", path)
        module = importlib.util.module_from_spec(spec)
        with patch("dotenv.load_dotenv"), patch("supabase.create_client"), patch.dict(os.environ, {
            "TELEGRAM_DURABLE_LOGIN": "true", "VITE_SUPABASE_URL": "https://dummy.invalid",
            "VITE_SUPABASE_ANON_KEY": "dummy", "TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "dummy",
        }):
            spec.loader.exec_module(module)
        tokens = {"owner": self.owner, "other": self.other}
        module.supabase = SimpleNamespace(auth=SimpleNamespace(
            get_user=lambda token: SimpleNamespace(user=SimpleNamespace(id=tokens[token]))))
        module._durable_login = self.manager
        class MetadataUnavailable:
            def table(self, name):
                raise RuntimeError("dummy metadata outage")
        module.get_authed_supabase = lambda token: MetadataUnavailable()
        original_client = module.get_telegram_client
        module.get_telegram_client = AsyncMock(return_value=SimpleNamespace(is_user_authorized=AsyncMock(return_value=False)))
        module.TelegramClient = lambda session, *args, **kwargs: self.telegram.client(StringSession.save(session))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://localhost:8000") as client:
            missing_auth = await client.post("/telegram/login/start", json={"phone": "+919876543210"})
            self.assertEqual(missing_auth.status_code, 401)
            headers = {"Authorization": "Bearer owner"}
            start = await client.post("/telegram/login/start", headers=headers, json={"phone": "+919876543210"})
            self.assertEqual(start.status_code, 200)
            attempt_id = start.json()["attempt_id"]
            cross_user = await client.get(f"/telegram/login/attempt/{attempt_id}", headers={"Authorization": "Bearer other"})
            self.assertEqual(cross_user.status_code, 409)
            invalid = await client.post("/telegram/login/otp", headers=headers,
                json={"attempt_id": attempt_id, "otp": "99999"})
            self.assertEqual(invalid.status_code, 400)
            self.assertEqual(invalid.json()["detail"]["code"], "invalid_code")
            # New manager instance models losing all process-local login state.
            module._durable_login = self.restart()
            success = await client.post("/telegram/login/otp", headers=headers,
                json={"attempt_id": attempt_id, "otp": "12345"})
            self.assertEqual(success.status_code, 200)
            self.assertEqual(success.json()["status"], "connected")
            self.assertTrue((await self.store.credential(self.owner))["metadata_pending"])
            module.get_telegram_client = original_client
            status = await client.get("/telegram/status", headers=headers)
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json(), {"connected": True, "phone": "+919876543210"})
            logout = await client.post("/telegram/logout", headers=headers)
            self.assertEqual(logout.status_code, 200)
            status = await client.get("/telegram/status", headers=headers)
            self.assertFalse(status.json()["connected"])
        for cached in module.clients.values():
            await cached.disconnect()


if __name__ == "__main__":
    unittest.main()
