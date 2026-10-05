"""Pinned Telethon SQLite schema in memory, with encrypted database checkpoints.

SQLite serialization preserves authorization, entity access hashes, sent-file
cache, and update state. No runtime .session file is opened or created.
"""
import base64
from dataclasses import replace
import os
import sqlite3
import zlib

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession, StringSession
from telethon.sessions.sqlite import CURRENT_VERSION

from .encryption import EncryptionError
from .login import credential_context

MAX_SNAPSHOT = 64 * 1024 * 1024
MAX_ENVELOPE = 24_000_000


def snapshot_context(row):
    return replace(credential_context(row), purpose="autoforward_checkpoint")


def encrypt_snapshot(cipher, data, row):
    if not isinstance(data, bytes) or len(data) > MAX_SNAPSHOT or not data.startswith(b"SQLite format 3\x00"):
        raise ValueError("Invalid or oversized session snapshot")
    version = cipher.active_key_version
    nonce = os.urandom(12)
    encrypted = AESGCM(cipher._keys[version]).encrypt(nonce, zlib.compress(data), snapshot_context(row).associated_data(version))
    result = "tgcp1." + version + "." + base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")
    if len(result) > MAX_ENVELOPE:
        raise ValueError("Session checkpoint exceeds storage limit")
    return result


def decrypt_snapshot(cipher, envelope, row):
    try:
        if not isinstance(envelope, str) or len(envelope) > MAX_ENVELOPE:
            raise ValueError
        prefix, version, payload = envelope.split(".")
        if prefix != "tgcp1":
            raise ValueError
        raw = base64.b64decode(payload, altchars=b"-_", validate=True)
        compressed = AESGCM(cipher._keys[version]).decrypt(raw[:12], raw[12:], snapshot_context(row).associated_data(version))
        decoder = zlib.decompressobj()
        result = decoder.decompress(compressed, MAX_SNAPSHOT + 1)
        if len(result) > MAX_SNAPSHOT or not decoder.eof or decoder.unused_data or not result.startswith(b"SQLite format 3\x00"):
            raise ValueError
        return result
    except Exception:
        raise EncryptionError("Unable to authenticate session checkpoint") from None


class DatabaseSession(SQLiteSession):
    def __init__(self, session_string="", snapshot=None):
        super().__init__(None)
        if snapshot is not None:
            self._conn.deserialize(snapshot)
            version = self._conn.execute("SELECT version FROM version").fetchone()[0]
            if not 6 <= version <= CURRENT_VERSION:
                raise ValueError("Unsupported Telethon checkpoint version")
            if version < CURRENT_VERSION:
                self._upgrade_database(version)
                self._conn.execute("UPDATE version SET version=?", (CURRENT_VERSION,))
                self.save()
            values = self._conn.execute("SELECT * FROM sessions").fetchone()
            if not values or len(values) != 6:
                raise ValueError("Unsupported Telethon checkpoint schema")
            self._dc_id, self._server_address, self._port, key, self._takeout_id, temporary = values
            self._auth_key = AuthKey(data=key)
            self._tmp_auth_key = AuthKey(data=temporary)
        elif session_string:
            source = StringSession(session_string)
            self.set_dc(source.dc_id, source.server_address, source.port)
            self.auth_key = source.auth_key

    def snapshot(self):
        self.save()
        return self._conn.serialize()

    def dispose(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @staticmethod
    def read_snapshot(path):
        """Read a stopped-worker snapshot; callers reject journals/symlinks first."""
        source = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        target = sqlite3.connect(":memory:")
        try:
            source.backup(target)
            target.execute("PRAGMA journal_mode=MEMORY")
            return target.serialize()
        finally:
            source.close()
            target.close()
