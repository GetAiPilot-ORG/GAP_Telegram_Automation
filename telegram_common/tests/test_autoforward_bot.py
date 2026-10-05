"""Exercise bot integration without importing its network-starting entrypoint."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock


SOURCE = Path(__file__).resolve().parents[2] / "GetAiPilot_autoforwarding_bot/gapautoforward.py"
TREE = ast.parse(SOURCE.read_text())


def load_function(name, namespace):
    node = next(node for node in TREE.body if isinstance(node, ast.AsyncFunctionDef) and node.name == name)
    node = ast.parse(ast.unparse(node)).body[0]
    node.decorator_list = []
    node.returns = None
    for arg in node.args.args:
        arg.annotation = None
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), str(SOURCE), "exec"), namespace)
    return namespace[name]


class BotIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def namespace(self, row):
        client = SimpleNamespace(owner_manager=object(), disconnect=AsyncMock())
        return {"asyncio": asyncio, "USER_CLIENT_LOCKS": {}, "USER_CLIENT_CACHE": {},
                "forward_loops": {}, "session_store": SimpleNamespace(telegram_credential=AsyncMock(return_value=row)),
                "DATABASE_CLIENTS": SimpleNamespace(get=AsyncMock(return_value=client)),
                "_get_legacy_user_client": AsyncMock(return_value="legacy")}

    async def test_active_database_credential_uses_leased_client(self):
        ns = self.namespace({"state": "active"})
        client = await load_function("get_user_client", ns)(123)
        self.assertIs(client, ns["USER_CLIENT_CACHE"][123])
        ns["DATABASE_CLIENTS"].get.assert_awaited_once_with(123)
        ns["_get_legacy_user_client"].assert_not_awaited()

    async def test_revoked_credential_never_falls_back_to_old_file(self):
        ns = self.namespace({"state": "revoked"})
        with self.assertRaises(RuntimeError):
            await load_function("get_user_client", ns)(123)
        ns["_get_legacy_user_client"].assert_not_awaited()

    async def test_storage_failure_never_falls_back_to_old_file(self):
        ns = self.namespace(None)
        ns["session_store"].telegram_credential.side_effect = RuntimeError("offline")
        with self.assertRaises(RuntimeError):
            await load_function("get_user_client", ns)(123)
        ns["_get_legacy_user_client"].assert_not_awaited()

    async def test_unmigrated_owner_retains_legacy_compatibility(self):
        ns = self.namespace(None)
        self.assertEqual(await load_function("get_user_client", ns)(123), "legacy")

    async def test_web_login_never_reads_bot_chat_secret(self):
        ns = {"AUTOFORWARD_WEB_LOGIN": True}
        await load_function("login_flow", ns)(object())

    async def test_stop_disconnects_and_persists_pause_even_on_retry(self):
        client = SimpleNamespace(list_event_handlers=lambda: [], disconnect=AsyncMock())
        ns = {"EXPLICIT_PAUSED_USERS": set(), "forward_loops": {123: {"client": client}},
              "USER_CLIENT_CACHE": {123: client}, "persist_stopped": AsyncMock()}
        event = SimpleNamespace(sender_id=123, respond=AsyncMock())
        stop = load_function("cmd_stop", ns)
        await stop(event)
        await stop(event)
        client.disconnect.assert_awaited_once()
        self.assertEqual(ns["persist_stopped"].await_count, 2)
        self.assertIn(123, ns["EXPLICIT_PAUSED_USERS"])
        self.assertNotIn(123, ns["USER_CLIENT_CACHE"])
