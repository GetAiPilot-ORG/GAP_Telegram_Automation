# AutoForward verification — 2026-10-03

Cleanup note (2026-10-05): migration-specific test suites and disposable test
artifacts were removed at the user's request after verification. Results below
are historical; test commands and fixture paths no longer exist in this checkout.
Runtime credentials/session files and implementation code were preserved.

## Completion — 2026-10-05, bot-chat flow

The gap identified in the recheck below is now fixed. In database mode with
website login disabled, existing phone/OTP/password prompts call
`telegram_common/autoforward_chat.py` and the durable AutoForward runtime.
The handler bypasses file creation and filename persistence for new logins.
Pending OTP/2FA attempts resume after restart; resend replaces the hash/session
atomically; cancellation works without in-memory state. Invalid passwords remain
retryable. Expired attempts can be cancelled/restarted. Identity checks reject
unlinked, ambiguous or incorrect Telegram accounts before credentials are saved.

Current verification: **78 shared tests passed with no skips**, using a fresh
disposable PostgreSQL 16 instance and dummy identities; **3 existing backend
tests passed**. The 78 include 13 isolated bot handler tests and four new
database chat-flow scenarios. Package-wheel build, inclusion of the new adapter,
Python compilation and whitespace checks passed. No frontend changes were
needed; the October 3 browser results below are historical, not rerun here.

Local bot configuration retains `AUTOFORWARD_DATABASE_SESSIONS=true` and now
sets `AUTOFORWARD_WEB_LOGIN=false`; backend AutoForward web-login is also false.
No running bot was restarted, no real login was performed and nothing was
deployed. Existing local session files were neither read nor changed by these
checks. Their already-present working-tree changes belong to the user's local
activity. Apply the existing AutoForward migrations/configuration to the chosen
environment before running this feature. No new SQL migration is required.

The initial phone prompt still requires `/login` again if a restart occurs
before phone submission. OTP/password values are passed through only and are
not stored in database state or bot memory state. Existing accounts require
explicit file migration or retain the legacy compatibility path until then.
The AutoForward command bot retains its own session. Join/Tracker, AI Chatbot
and Broadcast remain unchanged and outside scope. Telegram delivery and real
account behavior still need a controlled real-account check when authorized.

## Recheck — 2026-10-05, revised scope

The user requires the existing bot-chat phone/OTP/password flow and has excluded
Join/Tracker, AI Chatbot and Broadcast from this migration. No deployment is
authorized until the scoped work is finished.

AutoForward is **not complete for this revised requirement**. Its `login_flow`
still creates `TelegramClient(session_path(...))` and calls `sp_upsert_session`
to record filenames after phone/OTP/password authentication. The database-aware
worker and website login do not convert new bot-chat logins. The command bot
also retains its own SQLite session. Existing local session files must be kept.

The current offline suite ran 67 tests: 30 passed and 37 database integration
tests were skipped because the disposable PostgreSQL socket was unavailable.
All three backend login-state regression tests passed. These results do not
replace the October 3 database/browser checks or establish production readiness.

Submanager's durable backend endpoints and dashboard attempt restoration remain
implemented. The remaining AutoForward requirement is database-backed bot-chat
login with unchanged prompts, including restart, resend, cancellation, identity
checks and logout coverage. Other bot runners remain outside the authorized scope.

## Original website-flow verification

Implementation complete for milestone 3 user sessions. Production rollout is
pending. No production Telegram credentials or Supabase data were modified by
these checks. New feature flags remain disabled by default.

## Results

| Check | Result |
| --- | --- |
| Shared Python suite, real disposable PostgreSQL 16 | 67 passed |
| Existing backend login-state regressions | 3 passed |
| AutoForward browser scenarios, intercepted backend | 6 passed |
| Existing Submanager browser scenarios | 7 passed |
| Production dashboard build | Passed |
| Shared package wheel, isolated build | Passed |
| Python compilation, migration CLI help, both repositories' whitespace checks | Passed |
| Full application TypeScript check | Blocked by existing syntax errors outside AutoForward |
| Narrow AutoForward TypeScript check with real dependencies | Blocked by existing AuthContext/CRM type errors; no AutoForward errors reported |
| Real Telegram authentication, forwarding, VPS rollout | Not performed |

The root `tsc --noEmit` command has an empty root file list and is not a meaningful
application check. `tsc --noEmit -p tsconfig.app.json` reports syntax errors in
`src/lib/landing-zip-generator.ts:26` and
`src/pages/telegram/Telesub/telesubDashbaord.tsx:115`. A narrower configuration
including the AutoForward page, token helper and their dependencies reports
existing `AuthContext.tsx`/`CRMPage.tsx` errors. These files were not edited for
AutoForward. Vite's successful production build does not replace a type check.

## Coverage

Database tests cover owner-bound, hashed, expiring single-use links; replacement
and concurrent redemption; purpose isolation; OTP/password attempt recovery;
incorrect Telegram identity; authenticated HTTP endpoints; disabled feature flags;
restricted browser roles; credential versioning; fenced leases and checkpoints;
revocation retry; verified idempotent migration; immutable source files; private
entity/update-state preservation; in-memory schema upgrades; heartbeat failure;
stable delivery random IDs; multipart receipts; uncertain-response replay;
source recovery; filtering; and intentional-pause cursor reset.

Six isolated bot integration tests execute extracted functions without importing
the entrypoint that starts Telegram. They verify active database routing,
revocation/outage fallback prevention, unmigrated account compatibility, ignoring
chat secrets in web mode, and persisting repeated `/stop` requests.

Browser tests use the real AutoForward page in StrictMode with dummy dashboard
identities. They cover one-use token removal/redemption, OTP refresh, password
refresh without stored secrets, expired codes, restart cancellation, repeated
clicks and user changes during verification. All external requests are blocked.
Desktop and mobile screens were visually inspected. Submanager's seven scenarios
remain passing after shared-code changes.

## Reproduce

From the Telegram repository root, using a disposable database with the four
Telegram migrations and test auth/profile fixtures:

```sh
TELEGRAM_TEST_PG_SOCKET=/path/to/test/socket TELEGRAM_TEST_PG_DATABASE=test_database venv/bin/python -m unittest discover -s telegram_common/tests -v
venv/bin/python -m unittest discover -s GetaipilotBackEnd1/tests -v
venv/bin/pip wheel --no-deps . -w /tmp/telegram-package-check
```

Database fixtures/tests are for a disposable environment only. Never point them
at production. Browser instructions are in the dashboard's
`tests/autoforward-login/README.md`; run the two browser suites sequentially.

## Deployment requirements

Follow `SESSION_MIGRATION.md`: apply only the two new AutoForward migrations,
deploy code/dashboard, configure matching encryption/service-role/API settings,
verify one paused-worker migration, then enable web login and test a controlled
account. Schedule attempt/link cleanup separately. Retain protected source files
through a verified rollback window and keep the database-aware reader enabled
after credentials or revocations exist.

Mocks cannot prove Telegram server behavior, deduplication duration or VPS process
configuration. Delivery recovery depends on source availability and Telegram's
random-ID deduplication window; permanent exactly-once delivery is not promised.
The command bot's own session-file conversion remains milestone 4.
