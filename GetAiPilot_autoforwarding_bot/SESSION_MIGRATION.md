# AutoForward database sessions — milestone 3

Status: implemented and locally verified. Production AutoForward migrations,
configuration, account migration and real Telegram checks remain deployment work.
Flags default to false. No production credentials were migrated here.

## Implemented behavior

- Private `/login` opens a five-minute, single-use website link. Its verified
  dashboard owner must match the linked Telegram sender. The page strips the
  fragment before analytics load. Only token hashes are stored in the database;
  browser storage contains grant/attempt UUIDs, never OTPs or passwords.
- Purpose-specific durable attempts restore OTP/password steps after refresh.
  Success verifies the Telegram account ID before saving encrypted credentials.
  Submanager and AutoForward credentials remain separate.
- In-memory SQLite sessions use encrypted database checkpoints, including private
  entity access hashes and update state. Fenced worker leases enforce exclusive
  ownership. Heartbeat failures disconnect clients; expired owners cannot send.
- Persistent source cursors recover messages missed during downtime. Delivery
  receipts retain stable Telegram random IDs and skip completed targets, including
  separate poll captions. Flood waits remain pending with retry deadlines.
- `/stop` persists stopped intent. `/work` after an intentional pause skips the
  pause interval; a crash retains forwarding intent and recovers pending work.
  Logout retains revocation state until remote logout succeeds. Revoked database
  rows never fall back to old files.
- Existing accounts without database credentials retain the legacy path during
  staged migration. Database errors never authorize a file fallback.

The command bot's own `login_bot_runner.session` belongs to milestone 4 with the
other bot runners. This milestone migrates AutoForward user-account sessions.

## Deploy code and schema with flags off

1. Apply the two new dashboard migrations **in order**:
   `20261003002000_autoforward_login_links.sql`, then
   `20261003003000_autoforward_durable_sessions.sql`.
   Foundation/Submanager migrations are prerequisites already applied in your
   environment. Do not rerun previously applied migrations. New payload tables
   and functions are restricted to the backend service role.
2. Install using the actual Python 3.11+ environment used by the PM2 processes,
   from the Telegram repository root:

   ```sh
   venv/bin/pip install -e .
   venv/bin/pip install -r GetAiPilot_autoforwarding_bot/requirements.txt
   ```

   Install backend requirements too if its environment differs. Substitute the
   actual deployed interpreter path if it is not `venv`.
3. Set matching `TELEGRAM_SESSION_KEYS` and
   `TELEGRAM_SESSION_ACTIVE_KEY_VERSION` in backend and AutoForward environments.
   Retain key versions needed by existing Submanager credentials. Use matching
   Telegram API credentials and Supabase project/service-role credentials.
   Never expose service-role or encryption keys in frontend variables.
4. Initially keep backend `AUTOFORWARD_WEB_LOGIN=false` and bot
   `AUTOFORWARD_DATABASE_SESSIONS=false`, `AUTOFORWARD_WEB_LOGIN=false`.
   Additional bot settings are documented in `.env.example`.
5. Build/upload the dashboard with the production backend URL. Confirm
   `/autoforward/login` loads; hosting must serve the SPA for this route.

Retain **one command-bot update-ingestion process**. User leases do not authorize
multiple copies of the same command bot. Identify the actual AutoForward PM2
process using `pm2 list` and `pm2 describe`; do not restart Telesub in its place.

## Migrate existing users

Begin with one controlled account. Confirm its dashboard UUID and Telegram
numeric ID are uniquely linked in `profiles`. Record a timezone-qualified UTC
pause-start timestamp **before** stopping the owning worker. Copy the session
folder into a restricted backup while that worker is stopped. Do not copy active
SQLite databases; tools reject symlinks and journal/WAL sidecars.

From the Telegram repository root:

```sh
venv/bin/python -m telegram_common.autoforward_inventory /restricted/session-snapshot
```

Dry-run the selected file; replace all placeholders:

```sh
venv/bin/python -m telegram_common.migrate_autoforward /restricted/session-snapshot/TELEGRAM_ID_PHONE_DIGITS.session --owner DASHBOARD_UUID --telegram-id TELEGRAM_ID
```

Apply while its original worker remains stopped:

```sh
venv/bin/python -m telegram_common.migrate_autoforward /restricted/session-snapshot/TELEGRAM_ID_PHONE_DIGITS.session --owner DASHBOARD_UUID --telegram-id TELEGRAM_ID --env-file GetAiPilot_autoforwarding_bot/.env --apply --worker-stopped --replay-since PAUSE_START_ISO_TIMESTAMP
```

Dry-run validates the file without checking Telegram identity. Apply performs an
exclusive remote `get_me()` check, encrypts credentials/checkpoints, and atomically
imports existing mapping recovery boundaries. It never deletes or changes the
source file. Repeated import is idempotent and explicitly reports that remote
identity was not verified again in that run.

Set bot `AUTOFORWARD_DATABASE_SESSIONS=true`, keeping website login off, and
restart the owning worker. Check the controlled account's `/status`, `/work`,
private sources, media, filters and downtime recovery. Then migrate remaining
accounts individually with the same procedure. Investigate missing/duplicate
owners or failures. Keep protected source backups throughout a bounded rollback
window. Mapping and subscription eligibility still govern forwarding.

The pause timestamp covers migration downtime for mappings existing at import.
Messages deleted from Telegram before recovery cannot be recovered.

## Enable website login and check production

After the dashboard, schema and database-aware bot are deployed, enable
`AUTOFORWARD_WEB_LOGIN=true` in both backend and bot. Keep bot
`AUTOFORWARD_DATABASE_SESSIONS=true`. Restart both actual PM2 processes.

Validate a controlled account:

1. Private `/login`, matching dashboard sign-in, phone, OTP and successful login.
   Refresh during OTP and two-step password, then confirm connection.
2. Invalid/expired codes, replacement links and repeated clicks. A lost redemption
   response can require a fresh `/login` link; consumed links cannot be reused.
3. Text, media, poll/caption and private-source forwarding with existing filters,
   relay and delays. Restart while forwarding and check missed-message recovery.
4. `/stop` survives restart; `/work` skips the intentional pause interval.
5. A second worker cannot claim the account. On a controlled worker, simulate
   database loss and confirm the forwarding connection stops.
6. Logout remotely revokes the session, retains retry state if revocation fails,
   and prevents reconnecting through the old local file.

Schedule backend-only `tg_autoforward_cleanup()` to expire attempts and prune
old links. Scheduling is not installed automatically. Monitor checkpoint size,
database growth and pending deliveries. Sent receipts intentionally remain for
replay protection; coordinate their retention with source cursors before pruning.

## Rollback and limits

Once database credentials or revocations exist, **do not disable database-session
support and resume old files**. That bypasses ownership/revocation guarantees.
Stop the new worker before rollback and retain a compatible database-aware reader.
A reverse-export rollback tool is not included. Delete backups only after real
production checks and your rollback procedure have been verified.

Snapshots are bounded to 64 MiB uncompressed and 24 million encrypted characters.
Recovery stores message references rather than message bodies. Stable Telegram
random IDs reduce duplicate sends after uncertain responses, subject to Telegram's
deduplication window and source availability. Permanent exactly-once delivery is
not promised.

Local tests use disposable PostgreSQL and fake Telegram clients. Real Telegram
authentication/delivery and VPS configuration need the production checks above.
See `AUTOFORWARD_VERIFICATION.md` for the local results.
