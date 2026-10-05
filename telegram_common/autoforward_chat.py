"""Database-backed bot-chat login using the verified private Telegram sender.

Reuses the durable AutoForward claims and identity checks. OTPs/passwords are
passed directly to Telegram, never retained by this adapter.
"""
import secrets
from uuid import uuid4

from .login import LoginError, canonical_id
from .login_links import token_hash


class AutoForwardChatLogin:
    def __init__(self, store, runtime):
        self.store, self.runtime = store, runtime

    async def owner(self, telegram_id):
        if type(telegram_id) is not int or telegram_id <= 0:
            raise LoginError("identity_mismatch", "Use your private chat to login.")
        return canonical_id(await self.store.chat_owner(telegram_id))

    async def pending(self, telegram_id):
        owner = await self.owner(telegram_id)
        row = await self.store.chat_attempt(owner)
        if not row:
            return None
        if row["telegram_owner_id"] != telegram_id:
            raise LoginError("identity_mismatch", "Telegram account does not match this login.", 409, True)
        try:
            await self.runtime.resume(owner, row["id"])
        except LoginError as error:
            if error.code == "attempt_expired":
                await self.runtime.cancel(owner, row["id"])
            raise
        return row

    async def start(self, telegram_id, phone):
        owner = await self.owner(telegram_id)
        # Internal, short-lived identity grant. No website link is sent or needed.
        # Restricted RPCs recheck the profile mapping inside the transaction.
        token = secrets.token_urlsafe(32)
        digest = token_hash(token)
        await self.store.rpc("tg_autoforward_link_issue", {
            "p_telegram_owner": telegram_id, "p_id": str(uuid4()), "p_hash": digest})
        grant = await self.store.rpc("tg_autoforward_link_redeem", {"p_owner": owner, "p_hash": digest})
        return await self.runtime.start(owner, phone, grant["id"])

    async def verify(self, telegram_id, value):
        row = await self.pending(telegram_id)
        if not row:
            raise LoginError("attempt_stale", "No login in progress. Use /login.", 409, True)
        return await self.runtime.verify(row["dashboard_owner_id"], row["id"], row["step"], value)

    async def resend(self, telegram_id):
        row = await self.pending(telegram_id)
        if not row or row["step"] != "otp":
            raise LoginError("wrong_step", "No OTP login in progress. Use /login.", 409)
        # Phone/session/hash remain encrypted in storage. Start atomically replaces
        # the attempt, preserving cooldown and fencing checks.
        return await self.start(telegram_id, self.runtime._decode(row)["phone"])

    async def cancel(self, telegram_id):
        owner = await self.owner(telegram_id)
        row = await self.store.chat_attempt(owner)
        if row:
            if row["telegram_owner_id"] != telegram_id:
                raise LoginError("identity_mismatch", "Telegram account does not match this login.", 409)
            await self.runtime.cancel(owner, row["id"])
        return bool(row)
