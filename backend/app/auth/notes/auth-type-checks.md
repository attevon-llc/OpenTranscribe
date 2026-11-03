# auth — registry-based providers and the `auth_type` CHECK constraints (#866)

Moved intact from `backend/app/auth/CLAUDE.md`. Read before adding an auth provider or migrating `user.auth_type`.

- **Registry-based providers and the `auth_type` CHECK (#866).** `external_sync` writes
  `identity.provider` into the CHECK-constrained `user.auth_type`, so a provider registered under
  a string outside `DB_ADMITTED_AUTH_TYPES` needs the REGISTERING EDITION to widen
  `ck_user_auth_type_valid` and `ck_user_invitation_auth_type_valid` in its own **additive**
  migration. Core cannot do it for them. `register_verifier` logs a WARNING for such a type, and
  `auth_type_admission.assert_auth_type_admitted` (called first in `sync_external_user_to_db`)
  checks the live constraint and raises `ExternalProviderNotAdmittedError` naming the fix instead
  of a bare CheckViolation at commit (only positive answers are cached, so applying the migration
  needs no restart). **No core migration may rewrite unlisted values to `'local'`** (the
  v377/v380 `UPDATE ... WHERE auth_type NOT IN (...)` repair): replayed after an edition widened
  the CHECK, it silently turns delegated-auth accounts into password logins with no password. The
  test file above fails on any post-v380 migration containing that pattern. A catch-all
  `'external'` value (real provider kept in a metadata column) would remove the need for edition
  migrations entirely; it is a schema decision not taken here.
- **`user.auth_type` had TWO CHECK constraints** — `ck_user_auth_type_valid` (v377) and
  a legacy `users_auth_type_check` (v200, re-asserted by v367/v371, still in
  `database/init_db.sql`). Swapping only the first would not have failed the migration;
  it would have failed every OIDC login afterwards with a CheckViolation on JIT
  provisioning. `v380` drops the duplicate — one rule, one owner — and
  `test_v380_migration_consistency.py` pins that exactly one remains.
