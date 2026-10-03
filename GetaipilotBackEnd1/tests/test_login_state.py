"""Exercise login functions without Telegram connections or database credentials."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail


class PasswordNeeded(Exception):
    pass


class InvalidCode(Exception):
    pass


class ExpiredCode(Exception):
    pass


def load_functions(namespace):
    path = Path(__file__).resolve().parents[1] / "Telesub.py"
    tree = ast.parse(path.read_text())
    names = {"get_telegram_client", "_get_telegram_client", "login_otp"}
    functions = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name in names]
    for function in functions:
        function.decorator_list = []
    namespace.update(
        DURABLE_LOGIN_ENABLED=False,
        asyncio=asyncio, Client=object, OtpRequest=object,
        Depends=lambda value: None, get_auth_context=None,
        HTTPException=HTTPException, SessionPasswordNeededError=PasswordNeeded,
        PhoneCodeInvalidError=InvalidCode, PhoneCodeExpiredError=ExpiredCode,
        logger=SimpleNamespace(warning=lambda *args: None),
    )
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


class LoginStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_overlapping_requests_share_one_client(self):
        created = []

        class FakeClient:
            def __init__(self, *args, **kwargs):
                created.append(self)

            async def connect(self):
                await asyncio.sleep(0.01)

            def is_connected(self):
                return True

        ns = load_functions(dict(
            clients={}, client_locks={}, API_ID="1", API_HASH="test",
            StringSession=lambda value: value, TelegramClient=FakeClient,
        ))
        first, second = await asyncio.gather(
            ns["get_telegram_client"]("user"), ns["get_telegram_client"]("user"),
        )
        self.assertIs(first, second)
        self.assertIs(first, ns["clients"]["user"])
        self.assertEqual(len(created), 1)

    async def test_otp_uses_matching_hash_and_preserves_password_step(self):
        client = SimpleNamespace(
            _phone_number="+123456789", _login_code_hash="matching-hash",
            sign_in=AsyncMock(side_effect=PasswordNeeded),
        )
        ns = load_functions(dict(get_authed_supabase=lambda token: None))
        ns["get_telegram_client"] = AsyncMock(return_value=client)
        result = await ns["login_otp"](
            SimpleNamespace(otp=" 12345 "),
            {"user": SimpleNamespace(id="user"), "token": "test"},
        )
        client.sign_in.assert_awaited_once_with(
            "+123456789", "12345", phone_code_hash="matching-hash",
        )
        self.assertEqual(result["status"], "needs_password")

    async def test_expired_code_requires_a_new_request(self):
        client = SimpleNamespace(
            _phone_number="+123456789", _login_code_hash="old-hash",
            sign_in=AsyncMock(side_effect=ExpiredCode),
        )
        ns = load_functions(dict(get_authed_supabase=lambda token: None))
        ns["get_telegram_client"] = AsyncMock(return_value=client)
        context = {"user": SimpleNamespace(id="user"), "token": "test"}
        with self.assertRaises(HTTPException) as error:
            await ns["login_otp"](SimpleNamespace(otp="12345"), context)
        self.assertIn("expired", error.exception.detail)
        self.assertIsNone(client._login_code_hash)
        with self.assertRaises(HTTPException) as error:
            await ns["login_otp"](SimpleNamespace(otp="12345"), context)
        self.assertIn("request a new code", error.exception.detail)
        self.assertEqual(client.sign_in.await_count, 1)


if __name__ == "__main__":
    unittest.main()
