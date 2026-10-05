# Post-merge deployment review — 2026-10-03

Reviewed merge `1941856` with parents `11a6328` and `68b6626`.
All six incoming commits are included. The Submanager backend, shared package,
and package metadata are byte-for-byte unchanged relative to the local parent.
No merge regression was found in those components.

## Validation

- 32 shared encryption/durable login tests and 3 legacy login tests passed
  after the merge, including the disposable PostgreSQL tests.
- Frontend production build passed after the merge.
- All seven isolated Chromium login scenarios passed again after the merge.
- Every incoming Python file passed syntax parsing.
- Incoming changes include whitespace warnings; they are not syntax failures.

## Deployment recommendation

Deploy Submanager first with `TELEGRAM_DURABLE_LOGIN=false`. Configure and
verify the new flow with a controlled account before enabling it for users.
The new login code was preserved by the merge, but deployment still requires:

1. Commit the dashboard's two login component changes. They remain local edits
   in the separate dashboard repository. Its migration files and browser tests
   are also untracked. Avoid including unrelated dashboard edits accidentally.
2. Install the shared package into the backend's actual virtual environment.
   The existing `vps_setup.sh` installs only backend requirements, which do not
   install `telegram_common`. Use the root package or its built wheel as
   described in `DURABLE_LOGIN.md`.
3. Configure backend-only service-role/encryption secrets; retain encryption
   keys across deployments. Use one worker and the production frontend API URL.
4. Verify real OTP, password, refresh/restart, and logout behavior before
   enabling durable login for users. Main database migrations are already applied.

## Other incoming bots

This review does not approve restarting all bots together. Incoming commits
change AutoForward, Join, Broadcast, and add a private broadcast service.
Only syntax parsing was performed across all of those files; automated login
tests cover Submanager.

The incoming Broadcast code needs follow-up before rollout:

- `channel_selection_handler` accepts an unknown selected channel and looks it
  up globally when absent from `available_channels`, without checking ownership.
  Confirmation does not revalidate channel ownership before sending. Reject
  unknown selections and verify ownership at send time.
- The audience query combines users from all matching links, then loops over
  every bot token and sends to the whole combined audience. A subscriber who
  has started multiple matching bots can receive duplicate broadcasts. Scope
  recipients to their bot and define deduplication behavior before rollout.

The merge also changes a tracked AutoForward SQLite session binary. Preserve
the VPS's runtime session files during deployment; do not copy that repository
session over a live instance. Legacy tracked session files still need the
separate planned migration and removal work.
