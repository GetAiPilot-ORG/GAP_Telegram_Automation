# Submanager durable login (Milestone 2)

The new flow is opt-in: `TELEGRAM_DURABLE_LOGIN=false` remains the default.
Local tests use actual PostgreSQL and fake Telegram clients. The user applied
the production database migrations; subsequent live checks verified table and
RPC availability without creating login records. No real Telegram login has
been performed by these tests. See `DURABLE_LOGIN_VERIFICATION.md` for results.

## Prepare locally/staging

1. Apply these dashboard migrations, in order, to your test Supabase database:
   - `20261003000000_telegram_session_credentials.sql`
   - `20261003001000_submanager_durable_login.sql`
2. Use Python 3.11+ and install the shared package from the Telegram repository
   root with `python -m pip install -e .`. Install the backend requirements too.
   The shared package pins Telethon 1.45.0 and cryptography 50.0.1.
3. Configure the existing Supabase URL/anon key and Telegram API credentials,
   plus a **backend-only** `SUPABASE_SERVICE_ROLE_KEY` and encryption keyring.
   See `.env.example` and `../telegram_common/README.md`. Generate and retain a
   real random key in your secret manager; the example placeholders cannot run.
4. Set `TELEGRAM_DURABLE_LOGIN=true` on the test backend. Run from this directory:
   `uvicorn Telesub:app --host 127.0.0.1 --port 8000 --workers 1`.
5. Point the local dashboard's `VITE_TELEGRAM_SERVICE_URL` to
   `http://localhost:8000` and log into the **test** Supabase project. Confirm
   the dashboard and backend use the same Supabase project.

Keep one backend worker/replica during this milestone. Database claims protect
pending login operations across processes, but completed-session ownership and
continuous worker leases are a later milestone. This rollout does not support
multiple live backend workers sharing saved Telegram credentials.

## What changed

- Start returns an owner-bound attempt UUID and application expiry. The DB stores
  an encrypted JSON envelope containing the pending Telegram auth key/session,
  phone, and matching code hash. It never stores OTPs or passwords.
- OTP and password requests include `attempt_id`; password requests use the
  attempt's phone/session rather than trusting a caller-supplied phone.
- Each operation exclusively claims the attempt for 120 seconds, has a
  45-second operation timeout, and disconnects before releasing ownership.
  After a crashed request, the lease may require waiting up to 120 seconds.
- Attempts have a 10-minute application expiry. Telegram can reject a code
  sooner. Five invalid verifications end the attempt; FloodWait delays are
  honored in the database even when the user starts another attempt.
- Completing authentication atomically saves the encrypted credential and
  consumes the attempt. A database failure leaves it resumable: a retry checks
  whether Telegram already authenticated that auth key before using the OTP.
- Profile/subscription metadata failures leave `metadata_pending=true`; login
  remains successful, and later authenticated status checks retry synchronization.
- Refresh recovery stores only the attempt UUID in browser session storage,
  scoped by dashboard user. OTP/password fields are cleared on refresh, success,
  restart, and owner changes. A new-code action cancels the current attempt.
- Existing sessions are still readable when no new credential row exists.
  Legacy SQLite conversion explicitly uses `StringSession.save(client.session)`.
  This is not a bulk migration, and no legacy files are deleted by this rollout.
- Once an encrypted row exists, revoked/needs_reauth state cannot fall back to
  an old plaintext session. Remote logout retains credentials in `revoking`
  state until Telegram confirms logout. A failed revocation must be retried.

Errors use a structured `detail` with `code`, `message`, `restart`, and optional
`retry_after`. The frontend still accepts the old backend's string errors and
responses without attempt IDs while the feature is disabled.

## Cleanup and verification

Schedule the restricted `tg_login_cleanup()` RPC periodically in your backend
or database scheduler. It clears expired payloads and removes terminal attempts
after one day, retaining unexpired FloodWait cooldowns. No production scheduler
is created automatically.

Offline checks from the repository root:

```sh
python -m unittest discover -s telegram_common/tests -v
python -m unittest discover -s GetaipilotBackEnd1/tests -v
```

The PostgreSQL-backed tests intentionally skip without a disposable Unix socket:

```sh
TELEGRAM_TEST_PG_SOCKET=/tmp/tg-session-test-socket \
TELEGRAM_TEST_PG_PORT=55439 \
python -m unittest telegram_common.tests.test_durable_login -v
```

The isolated cluster needs Supabase-compatible `anon`, `authenticated`, and
`service_role` roles, `auth.users(id uuid primary key)`, and both migrations.
These fixtures exercise SQL grants/transactions; they do not reproduce the
Supabase Auth/PostgREST deployment. HTTP tests use FastAPI's ASGI transport at
`http://localhost:8000` with dummy JWT validation and fake Telegram.

Before production, use a controlled Telegram account to test OTP, two-step
password, restart at both steps, stale code after restarting login, expired
code recovery, metadata retries, channel listing, and failed/retried logout.
Verify the real Supabase service-role RPC grants and owner authentication too.

## Deployment order and rollback

Apply additive migrations, install the shared package, deploy the compatible
frontend/backend with the flag off, then enable on staging and verify. Enable
production only after real account checks and secure key configuration.

Do not blindly turn the flag off after encrypted credentials are written:
new-mode metadata contains no plaintext session string. Roll back with the
new reader retained or stop the backend and deliberately migrate credentials
through an authorized encrypted rollback tool. Do not restore revoked sessions
or auto-fallback because a database/key lookup failed. Keep old files untouched
until the separate migration and rollback window have been verified.

## Local verification recorded

Both migrations applied from scratch on isolated PostgreSQL 16.2; the foundation
SQL assertions passed. Encryption, durable login, HTTP, and legacy regression
tests passed with fake Telegram clients. The shared Python wheel builds, and
the dashboard production build passes. Full-project TypeScript checking is
blocked by existing syntax errors in `src/lib/landing-zip-generator.ts` and
`src/pages/telegram/Telesub/telesubDashbaord.tsx`. Seven isolated Chromium tests
also passed against the real dashboard with simulated Telegram responses.
Live Supabase schema, service-role RPC guards, and anonymous access restrictions
were checked after the user applied the migrations. Real-account login and
live authenticated-user permissions remain unverified; the deployed backend
still exposes the old login API.
