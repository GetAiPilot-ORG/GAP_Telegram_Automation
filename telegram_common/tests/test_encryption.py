import base64
from dataclasses import replace
import json
import unittest
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from telegram_common import EncryptionError, SessionContext, SessionEncryption


class SessionEncryptionTests(unittest.TestCase):
    def setUp(self):
        self.keys = {"v1": AESGCM.generate_key(bit_length=256)}
        self.encryption = SessionEncryption(self.keys, "v1")
        self.context = SessionContext(
            credential_id=str(uuid4()), principal_kind="user", purpose="submanager",
            dashboard_owner_id=str(uuid4()),
        )
        self.session = "dummy-session-for-local-tests-only"

    def test_roundtrip(self):
        encrypted = self.encryption.encrypt(self.session, self.context)
        self.assertTrue(encrypted.startswith("tg1.v1."))
        self.assertNotIn(self.session, encrypted)
        self.assertEqual(self.encryption.decrypt(encrypted, self.context), self.session)

    def test_same_plaintext_has_fresh_nonce(self):
        first = self.encryption.encrypt(self.session, self.context)
        second = self.encryption.encrypt(self.session, self.context)
        self.assertNotEqual(first, second)

    def test_tampered_ciphertext_and_nonce_are_rejected(self):
        encrypted = self.encryption.encrypt(self.session, self.context)
        payload = bytearray(base64.urlsafe_b64decode(encrypted.split(".")[2]))
        for index in (0, 12, len(payload) - 1):
            with self.subTest(index=index):
                modified = payload.copy()
                modified[index] ^= 1
                tampered = "tg1.v1." + base64.urlsafe_b64encode(modified).decode("ascii")
                with self.assertRaises(EncryptionError):
                    self.encryption.decrypt(tampered, self.context)

    def test_copying_to_another_owner_purpose_or_row_is_rejected(self):
        encrypted = self.encryption.encrypt(self.session, self.context)
        for context in (
            replace(self.context, dashboard_owner_id=str(uuid4())),
            replace(self.context, purpose="autoforward"),
            replace(self.context, credential_id=str(uuid4())),
            replace(self.context, telegram_owner_id=123456789),
        ):
            with self.subTest(context=context), self.assertRaises(EncryptionError):
                self.encryption.decrypt(encrypted, context)

    def test_bot_identity_is_authenticated(self):
        context = SessionContext(str(uuid4()), "bot", "join", bot_configuration_id="join-master")
        encrypted = self.encryption.encrypt(self.session, context)
        self.assertEqual(self.encryption.decrypt(encrypted, context), self.session)
        with self.assertRaises(EncryptionError):
            self.encryption.decrypt(encrypted, replace(context, bot_configuration_id="another-bot"))

    def test_wrong_key_is_rejected(self):
        encrypted = self.encryption.encrypt(self.session, self.context)
        wrong = SessionEncryption({"v1": AESGCM.generate_key(bit_length=256)}, "v1")
        with self.assertRaises(EncryptionError):
            wrong.decrypt(encrypted, self.context)

    def test_rotation_reads_old_key_and_writes_new_key(self):
        encrypted = self.encryption.encrypt(self.session, self.context)
        new_key = AESGCM.generate_key(bit_length=256)
        rotated_cipher = SessionEncryption({**self.keys, "v2": new_key}, "v2")
        rotated = rotated_cipher.rotate(encrypted, self.context)
        self.assertTrue(rotated.startswith("tg1.v2."))
        only_new = SessionEncryption({"v2": new_key}, "v2")
        self.assertEqual(only_new.decrypt(rotated, self.context), self.session)
        with self.assertRaises(EncryptionError):
            only_new.decrypt(encrypted, self.context)

    def test_key_version_is_authenticated(self):
        encrypted = self.encryption.encrypt(self.session, self.context)
        alias = SessionEncryption({"v1": self.keys["v1"], "v2": self.keys["v1"]}, "v2")
        with self.assertRaises(EncryptionError):
            alias.decrypt(encrypted.replace("tg1.v1.", "tg1.v2."), self.context)

    def test_malformed_envelopes_fail_without_echoing_input(self):
        for value in (None, "", "plaintext-secret", "tg2.v1.abc", "tg1.unknown.abc",
                      "tg1.v1.@@@@", "tg1.v1." + "A" * 25000):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaises(EncryptionError) as error:
                    self.encryption.decrypt(value, self.context)
                self.assertEqual(str(error.exception), "Unable to authenticate Telegram session credential")

    def test_invalid_key_configuration_is_rejected(self):
        for keys, active in (({}, "v1"), ({"v1": b"short"}, "v1"),
                             ({"bad.version": self.keys["v1"]}, "bad.version")):
            with self.subTest(active=active), self.assertRaises(ValueError):
                SessionEncryption(keys, active)

    def test_environment_configuration(self):
        env = {
            "TELEGRAM_SESSION_KEYS": json.dumps({"v1": base64.urlsafe_b64encode(self.keys["v1"]).decode()}),
            "TELEGRAM_SESSION_ACTIVE_KEY_VERSION": "v1",
        }
        cipher = SessionEncryption.from_environment(env)
        encrypted = cipher.encrypt(self.session, self.context)
        self.assertEqual(cipher.decrypt(encrypted, self.context), self.session)
        for invalid in ({}, {**env, "TELEGRAM_SESSION_KEYS": "[]"},
                        {**env, "TELEGRAM_SESSION_KEYS": '{"v1":"secret-invalid-key"}'}):
            with self.assertRaises(ValueError) as error:
                SessionEncryption.from_environment(invalid)
            self.assertEqual(str(error.exception), "Invalid Telegram session encryption configuration")

    def test_empty_and_oversized_plaintext_is_rejected(self):
        for value in (None, "", "  ", "x" * 16385):
            with self.assertRaises(ValueError):
                self.encryption.encrypt(value, self.context)

    def test_invalid_identity_is_rejected(self):
        for kwargs in (
            {"principal_kind": "other"}, {"purpose": ""},
            {"dashboard_owner_id": None}, {"bot_configuration_id": "bot"},
            {"telegram_owner_id": True}, {"telegram_owner_id": -1},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                replace(self.context, **kwargs)

    def test_uuid_spelling_is_canonical(self):
        canonical = self.context
        uppercase = replace(canonical, credential_id=canonical.credential_id.upper())
        self.assertEqual(canonical.associated_data("v1"), uppercase.associated_data("v1"))


if __name__ == "__main__":
    unittest.main()
