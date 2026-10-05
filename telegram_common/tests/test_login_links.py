import asyncio
import os
from pathlib import Path
import secrets
import unittest
from uuid import uuid4
from unittest.mock import AsyncMock

from telegram_common.login import LoginError
from telegram_common.login_links import AutoForwardLoginLinks, token_hash
from telegram_common.tests.test_durable_login import PostgresStore, literal


class LinkHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_hash_reaches_storage_and_token_is_in_fragment(self):
        store = AsyncMock()
        store.rpc.return_value = {"expires_at": "dummy"}
        broker = AutoForwardLoginLinks(store, "https://getaipilot.in/telegram/autoforward/login")
        result = await broker.issue(123)
        token = result["url"].split("#login_token=")[1]
        params = store.rpc.call_args.args[1]
        self.assertEqual(params["p_hash"], token_hash(token))
        self.assertNotIn(token, str(params))
        self.assertNotIn("?", result["url"])

    async def test_invalid_token_or_owner_never_reaches_store(self):
        store = AsyncMock()
        broker = AutoForwardLoginLinks(store, "https://getaipilot.in")
        with self.assertRaises(LoginError):
            await broker.redeem(str(uuid4()), "bad-token")
        with self.assertRaises(LoginError):
            await broker.redeem("bad-owner", secrets.token_urlsafe(32))
        store.rpc.assert_not_called()

    def test_unsafe_urls_are_rejected(self):
        for url in ["http://example.com", "https://a:b@example.com", "https://example.com?token=x", "https://example.com#x"]:
            with self.assertRaises(ValueError):
                AutoForwardLoginLinks(None, url)


@unittest.skipUnless(os.environ.get("TELEGRAM_TEST_PG_SOCKET", "").startswith("/tmp/"),
                     "Requires disposable /tmp PostgreSQL socket")
class LinkDatabaseTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = PostgresStore()
        cls.store.sql("CREATE TABLE IF NOT EXISTS public.profiles(id uuid PRIMARY KEY REFERENCES auth.users(id), telegram_user_id bigint);")
        migration = Path(__file__).resolve().parents[3] / "bot-dashboard/getaipilot.in/supabase/migrations/20261003002000_autoforward_login_links.sql"
        if not cls.store.sql("SELECT to_json(to_regclass('public.tg_autoforward_login_links') IS NOT NULL)"):
            cls.store.sql(migration.read_text())
        cls.store.sql("GRANT SELECT ON public.profiles TO service_role;")

    async def asyncSetUp(self):
        self.owner, self.other = str(uuid4()), str(uuid4())
        self.telegram = secrets.randbelow(1_000_000_000) + 1_000_000_000
        for owner in (self.owner, self.other):
            self.store.sql(f"INSERT INTO auth.users(id) VALUES ({literal(owner)})")
        self.store.sql(f"INSERT INTO public.profiles VALUES ({literal(self.owner)}, {self.telegram})")
        self.broker = AutoForwardLoginLinks(self.store, "https://getaipilot.in/telegram/autoforward/login")

    async def asyncTearDown(self):
        owners = f"({literal(self.owner)},{literal(self.other)})"
        self.store.sql(f"DELETE FROM public.tg_autoforward_login_links WHERE dashboard_owner_id IN {owners}; DELETE FROM public.profiles WHERE id IN {owners}; DELETE FROM auth.users WHERE id IN {owners};")

    async def issue(self):
        result = await self.broker.issue(self.telegram)
        return result["url"].split("#login_token=")[1]

    async def test_owner_binding_and_one_use(self):
        token = await self.issue()
        with self.assertRaises(RuntimeError):
            await self.broker.redeem(self.other, token)
        grant = await self.broker.redeem(self.owner, token)
        self.assertEqual(grant["telegram_owner_id"], self.telegram)
        with self.assertRaises(RuntimeError):
            await self.broker.redeem(self.owner, token)

    async def test_concurrent_redemption_has_one_winner(self):
        token = await self.issue()
        outcomes = await asyncio.gather(*[self.broker.redeem(self.owner, token) for _ in range(2)], return_exceptions=True)
        self.assertEqual(sum(isinstance(r, dict) for r in outcomes), 1)

    async def test_replacement_invalidates_old_link(self):
        old = await self.issue()
        new = await self.issue()
        with self.assertRaises(RuntimeError):
            await self.broker.redeem(self.owner, old)
        self.assertEqual((await self.broker.redeem(self.owner, new))["dashboard_owner_id"], self.owner)

    async def test_expiry_and_changed_identity(self):
        token = await self.issue()
        self.store.sql(f"UPDATE public.tg_autoforward_login_links SET created_at=now()-interval '6 minutes', expires_at=now()-interval '1 minute' WHERE dashboard_owner_id={literal(self.owner)}")
        with self.assertRaises(RuntimeError):
            await self.broker.redeem(self.owner, token)
        token = await self.issue()
        self.store.sql(f"UPDATE public.profiles SET telegram_user_id=NULL WHERE id={literal(self.owner)}")
        with self.assertRaises(RuntimeError):
            await self.broker.redeem(self.owner, token)

    async def test_ambiguous_identity_cannot_issue(self):
        self.store.sql(f"INSERT INTO public.profiles VALUES ({literal(self.other)}, {self.telegram})")
        with self.assertRaises(RuntimeError):
            await self.issue()

    def test_browser_grants_and_service_role(self):
        for role in ("anon", "authenticated"):
            self.assertFalse(self.store.sql(f"SELECT to_json(has_table_privilege('{role}', 'public.tg_autoforward_login_links', 'SELECT'))"))
            self.assertFalse(self.store.sql(f"SELECT to_json(has_function_privilege('{role}', 'public.tg_autoforward_link_redeem(uuid,text)', 'EXECUTE'))"))
        self.assertTrue(self.store.sql("SELECT to_json(has_function_privilege('service_role', 'public.tg_autoforward_link_issue(bigint,uuid,text)', 'EXECUTE'))"))


if __name__ == "__main__":
    unittest.main()
