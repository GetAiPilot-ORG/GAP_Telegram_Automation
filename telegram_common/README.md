# Telegram session foundation

Milestone 1 supplies an offline encryption helper and an additive credential
table migration. Milestone 2 adds owner-bound durable OTP attempts in `login.py`
and a separate migration, behind a disabled backend feature flag. See
`../GetaipilotBackEnd1/DURABLE_LOGIN.md`. No production bots or credentials have
been migrated. Continuous worker ownership leases remain a later milestone.

From the Telegram repository root:

```sh
python -m pip install -r requirements-session.txt
python -m unittest discover -s telegram_common/tests -v
```

The migration lives in the dashboard repository:
`getaipilot.in/supabase/migrations/20261003000000_telegram_session_credentials.sql`.
Review and apply it to a disposable/local Supabase database before production.
It creates a new backend-only table and leaves existing tables untouched.

## Encryption and usage

AES-256-GCM uses a fresh random 12-byte nonce for each encryption. The envelope
is `tg1.<key_version>.<base64_nonce_ciphertext_tag>`. Its authenticated metadata
includes the credential ID, principal kind, purpose, owner IDs, bot config ID,
format, and key version. Copying ciphertext to another credential identity
fails decryption. State and timestamps are mutable and are not part of that
identity. Identity columns cannot change in the database; allocate a new row
and deliberately re-encrypt if identity linking changes.

An offline dummy example (no database or Telegram requests):

```python
from uuid import uuid4
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from telegram_common import SessionContext, SessionEncryption

cipher = SessionEncryption({"v1": AESGCM.generate_key(bit_length=256)}, "v1")
context = SessionContext(
    credential_id=str(uuid4()),
    principal_kind="user",
    purpose="submanager",
    dashboard_owner_id=str(uuid4()),
)
encrypted = cipher.encrypt("dummy-session", context)
assert cipher.decrypt(encrypted, context) == "dummy-session"
```

For real usage generate the row UUID before encrypting; build context from
verified server-side identities, and persist that exact UUID with ciphertext
and `cipher.active_key_version`. `bot_configuration_id` is a stable identifier,
never a token. The helper encrypts strings; it does not validate Telegram
authorization or turn arbitrary text into a valid session.

## Key configuration

Production backends must receive these variables through deployment secrets:

- `TELEGRAM_SESSION_KEYS`: a JSON object mapping versions to URL-safe base64
  encoded **32-byte random keys**, such as `{"v1":"<base64-key>"}`.
- `TELEGRAM_SESSION_ACTIVE_KEY_VERSION`: the version used for new writes.

Load with `SessionEncryption.from_environment()`. Missing or invalid keys
fail closed; no fallback to plaintext, dummy keys, or generated runtime keys.
Do not put keys in frontend `VITE_*` variables, SQL migrations, source control,
logs, or the credential database. Keep them recoverable in secret management;
losing keys prevents restoring the corresponding Telegram sessions.

For rotation, configure old and new keys, select the new active version, use
`rotate(envelope, context)`, and update ciphertext/key version together using
the expected database `version`. Remove old keys only when no stored rows or
retained backups need them. This method does not persist anything itself.

## Database boundary

The migration revokes browser grants, enables and forces RLS, and deliberately
creates no user policies. Only the backend service role receives CRUD access.
Before later services use it, authenticate and authorize the caller independently
of this privileged client. Return connection status through backend endpoints,
never by granting frontend access to encrypted payloads.

The table has separate unique indexes for dashboard users, Telegram numeric
users, and bots per purpose. Revoked rows retain their identity; new logins
update them deliberately. An update trigger increments `version` and timestamps;
callers must filter writes by their expected version and detect zero-row updates.
This is optimistic versioning, not a lease or distributed-lock implementation.
Revocation clears payload and key version; active/revoking/needs_reauth rows
retain encrypted credentials. Auth user deletion is restricted until backend
credential revocation/cleanup is completed.

## Local SQL checks

`tests/credentials_schema.sql` is a rollback-only assertion suite for a local
Supabase database **after** the migration. It verifies grants/RLS, valid and
invalid principals, uniqueness, identity immutability, revocation, and version
updates. Run it only against a disposable database as its owner:

```sh
psql "$LOCAL_TEST_DATABASE_URL" -v ON_ERROR_STOP=1 -f telegram_common/tests/credentials_schema.sql
```

The SQL fixtures contain format-shaped dummy envelopes, not valid ciphertext.
Cryptographic authenticity is checked by Python; SQL only checks structural
constraints. SQL tests do not establish PostgREST or deployed production behavior.

References: [cryptography AEAD](https://cryptography.io/en/latest/hazmat/primitives/aead/)
and [Supabase RLS](https://supabase.com/docs/guides/database/postgres/row-level-security).
