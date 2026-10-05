"""Offline AutoForward migration inventory; never exports authentication keys.

Run against a snapshot made while the owning worker is stopped. Immutable SQLite
reads avoid modifying files and deliberately do not merge WAL/journal changes.
Files with journals are therefore rejected rather than declared migration-ready.
"""

import argparse
import json
from pathlib import Path
import re
import sqlite3


_USER_FILE = re.compile(r"([1-9][0-9]*)_[0-9]+\.session\Z")


def inventory(directory):
    root = Path(directory)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("Use a real session snapshot directory")
    results = []
    for path in sorted(root.glob("*.session")):
        match = _USER_FILE.fullmatch(path.name)
        if not match:
            continue  # Command-bot sessions are a separate migration.
        row = {"telegram_owner_id": int(match[1]), "status": "unverified"}
        results.append(row)
        if path.is_symlink() or not path.is_file():
            row["status"] = "unsafe_file"
            continue
        if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
            row["status"] = "snapshot_required"
            continue
        connection = None
        try:
            connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
            sessions = connection.execute(
                "SELECT count(*), sum(CASE WHEN length(auth_key) = 256 THEN 1 ELSE 0 END) FROM sessions"
            ).fetchone()
            row["has_auth_key"] = sessions[0] == 1 and sessions[1] == 1
            for table in ("entities", "update_state"):
                row[table + "_count"] = connection.execute("SELECT count(*) FROM " + table).fetchone()[0]
            row["status"] = "identity_check_required" if row["has_auth_key"] else "reauth_required"
        except sqlite3.Error:
            row["status"] = "invalid_session_database"
        finally:
            if connection is not None:
                connection.close()
    owners = [r["telegram_owner_id"] for r in results]
    for row in results:
        if owners.count(row["telegram_owner_id"]) > 1:
            row["duplicate_owner"] = True
    return {"files": results, "user_session_count": len(results), "remote_identity_verified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot_directory")
    args = parser.parse_args()
    try:
        result = inventory(args.snapshot_directory)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
