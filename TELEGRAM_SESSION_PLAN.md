# Telegram session storage and login implementation plan

## Scope update — 2026-10-05

Current work is limited to Submanager and AutoForward user-account sessions.
AutoForward keeps the existing bot-chat phone/OTP/2FA prompts with database
storage (`AUTOFORWARD_DATABASE_SESSIONS=true`, `AUTOFORWARD_WEB_LOGIN=false`).
The database-backed chat adapter is implemented, including restart recovery,
resend, cancel and verified Telegram identity. Website login remains optional.
Join/Tracker, AI Chatbot and Broadcast migrations are deferred at the user's
request; the bot-runner milestone below is historical scope, not authorized
current work. No further deployment is authorized until the user chooses it.

Status: foundation and Submanager migrations applied by the user; compatible
Submanager backend/frontend deployed. User confirmed login and refresh work;
two-step password and logout verification remain pending.
AutoForward secure website login, durable attempts, fenced database sessions,
checkpoints, delivery recovery and verified migration tooling are implemented and
locally tested. AutoForward production rollout remains pending.
Date: 2026-10-03

## Guided milestones

1. **Foundation:** credential-table migration, shared encryption helper,
   offline encryption tests, and disposable-database schema checks. Local
   implementation now exists under `telegram_common/`. The migration and
   rollback-only assertions pass on an isolated PostgreSQL 16 test database.
   The user subsequently applied the foundation migration to main Supabase.
2. **Submanager:** durable login attempts, authenticated attempt endpoints,
   local integration checks, and legacy conversion. Implemented behind the
   disabled-by-default `TELEGRAM_DURABLE_LOGIN` flag. PostgreSQL and FastAPI
   tests use fake Telegram clients; controlled real-account and staging
   Supabase verification remain required. See `GetaipilotBackEnd1/DURABLE_LOGIN.md`.
3. **AutoForward:** secure website login and verified per-user migration with
   a brief worker pause. Implementation and local checks are complete; see
   `GetAiPilot_autoforwarding_bot/SESSION_MIGRATION.md`. Preserve old files for
   a bounded rollback window.
4. **Bot runners:** session migration and update recovery for Join/Tracker,
   AI, Broadcast, and the AutoForward command bot. Bot tokens remain a
   separate credential type; do not put tokens into session-string columns.

Use dummy data first, then controlled Telegram accounts and a staging database.
Local verification reduces risk; it does not establish zero production risk.
`*.session` is already ignored in the repository. Never delete local credentials
simply because their names are ignored; verify migration and rollback first.

## Target

Use the existing Supabase/Postgres database for durable Telegram credentials
and expiring login attempts. Keep live network clients in worker memory, with
one owner for each session. Remove runtime dependence on local session files
after verified migrations. A database stores credentials; it cannot store a
live Telegram connection or eliminate concurrency management.

## Scope by service

| Service | Current behavior | Target |
| --- | --- | --- |
| Submanager login backend | Database StringSession, legacy file support, memory-only login state | Encrypted user sessions and durable login attempts; correct legacy conversion |
| AutoForward | User SQLite session files; tg_user_sessions contains filenames; OTP/password entered in bot chat | Encrypted user sessions; website login; commands remain in bot chat |
| AutoForward command bot | login_bot_runner SQLite session | Encrypted bot session while retaining Telethon |
| Join Bot / Join Tracker | sessions/bot_{bot_id} SQLite sessions | Separate encrypted bot sessions and explicit update-worker ownership |
| AI Chatbot | sessions/llm_bot_{bot_id} SQLite sessions | Encrypted bot sessions; preserve Telegram Business behavior |
| Broadcast Bot | sessions/broadcast_master SQLite session | Encrypted bot session initially |
| Subscription Bot / AutoApprove | Bot API token clients | Keep Bot API; protect tokens; no user sessions to migrate |

Do not merge Submanager and AutoForward user sessions automatically. Their
owner IDs differ (dashboard UUID versus Telegram numeric ID), and their
connections perform different tasks. Preserve separate credentials by purpose
until identity linking and worker ownership are verified.

## 1. Introduce shared storage

Add a shared Python package under `telegram_common/`, used by all Telethon
services. Include session encryption, repository access, owner leases, and
client lifecycle management. Pin the tested Telethon version across services.

Create additive database migrations in the dashboard's existing Supabase
migration directory:

- `tg_session_credentials`: UUID primary key, principal kind (`user`/`bot`),
  purpose, optional dashboard owner UUID, optional Telegram owner ID, optional
  bot configuration ID, encrypted session, key version, state (`active`,
  `revoking`, `revoked`, `needs_reauth`), timestamps, and version number.
  Enforce the principal-specific required fields and uniqueness per owner and
  purpose, including nullable-column behavior explicitly.
- `tg_login_attempts`: random attempt ID, verified owner, purpose, encrypted
  pending session and phone/code hash, step, generation, expiry, timestamps,
  and claim/version fields. No stored OTP or two-step password. One active
  attempt per owner and purpose; replacing an attempt invalidates the old one.
- `tg_session_leases`: credential/attempt ID, worker ID, monotonically
  increasing fencing token, lease expiry. Claims and renewals run atomically
  through restricted database functions.

Credential and attempt tables must be backend-only. Deny browser/anon reads
of credential payloads; provide a separate owner-scoped status response with
no secrets. Restrict lease functions to backend callers. Authenticate and
authorize every request before using privileged database access. Initially
keep existing application metadata tables and their foreign keys intact.

Encrypt with authenticated encryption through a vetted library, using a key
provided by deployment secret management rather than stored beside ciphertext
in the database. Include key version and bind owner/purpose as authenticated
metadata where supported. Rotate by reading old versions and rewriting to the
current version. Never log session strings, tokens, OTPs, passwords, full phone
numbers, or raw database payloads containing secrets.

## 2. Make login attempts durable

Implement start, OTP, password, cancel, and restart against explicit attempt
IDs. Bind each attempt to the authenticated owner and service purpose; an ID
alone grants no access.

1. Start obtains an exclusive claim, normalizes the phone and requests the code.
2. Save the pending auth session, phone and returned `phone_code_hash` before
   acknowledging `otp_sent`. Persistence failure returns a storage error.
3. OTP restores that session and matching hash under an atomic claim. A local
   lock alone is insufficient across processes.
4. A password-required response persists the new step and updated session before
   releasing the claim. Passwords exist only for the verification call.
5. Success verifies identity and atomically saves the encrypted credential while
   consuming the attempt. Metadata synchronization is separately retryable and
   must not misreport successful authentication as an invalid OTP.
6. Expired Telegram codes end the attempt and show a fresh-code action. Invalid
   codes allow a bounded retry. Flood-wait responses show the real retry delay.

Use a configurable 10-minute application expiry as an initial ceiling;
Telegram can invalidate a code sooner. Cleanup removes expired attempts.
Requests arriving after replacement, restart, or consumption receive a clear
stale-attempt response. Do not automatically request another code after every
failed verification.

For AutoForward, `/login` opens an authenticated website login flow. Use an
expiring, single-use link bound to the verified Telegram sender and validate
dashboard identity linking; never trust a user ID supplied in a URL. Remove
bot-chat OTP and password collection. Preserve `/work`, `/incoming`,
`/outgoing`, and existing forwarding configuration.

## 3. Control live connection ownership

Cache live clients in memory within the owning worker. Before opening a saved
credential, claim its database lease. Renew while connected; losing ownership
must stop processing, disconnect, and prevent stale credential writes using
the fencing/version token. New workers only take expired leases. Define
graceful shutdown and bounded reconnect behavior.

For each bot, inspect whether Join Bot, Join Tracker, and AI Chatbot are
intentionally processing the same Telegram updates. Give each stored MTProto
session a unique owner; if features share one connection, dispatch updates
inside that owner. Do not point multiple workers at the same credential or
SQLite file. A lease prevents duplicate owners only when workers also stop
using Telegram after losing it.

StringSession preserves authorization but does not replace SQLite's persistent
entity cache and update state. For continuous AutoForward/join/AI workers,
persist required entity access hashes and update checkpoints in a database
Session implementation based on Telethon's Session interface, or prove the
existing application data and reconnection strategy provide equivalent
behavior. Test private channels, restart recovery, and missed/duplicate updates
before removing SQLite. Do not assume switching to StringSession alone is
sufficient for these long-lived listeners.

## 4. Migrate existing credentials without requiring login again

1. Inventory deployed service versions, session files, database rows, worker
   assignments, and verified identities. Local source findings do not establish
   production migration status.
2. Deploy additive schema, encryption key, and shared storage first. Keep a
   feature flag per service for the old read path.
3. Stop the owning worker while migrating each file. Open the SQLite session
   and explicitly export with `StringSession.save(client.session)`; calling
   SQLiteSession's own `save()` does not export a session string.
4. Store encrypted credentials; preserve owner/purpose metadata; reconnect
   exclusively and verify `get_me()` matches the expected principal. Missing
   files or wrong identities require intervention, never an invented session.
5. Encrypt Submanager's existing database strings in place through the new
   credential store. Use a restricted one-time migration tool, not an endpoint
   available to the browser.
6. Cut over verified rows and observe recovery. Quarantine legacy files with
   restricted permissions for a bounded rollback window; do not delete them
   until the new path passes verification. Expire retained backups afterwards.

During rollback, stop the new worker first. Revoked credentials must never be
reactivated by a legacy fallback. Keep migration idempotent with checkpoints;
do not overwrite valid new credentials with stale files on a rerun.

## 5. Fix disconnect and revocation semantics

Separate transport disconnect from user-requested logout. Logout stops jobs,
marks the credential revoking, calls Telegram `log_out()` while credentials
are still available, then deletes/marks revoked and clears caches. If Telegram
is unreachable, record pending revocation and retry; explain that remote
revocation is pending. Reject further work immediately. Remove the current
AutoForward behavior that only deletes the local file and database row.

Preserve channel mappings and subscription/payment data unless the user
explicitly requests their deletion. Bot removal revokes/deletes the relevant
bot credentials separately from user-account logout.

## 6. Rollout order and acceptance

1. Shared storage and durable Submanager login; correct legacy conversion.
2. AutoForward website login, user-session migration, restart/update recovery,
   and remote logout.
3. AutoForward command bot, Broadcast Bot, then Join/Tracker and AI session
   migration after update ownership and entity persistence are resolved.
4. Remove legacy file paths after verification; audit token access for
   Subscription Bot and AutoApprove.

Required tests: overlapping status/start requests; simultaneous workers;
restart between code request and OTP and between OTP and password; resends
and stale generations; real expired/invalid code handling; database outages;
unauthorized attempt access; encryption rotation; migration reruns and identity
mismatches; lease expiry and stale writes; private-channel resolution after
restart; missed/duplicate updates; failed revocation and retry; rollback.

Use mocks for deterministic regression tests, plus controlled test accounts
for Telegram integration checks. Track error category, service, attempt ID,
lease contention, persistence failures, and recovery latency without secrets.
Exit criteria: no runtime user credential files, successful restart recovery,
single ownership, correct identity matching, and working remote logout.

Bot API rewrites are a later feature-by-feature project, not a prerequisite
for moving storage. Confirm parity for invite tracking, Business messages,
broadcasts, and update dispatch first. Bot API polling and webhooks are mutually
exclusive; retain one update-ingestion owner per bot when converting.

## References

- [Telethon sessions and explicit StringSession conversion](https://docs.telethon.dev/en/stable/concepts/sessions.html)
- [Telethon Session interface](https://docs.telethon.dev/en/stable/modules/sessions.html)
- [Telethon entity resolution](https://docs.telethon.dev/en/stable/concepts/entities.html)
- [Telegram Bot API update ingestion](https://core.telegram.org/bots/api#getting-updates)
