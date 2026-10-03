# Durable login verification — 2026-10-03

The database migrations are installed, and the automated checks passed.
The new durable login flow is not deployed or enabled on the live backend.
Applying the migrations alone does not fix the current production OTP issue.

## Main Supabase checks

- Both `tg_session_credentials` and `tg_login_attempts` accept service-role
  queries selecting the expected columns. Both tables currently contain zero
  rows. Queries used `limit=0`; no user credentials were downloaded.
- All seven new RPCs appear in the service-role PostgREST schema and none
  appear in the anonymous schema.
- Begin, claim, save, complete, and cancel reject deliberately invalid or
  nonexistent attempts with `TG_ATTEMPT_STALE`. Revoke with a nonexistent owner
  returns a no-op. No test users, attempts, or credentials were created.
- Anonymous access to both tables and the six probed RPCs returns permission
  denied. Cleanup was not invoked against production because it mutates data.
- Live authenticated-user grants were not tested without a user JWT. Local
  PostgreSQL tests cover browser-role grant restrictions.

## Automated verification

| Check | Result |
| --- | --- |
| Encryption tests | 14 passed |
| Durable login, SQL, and ASGI HTTP tests | 18 passed |
| Legacy login regression tests | 3 passed |
| Isolated Chromium dashboard scenarios | 7 passed |
| Frontend production build | Passed |
| Full-project TypeScript check | Blocked by existing syntax errors |

PostgreSQL tests use a disposable PostgreSQL 16.2 cluster with the migrations
installed. They exercise claims, expiry, retry, cancellation, credential
completion, metadata recovery, and access controls with fake Telegram clients.
They do not log into Telegram.

Chromium tests load the real dashboard with dummy authentication and Supabase
responses. External requests are blocked. Coverage includes OTP refresh,
password refresh without storing secrets, mobile expired-code recovery,
retryable invalid codes, explicit restart/cancellation, repeated clicks, and
discarding responses after switching dashboard users. The test harness is in
`bot-dashboard/getaipilot.in/tests/submanager-login/`.

Existing TypeScript syntax errors are in `src/lib/landing-zip-generator.ts:26`
and `src/pages/telegram/Telesub/telesubDashbaord.tsx:115`.

## Deployment status and remaining checks

`https://tg.getaipilot.in/health` returns healthy. Unauthenticated Telegram
status requests return 401. Its OpenAPI schema still has an OTP body containing
only `otp`, with no attempt resume/cancel endpoints: the new backend is not live.

The local backend flag is unset, so durable login defaults off. Its service-role
and encryption settings still need configuration. The local frontend points to
`http://127.0.0.1:8000`, where no backend was running during verification.

Next, configure backend-only secrets, install the shared package, and deploy
the compatible backend/frontend according to `DURABLE_LOGIN.md`. Verify the
enabled flow using a controlled Telegram account before enabling it for users.
Real OTP delivery, Telegram code expiry, two-step password verification, and
remote logout remain unverified against Telegram itself. Keep one backend worker
until the session ownership milestone is implemented.
