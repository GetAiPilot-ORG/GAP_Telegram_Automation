import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from telegram_common.autoforward_inventory import inventory


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def session(self, name="123_919999999999.session", key=b"x" * 256):
        path = self.root / name
        with closing(sqlite3.connect(path)) as db:
            with db:
                db.executescript("CREATE TABLE sessions(auth_key BLOB); CREATE TABLE entities(id INTEGER); CREATE TABLE update_state(id INTEGER);")
                db.execute("INSERT INTO sessions VALUES (?)", (key,))
        return path

    def test_inventory_does_not_modify_or_export_credentials(self):
        path = self.session()
        before = path.read_bytes()
        result = inventory(self.root)
        self.assertEqual(result["files"][0]["status"], "identity_check_required")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [path])
        self.assertNotIn("919999999999", json.dumps(result))
        self.assertNotIn("x" * 256, json.dumps(result))
        self.assertFalse(result["remote_identity_verified"])

    def test_journal_and_symlink_are_rejected(self):
        path = self.session()
        Path(str(path) + "-journal").touch()
        (self.root / "456_918888888888.session").symlink_to(path)
        self.assertEqual([r["status"] for r in inventory(self.root)["files"]],
                         ["snapshot_required", "unsafe_file"])

    def test_missing_key_and_invalid_database(self):
        self.session(key=None)
        (self.root / "456_918888888888.session").write_text("invalid")
        self.assertEqual([r["status"] for r in inventory(self.root)["files"]],
                         ["reauth_required", "invalid_session_database"])

    def test_bot_files_ignored_and_duplicate_owners_flagged(self):
        self.session()
        self.session("123_918888888888.session")
        self.session("login_bot_runner.session")
        result = inventory(self.root)
        self.assertEqual(result["user_session_count"], 2)
        self.assertTrue(all(r["duplicate_owner"] for r in result["files"]))


if __name__ == "__main__":
    unittest.main()
