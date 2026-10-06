"""Exercise actual recovery handlers without importing the live Telegram bot."""
import ast
import asyncio
import contextlib
import io
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock


SOURCE = Path(__file__).resolve().parents[1] / "gapautoforward.py"


async def inline_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


class RecoveryNotifications(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tree = ast.parse(SOURCE.read_text())
        names = {"serialize_user_operation", "cmd_work", "resume_forwarding_on_start"}
        nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
        for node in nodes:
            if node.name == "cmd_work":
                node.decorator_list = [ast.Name(id="serialize_user_operation", ctx=ast.Load())]
        ast.fix_missing_locations(tree)
        self.client = Mock()
        self.client.on.side_effect = lambda event: lambda handler: handler
        self.client.disconnect = AsyncMock()
        self.db = Mock()
        self.db.table.return_value.upsert.return_value.execute.return_value = None
        self.bot = Mock(send_message=AsyncMock())
        self.ns = {
            "asyncio": SimpleNamespace(to_thread=inline_thread, Lock=asyncio.Lock),
            "datetime": datetime, "timezone": timezone,
            "forward_loops": {}, "EXPLICIT_PAUSED_USERS": set(),
            "USER_CLIENT_CACHE": {}, "USER_FORWARD_LOCKS": {}, "select_state": {},
            "DATABASE_CLIENTS": None, "supabase": self.db, "bot": self.bot,
            "events": SimpleNamespace(NewMessage=object()),
            "sp_get_forwarding_users": Mock(return_value=[101, 202]),
            "get_user_client": AsyncMock(return_value=self.client),
            "guard_or_hint": AsyncMock(return_value=True),
            "premium_or_hint": AsyncMock(return_value=True),
            "sp_load_mapping": Mock(return_value={10: [20]}),
            "titles_for_ids": AsyncMock(return_value=["Chat"]),
            "sp_upsert_mapping": Mock(), "compile_filters_for_user": Mock(return_value=[]),
            "compile_blacklist_for_user": Mock(return_value=[]),
            "sp_get_delay": Mock(return_value=0), "sp_get_text_addons": Mock(return_value={}),
        }
        exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), self.ns)
        self.output = io.StringIO()

    async def resume(self, missing_only=False):
        with contextlib.redirect_stdout(self.output):
            await self.ns["resume_forwarding_on_start"](missing_only=missing_only)
        self.bot.send_message.assert_not_called()

    async def test_startup_resumes_all_users_without_dms(self):
        await self.resume()
        self.assertEqual(set(self.ns["forward_loops"]), {101, 202})
        self.assertEqual(self.client.on.call_count, 2)
        self.assertEqual(self.db.table.return_value.upsert.call_count, 2)

    async def test_repeated_owner_loss_recovers_without_dms(self):
        for _ in range(5):
            self.ns["forward_loops"].clear()
            await self.resume(missing_only=True)
            self.assertEqual(set(self.ns["forward_loops"]), {101, 202})

    async def test_retry_skips_active_and_paused_users(self):
        self.ns["forward_loops"][101] = {"client": self.client}
        self.ns["EXPLICIT_PAUSED_USERS"].add(202)
        await self.resume(missing_only=True)
        self.ns["get_user_client"].assert_not_awaited()

    async def test_failed_login_is_silent_and_continues_other_users(self):
        self.ns["get_user_client"].side_effect = [RuntimeError("login unavailable"), self.client, self.client]
        await self.resume()
        self.assertEqual(set(self.ns["forward_loops"]), {202})
        self.assertIn("login unavailable", self.output.getvalue())

    async def test_missing_mapping_errors_are_silent_on_every_retry(self):
        self.ns["sp_load_mapping"].return_value = {}
        for _ in range(3):
            await self.resume(missing_only=True)
        self.assertEqual(self.ns["forward_loops"], {})
        self.assertIn("Please set both", self.output.getvalue())

    async def test_empty_user_list(self):
        self.ns["sp_get_forwarding_users"].return_value = []
        await self.resume()
        self.ns["get_user_client"].assert_not_awaited()

    async def test_manual_work_still_confirms_and_attaches_forwarder(self):
        event = SimpleNamespace(sender_id=101, raw_text="/work", respond=AsyncMock())
        await self.ns["cmd_work"](event)
        event.respond.assert_awaited_once_with("▶️ **Forwarding started!**\nStop anytime using **/stop**.")
        self.assertIn(101, self.ns["forward_loops"])
        self.client.on.assert_called_once()

    async def test_manual_work_still_reports_configuration_errors(self):
        self.ns["sp_load_mapping"].return_value = {}
        event = SimpleNamespace(sender_id=101, raw_text="/work", respond=AsyncMock())
        await self.ns["cmd_work"](event)
        event.respond.assert_awaited_once()
        self.assertIn("Please set both", event.respond.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
