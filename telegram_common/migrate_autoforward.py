"""Single-account file-to-DB migration. Dry-run by default; never deletes files."""
import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from .database_session import DatabaseSession, encrypt_snapshot
from .login import LoginError, canonical_id, credential_context
from .autoforward_inventory import _USER_FILE


def validate_file(path, telegram_id):
    path = Path(path)
    match = _USER_FILE.fullmatch(path.name)
    if path.is_symlink() or not path.is_file() or not match or int(match[1]) != telegram_id:
        raise ValueError("Expected a regular session snapshot belonging to this Telegram owner")
    if any(Path(str(path) + suffix).exists() for suffix in ("-journal", "-wal", "-shm")):
        raise ValueError("Use a clean snapshot made while the owning worker is stopped")
    return path


async def migrate_one(store, cipher, path, owner, telegram_id, client_factory, replay_since=None, sources=None):
    owner = canonical_id(owner)
    path = validate_file(path, telegram_id)
    existing = await store.telegram_credential(telegram_id)
    if existing:
        if existing["dashboard_owner_id"] != owner or existing["state"] != "active":
            raise LoginError("credential_changed", "Existing credential requires intervention.", 409)
        return {"credential_id": existing["id"], "already_imported": True, "remote_verified_this_run": False}
    session = DatabaseSession(snapshot=DatabaseSession.read_snapshot(path))
    client = client_factory(session)
    try:
        async with asyncio.timeout(45):
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    raise LoginError("reauth_required", "Session requires login.", 409)
                me = await client.get_me()
                if not me or getattr(me, "bot", False) or me.id != telegram_id:
                    raise LoginError("identity_mismatch", "Session does not match its expected owner.", 409)
            finally:
                await asyncio.wait_for(client.disconnect(), 5)
            from telethon.sessions import StringSession
            row = {"id": str(uuid4()), "principal_kind": "user", "purpose": "autoforward",
                   "dashboard_owner_id": owner, "telegram_owner_id": telegram_id}
            result = await store.rpc("tg_autoforward_import", {"p_credential": row["id"], "p_owner": owner,
                "p_telegram": telegram_id, "p_session": cipher.encrypt(StringSession.save(session), credential_context(row)),
                "p_key": cipher.active_key_version, "p_snapshot": encrypt_snapshot(cipher, session.snapshot(), row),
                "p_replay_since": replay_since, "p_sources": sources or []})
            return {"credential_id": result["id"], "already_imported": result["already_imported"], "remote_verified_this_run": True}
    finally:
        session.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_file")
    parser.add_argument("--owner", required=True, help="Verified dashboard UUID")
    parser.add_argument("--telegram-id", type=int, required=True)
    parser.add_argument("--env-file", help="Backend-only .env file; never print its contents")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--worker-stopped", action="store_true")
    parser.add_argument("--replay-since", help="Timezone-qualified ISO timestamp recorded before pausing forwarding")
    args = parser.parse_args()
    try:
        canonical_id(args.owner)
        path = validate_file(args.session_file, args.telegram_id)
        if not args.apply:
            session = DatabaseSession(snapshot=DatabaseSession.read_snapshot(path))
            try:
                if not session.auth_key:
                    raise ValueError("Session has no authorization key")
            finally:
                session.dispose()
            print(json.dumps({"status": "dry_run", "remote_identity_verified": False, "files_deleted": False}))
            return
        if not args.worker_stopped:
            parser.error("Stop the owning worker and pass --worker-stopped before applying")
        from datetime import datetime, timezone
        if not args.replay_since:
            parser.error("Record the pause-start timestamp and pass --replay-since to cover migration downtime")
        replay = datetime.fromisoformat(args.replay_since.replace("Z", "+00:00"))
        if replay.tzinfo is None or replay > datetime.now(timezone.utc):
            raise ValueError("Expected a past timezone-qualified replay timestamp")
        import os
        from dotenv import load_dotenv
        from supabase import create_client, ClientOptions
        from telethon import TelegramClient
        from .autoforward import AutoForwardStore
        from .encryption import SessionEncryption
        if args.env_file:
            load_dotenv(args.env_file)
        store = AutoForwardStore(create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"],
                                               options=ClientOptions(postgrest_client_timeout=8)))
        source_rows = store.client.table("tg_forward_mappings").select("sender_id").eq("user_id", args.telegram_id).execute()
        sources = list({int(row["sender_id"]) for row in source_rows.data or []})
        api_id = int(os.getenv("API_ID") or os.environ["TELEGRAM_API_ID"])
        api_hash = os.getenv("API_HASH") or os.environ["TELEGRAM_API_HASH"]
        result = asyncio.run(migrate_one(store, SessionEncryption.from_environment(), path, args.owner, args.telegram_id,
            lambda session: TelegramClient(session, api_id, api_hash, receive_updates=False, connection_retries=1, flood_sleep_threshold=0),
            replay_since=replay.isoformat(), sources=sources))
        print(json.dumps(result))
    except LoginError as error:
        parser.error(error.message)
    except Exception:
        parser.error("Migration failed. Check backend configuration, snapshot validity, and exclusive worker ownership. Source files are retained.")


if __name__ == "__main__":
    main()
