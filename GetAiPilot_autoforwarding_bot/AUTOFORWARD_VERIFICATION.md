# AutoForward verification — 2026-10-03

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
