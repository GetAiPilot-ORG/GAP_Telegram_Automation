"""Single-use AutoForward identity links. No OTP/password/session persistence.

Issue only for an incoming private Telegram event's verified sender ID. Redeem
only for a dashboard owner resolved from a verified JWT, never a request body.
The returned grant is an identity bridge, not a completed Telegram login.
"""

import asyncio
import hashlib
import re
import secrets
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from .login import LoginError


def token_hash(token):
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise LoginError("link_invalid", "Request a new login link in the bot.", 400)
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class SupabaseLinkStore:
    def __init__(self, client):
        self.client = client

    async def rpc(self, name, params):
        try:
            return await asyncio.to_thread(lambda: self.client.rpc(name, params).execute().data)
        except Exception as error:
            if "TG_LINK_IDENTITY_REQUIRED" in str(error):
                raise LoginError("identity_required", "Connect your dashboard account to this Telegram account first.", 409) from None
            if "TG_LINK_INVALID" in str(error):
                raise LoginError("link_invalid", "This login link is expired or unavailable. Request a new one.", 400) from None
            raise LoginError("storage_unavailable", "Unable to prepare login. Please retry.", 503) from None


class AutoForwardLoginLinks:
    def __init__(self, store, website_url):
        parsed = urlsplit(website_url)
        is_local = parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")
        if (parsed.scheme != "https" and not is_local) or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Login website must be an HTTPS URL (or HTTP on localhost) without credentials, query, or fragment")
        self.store = store
        self.website_url = website_url.rstrip("/")

    async def issue(self, verified_telegram_sender_id):
        if type(verified_telegram_sender_id) is not int or not 0 < verified_telegram_sender_id <= 9223372036854775807:
            raise ValueError("Expected a verified Telegram sender ID")
        token = secrets.token_urlsafe(32)
        result = await self.store.rpc("tg_autoforward_link_issue", {
            "p_telegram_owner": verified_telegram_sender_id, "p_id": str(uuid4()), "p_hash": token_hash(token),
        })
        # Fragment avoids HTTP access logs/referrer leakage. The future page must
        # remove it immediately, hold it in memory, and never persist/log it.
        return {"url": self.website_url + "#login_token=" + token, "expires_at": result["expires_at"]}

    async def redeem(self, verified_dashboard_owner_id, token):
        try:
            owner = str(UUID(str(verified_dashboard_owner_id)))
        except (ValueError, TypeError, AttributeError):
            raise LoginError("unauthorized", "Sign in to your dashboard first.", 401) from None
        return await self.store.rpc("tg_autoforward_link_redeem", {"p_owner": owner, "p_hash": token_hash(token)})
