"""Durable, owner-bound Submanager login attempts. No OTP/password persistence."""

import asyncio
from dataclasses import dataclass
import json
import re
from uuid import UUID, uuid4

from telethon import errors
from telethon.sessions import StringSession

from .encryption import EncryptionError, SessionContext, SessionEncryption


@dataclass
class LoginError(Exception):
    code: str
    message: str
    status_code: int = 400
    restart: bool = False
    retry_after: int | None = None


_STORE_ERRORS = {
    "TG_ATTEMPT_BUSY": LoginError("attempt_busy", "Login is being processed. Please retry shortly.", 409),
    "TG_ATTEMPT_COOLDOWN": LoginError("login_cooldown", "Please wait before retrying Telegram login.", 429),
    "TG_ATTEMPT_EXPIRED": LoginError("attempt_expired", "Login expired. Request a new code.", 410, True),
    "TG_ATTEMPT_STALE": LoginError("attempt_stale", "This login is no longer available. Request a new code.", 409, True),
    "TG_ATTEMPT_WRONG_STEP": LoginError("wrong_step", "Resume the current login step.", 409),
    "TG_CREDENTIAL_CHANGED": LoginError("credential_changed", "Connection changed. Start login again.", 409, True),
    "TG_CREDENTIAL_REVOKING": LoginError("revocation_pending", "Telegram logout is still pending.", 409),
}


def canonical_id(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise LoginError("attempt_missing", "Request a new code to start login.", 400, True) from None


def attempt_context(row):
    return SessionContext(row["id"], "user", "submanager_login", dashboard_owner_id=row["dashboard_owner_id"])


def credential_context(row):
    return SessionContext(
        row["id"], row["principal_kind"], row["purpose"],
        dashboard_owner_id=row.get("dashboard_owner_id"),
        telegram_owner_id=row.get("telegram_owner_id"),
        bot_configuration_id=row.get("bot_configuration_id"),
    )


class SupabaseLoginStore:
    def __init__(self, client):
        self.client = client

    async def _execute(self, operation):
        try:
            return await asyncio.to_thread(operation)
        except Exception as error:
            # Never forward raw Supabase errors containing payloads to users/logs.
            message = str(error)
            for marker, mapped in _STORE_ERRORS.items():
                if marker in message:
                    raise LoginError(**vars(mapped)) from None
            raise LoginError("storage_unavailable", "Unable to save or restore Telegram login. Please retry.", 503) from None

    async def rpc(self, name, params):
        return await self._execute(lambda: self.client.rpc(name, params).execute().data)

    async def credential(self, owner):
        result = await self._execute(lambda: self.client.table("tg_session_credentials")
            .select("*").eq("dashboard_owner_id", canonical_id(owner))
            .eq("principal_kind", "user").eq("purpose", "submanager").maybe_single().execute())
        return result.data if result else None

    async def attempt(self, owner, attempt_id):
        result = await self._execute(lambda: self.client.table("tg_login_attempts")
            .select("*").eq("dashboard_owner_id", canonical_id(owner))
            .eq("id", canonical_id(attempt_id)).maybe_single().execute())
        return result.data if result else None

    async def metadata_synced(self, credential):
        await self._execute(lambda: self.client.table("tg_session_credentials")
            .update({"metadata_pending": False}).eq("id", credential["id"])
            .eq("version", credential["version"]).execute())


class DurableLogin:
    """Each operation disconnects before releasing its 120-second DB claim.

    The bounded operation timeout is intentionally much shorter than the claim.
    Completed-session worker ownership remains a separate rollout requirement.
    """

    def __init__(self, store, encryption: SessionEncryption, client_factory, timeout=45):
        if not 0 < timeout <= 45:
            raise ValueError("Login operations must finish within 45 seconds")
        self.store = store
        self.encryption = encryption
        self.client_factory = client_factory
        self.timeout = timeout

    async def _disconnect(self, client):
        try:
            await asyncio.wait_for(asyncio.shield(client.disconnect()), timeout=5)
        except Exception:
            # Do not release a claim while its connection may still be alive.
            raise LoginError("disconnect_failed", "Telegram connection is closing. Please retry shortly.", 503) from None

    def _encode(self, row, state):
        return self.encryption.encrypt(json.dumps(state, separators=(",", ":")), attempt_context(row))

    def _decode(self, row):
        try:
            state = json.loads(self.encryption.decrypt(row["encrypted_state"], attempt_context(row)))
            if not isinstance(state, dict) or not all(
                isinstance(state.get(key), str) and state[key] for key in ("session", "phone", "code_hash")
            ):
                raise ValueError
            return state
        except (EncryptionError, ValueError, KeyError, TypeError):
            raise LoginError("state_invalid", "Unable to restore login. Request a new code.", 503, True) from None

    async def _save(self, row, claim, step, state=None, failed=False, wait=0):
        return await self.store.rpc("tg_login_save", {
            "p_owner": row["dashboard_owner_id"], "p_attempt": row["id"], "p_claim": claim,
            "p_step": step, "p_state": self._encode(row, state) if state else None,
            "p_key": self.encryption.active_key_version if state else None,
            "p_failed": failed, "p_wait": wait,
        })

    async def start(self, owner, phone):
        owner = canonical_id(owner)
        phone = re.sub(r"[\s()-]", "", phone)
        if not re.fullmatch(r"\+[1-9]\d{5,14}", phone):
            raise LoginError("invalid_phone", "Enter your phone number with its country code.")
        try:
            async with asyncio.timeout(self.timeout):
                claim = str(uuid4())
                row = await self.store.rpc("tg_login_begin", {
                    "p_owner": owner, "p_attempt": str(uuid4()), "p_claim": claim,
                })
                client = self.client_factory("")
                failure = None
                state = None
                try:
                    await client.connect()
                    sent = await client.send_code_request(phone)
                    if not getattr(sent, "phone_code_hash", None):
                        raise LoginError("code_unavailable", "Telegram did not return a login code. Please retry.")
                    state = {"phone": phone, "code_hash": sent.phone_code_hash}
                except errors.FloodWaitError as error:
                    failure = LoginError("flood_wait", "Telegram requires a wait before requesting another code.", 429,
                        retry_after=error.seconds)
                except errors.PhoneNumberInvalidError:
                    failure = LoginError("invalid_phone", "Telegram rejected this phone number.")
                except errors.PhoneNumberBannedError:
                    failure = LoginError("phone_banned", "Telegram has blocked login for this phone number.")
                except LoginError as error:
                    failure = error
                except Exception:
                    failure = LoginError("telegram_unavailable", "Unable to request a Telegram code. Please retry.", 503)
                finally:
                    await self._disconnect(client)
                if failure:
                    await self._save(row, claim, "cancelled", wait=failure.retry_after or 0)
                    raise failure
                state["session"] = StringSession.save(client.session)
                row = await self._save(row, claim, "otp", state)
                return {"status": "otp_sent", "attempt_id": row["id"], "expires_at": row["expires_at"]}
        except TimeoutError:
            raise LoginError("login_timeout", "Telegram login timed out. Please retry shortly.", 503) from None

    async def verify(self, owner, attempt_id, step, value):
        owner, attempt_id = canonical_id(owner), canonical_id(attempt_id)
        if step == "otp" and not re.fullmatch(r"\d{4,8}", value.strip()):
            raise LoginError("invalid_code", "Enter the numeric Telegram code.")
        if step not in ("otp", "password") or not value:
            raise LoginError("invalid_input", "Enter the requested login information.")
        try:
            async with asyncio.timeout(self.timeout):
                claim = str(uuid4())
                row = await self.store.rpc("tg_login_claim", {
                    "p_owner": owner, "p_attempt": attempt_id, "p_claim": claim, "p_step": step,
                })
                state = self._decode(row)
                client = self.client_factory(state["session"])
                failure = None
                next_step = step
                identity = None
                try:
                    await client.connect()
                    # Handles a crash after Telegram succeeds but before the DB commit.
                    if not await client.is_user_authorized():
                        if step == "otp":
                            await client.sign_in(state["phone"], value.strip(), phone_code_hash=state["code_hash"])
                        else:
                            await client.sign_in(password=value)
                    identity = await client.get_me()
                    if (not identity or getattr(identity, "bot", False)
                        or not getattr(identity, "phone", None)
                        or "+" + identity.phone != state["phone"]):
                        raise LoginError("identity_mismatch", "Telegram account did not match this login.", 409, True)
                except errors.SessionPasswordNeededError:
                    next_step = "password"
                except (errors.PhoneCodeInvalidError, errors.PasswordHashInvalidError):
                    failure = LoginError("invalid_code" if step == "otp" else "invalid_password",
                        "Invalid Telegram code." if step == "otp" else "Incorrect two-step verification password.")
                except errors.PhoneCodeExpiredError:
                    next_step = "expired"
                    failure = LoginError("code_expired", "Telegram expired this code. Request a new one.", 410, True)
                except errors.FloodWaitError as error:
                    failure = LoginError("flood_wait", "Telegram requires a wait before retrying.", 429,
                        retry_after=error.seconds)
                except LoginError as error:
                    next_step = "cancelled"
                    failure = error
                except Exception:
                    failure = LoginError("telegram_unavailable", "Unable to verify Telegram login. Please retry.", 503)
                finally:
                    await self._disconnect(client)
                state["session"] = StringSession.save(client.session)
                if identity and not failure:
                    context = SessionContext(row["credential_id"], "user", "submanager", dashboard_owner_id=owner)
                    await self.store.rpc("tg_login_complete", {
                        "p_owner": owner, "p_attempt": attempt_id, "p_claim": claim,
                        "p_session": self.encryption.encrypt(state["session"], context),
                        "p_key": self.encryption.active_key_version, "p_telegram_id": identity.id,
                    })
                    return {"status": "connected", "next": "done", "phone": state["phone"], "metadata_pending": True}
                updated = await self._save(row, claim, next_step,
                    state if next_step in ("otp", "password") else None,
                    failed=bool(failure and failure.code in ("invalid_code", "invalid_password")),
                    wait=failure.retry_after if failure and failure.retry_after else 0)
                if updated["step"] == "cancelled" and next_step not in ("cancelled", "expired"):
                    raise LoginError("retry_limit", "Too many attempts. Request a new code.", 429, True)
                if failure:
                    raise failure
                return {"status": "needs_password", "next": "password_required", "attempt_id": attempt_id}
        except TimeoutError:
            raise LoginError("login_timeout", "Telegram login timed out. Please retry shortly.", 503) from None

    async def resume(self, owner, attempt_id):
        row = await self.store.attempt(canonical_id(owner), canonical_id(attempt_id))
        if not row or row["step"] not in ("otp", "password"):
            raise LoginError("attempt_stale", "Request a new code to start login.", 409, True)
        from datetime import datetime, timezone
        if datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00")) <= datetime.now(timezone.utc):
            raise LoginError("attempt_expired", "Login expired. Request a new code.", 410, True)
        return {"attempt_id": row["id"], "step": row["step"], "expires_at": row["expires_at"]}

    async def cancel(self, owner, attempt_id):
        await self.store.rpc("tg_login_cancel", {"p_owner": canonical_id(owner), "p_attempt": canonical_id(attempt_id)})
        return {"status": "cancelled"}

    async def logout(self, owner):
        owner = canonical_id(owner)
        row = await self.store.rpc("tg_submanager_revoke", {"p_owner": owner, "p_done": False})
        if not row:
            return None  # Legacy account: existing logout handler still owns it.
        if row["state"] == "revoked":
            return {"status": "success"}
        try:
            async with asyncio.timeout(self.timeout):
                client = self.client_factory(self.encryption.decrypt(row["encrypted_session"], credential_context(row)))
                try:
                    await client.connect()
                    if await client.is_user_authorized() and not await client.log_out():
                        raise LoginError("revocation_pending", "Remote Telegram logout is pending. Retry disconnect.", 503)
                finally:
                    await self._disconnect(client)
                await self.store.rpc("tg_submanager_revoke", {
                    "p_owner": owner, "p_done": True, "p_version": row["version"],
                })
                return {"status": "success"}
        except LoginError:
            raise
        except Exception:
            raise LoginError("revocation_pending", "Remote Telegram logout is pending. Retry disconnect.", 503) from None
