# Durable login verification — 2026-10-03

The database migrations are installed, and the automated checks passed.
The updated backend is now deployed and healthy. Its durable-login feature flag
and secret configuration have not been verified remotely. The updated dashboard
is now deployed with the correct production API URL. The user confirmed that
controlled-account login and refresh both work. Two-step password, pending-login
refresh, and logout checks remain unconfirmed; the feature flag itself has not
been inspected remotely.

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

Latest deployment check: live `TelesubDashboard-C24VAfMZ.js` contains
`https://tg.getaipilot.in`, attempt-ID handling, refresh recovery, and cancel
support. It no longer contains the previous localhost API URL. Backend health
still reports healthy. The localhost issue described below is historical.

After the backend deployment, `https://tg.getaipilot.in/health` returns healthy.
Unauthenticated status and attempt-resume requests both return 401. Its OpenAPI
schema now includes attempt IDs in OTP/password requests and the resume/cancel
endpoints, confirming deployment of the updated backend code. These public checks
do not prove the feature flag is enabled or the backend secrets are valid.

On the first deployment check, the user confirmed deploying only the backend,
and the live dashboard still sent only `{otp}`. After the subsequent frontend
deployment, the live dashboard asset `TelesubDashboard-C_h9mP4n.js` now contains
attempt IDs, the resume/cancel routes, and the attempt persistence marker.
However, that bundle contains `http://127.0.0.1:8000` as its Telegram API URL and
does not contain `https://tg.getaipilot.in`. This points visitors' browsers at
their own machines, not the VPS. Rebuild/redeploy with the production API URL
before attempting real-account login.

A fresh local frontend production build passed with
`VITE_TELEGRAM_SERVICE_URL=https://tg.getaipilot.in`. Its built dashboard includes
attempt-ID handling and refresh recovery. This build has not been published.

At the earlier local verification, the backend flag was unset, so durable login
defaulted off. Its service-role
and encryption settings still need configuration. The local frontend points to
`http://127.0.0.1:8000`, where no backend was running during verification.

Next, deploy the compatible frontend and confirm backend-only secrets, the
shared package installation, and single-worker configuration according to
`DURABLE_LOGIN.md`. Verify the
enabled flow using a controlled Telegram account before enabling it for users.
Real OTP delivery, Telegram code expiry, two-step password verification, and
remote logout remain unverified against Telegram itself. Keep one backend worker
until the session ownership milestone is implemented.
