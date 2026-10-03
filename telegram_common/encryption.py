"""Versioned AES-256-GCM envelopes bound to immutable credential identity.

This module never generates production keys implicitly, reads .env files,
connects to a database, or logs plaintext/key material.
"""

import base64
from dataclasses import asdict, dataclass
import json
import os
import re
from typing import Mapping
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_LABEL = re.compile(r"[a-zA-Z0-9_-]{1,64}\Z")
_PURPOSE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_MAX_SESSION_BYTES = 16384


class EncryptionError(ValueError):
    """A credential could not be authenticated or decoded; no secret in message."""


def _decode(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("Expected a nonempty base64 string")
    return base64.b64decode(value.encode("ascii"), altchars=b"-_", validate=True)


def _uuid(value: str) -> str:
    return str(UUID(value))


@dataclass(frozen=True)
class SessionContext:
    """Immutable columns of tg_session_credentials used as authenticated data."""

    credential_id: str
    principal_kind: str
    purpose: str
    dashboard_owner_id: str | None = None
    telegram_owner_id: int | None = None
    bot_configuration_id: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "credential_id", _uuid(self.credential_id))
        if self.dashboard_owner_id is not None:
            object.__setattr__(self, "dashboard_owner_id", _uuid(self.dashboard_owner_id))
        if not _PURPOSE.fullmatch(self.purpose):
            raise ValueError("Invalid credential purpose")
        if self.telegram_owner_id is not None and (
            type(self.telegram_owner_id) is not int
            or not 0 < self.telegram_owner_id <= 9223372036854775807
        ):
            raise ValueError("Invalid Telegram owner ID")
        if self.principal_kind == "user":
            if self.bot_configuration_id is not None or (
                self.dashboard_owner_id is None and self.telegram_owner_id is None
            ):
                raise ValueError("User credentials require a user owner and no bot ID")
        elif self.principal_kind == "bot":
            if (
                not isinstance(self.bot_configuration_id, str)
                or not 1 <= len(self.bot_configuration_id) <= 128
                or self.bot_configuration_id != self.bot_configuration_id.strip()
                or self.telegram_owner_id is not None
            ):
                raise ValueError("Bot credentials require a bot ID and no Telegram user owner")
        else:
            raise ValueError("Invalid credential principal kind")

    def associated_data(self, key_version: str) -> bytes:
        return json.dumps(
            {"format": "tg1", "key_version": key_version, **asdict(self)},
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")


class SessionEncryption:
    def __init__(self, keys: Mapping[str, bytes], active_key_version: str):
        if not isinstance(active_key_version, str) or not _LABEL.fullmatch(active_key_version):
            raise ValueError("Invalid active encryption key version")
        self._keys = dict(keys)
        if active_key_version not in self._keys:
            raise ValueError("Active encryption key is not configured")
        for version, key in self._keys.items():
            if not isinstance(version, str) or not _LABEL.fullmatch(version):
                raise ValueError("Invalid encryption key version")
            if not isinstance(key, bytes) or len(key) != 32:
                raise ValueError("Each encryption key must contain exactly 32 bytes")
        self.active_key_version = active_key_version

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None):
        env = os.environ if environment is None else environment
        try:
            encoded_keys = json.loads(env["TELEGRAM_SESSION_KEYS"])
            if not isinstance(encoded_keys, dict):
                raise ValueError
            keys = {version: _decode(key) for version, key in encoded_keys.items()}
            return cls(keys, env["TELEGRAM_SESSION_ACTIVE_KEY_VERSION"])
        except (KeyError, TypeError, ValueError, UnicodeError):
            raise ValueError("Invalid Telegram session encryption configuration") from None

    def encrypt(self, session_string: str, context: SessionContext) -> str:
        if not isinstance(session_string, str) or not session_string.strip():
            raise ValueError("Session string must be nonempty")
        plaintext = session_string.encode("utf-8")
        if len(plaintext) > _MAX_SESSION_BYTES:
            raise ValueError("Session string exceeds the allowed size")
        version = self.active_key_version
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._keys[version]).encrypt(
            nonce, plaintext, context.associated_data(version),
        )
        payload = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
        return f"tg1.{version}.{payload}"

    def decrypt(self, envelope: str, context: SessionContext) -> str:
        try:
            if not isinstance(envelope, str) or len(envelope) > 23000:
                raise ValueError
            format_version, key_version, payload = envelope.split(".")
            if format_version != "tg1" or key_version not in self._keys:
                raise ValueError
            packed = _decode(payload)
            if not 29 <= len(packed) <= _MAX_SESSION_BYTES + 28:
                raise ValueError
            plaintext = AESGCM(self._keys[key_version]).decrypt(
                packed[:12], packed[12:], context.associated_data(key_version),
            )
            result = plaintext.decode("utf-8")
            if not result.strip():
                raise ValueError
            return result
        except (InvalidTag, ValueError, TypeError, UnicodeError):
            raise EncryptionError("Unable to authenticate Telegram session credential") from None

    def rotate(self, envelope: str, context: SessionContext) -> str:
        """Re-encrypt using the active key; callers persist with a version check."""
        return self.encrypt(self.decrypt(envelope, context), context)
