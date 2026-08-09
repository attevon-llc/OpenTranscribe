# app/db — session factory + the startup migration runner

## Purpose

**Alembic is the sole schema authority.** `migrations.py:run_migrations()` executes on
backend startup (`app/main.py` lifespan) and handles three cases: empty DB → `upgrade head`;
existing *untracked* DB → detect version, `stamp`, then upgrade; already tracked → apply
pending. `alembic upgrade head` by hand is **production-only**.
`database/init_db.sql` is **legacy reference only — not used for schema.**

## Key files

- `migrations.py` — the runner plus `_detect_schema_version()`, a ~400-line ladder of
  `information_schema` probes that maps a schema fingerprint to a revision to stamp.
- `base.py` — `engine`, `SessionLocal`, `Base`, `get_db()`. `session_utils.py` —
  `session_scope()` (the contextmanager tasks use), `get_refreshed_object`.
- `README.md` — longer prose on the layering.

## Adding a schema change

1. New revision in `backend/alembic/versions/` — **idempotent SQL** (`IF NOT EXISTS`,
   `DO $$ … END $$` guards). Every existing revision follows this.
2. Update the SQLAlchemy model in `app/models/`.
3. Update the Pydantic schema in `app/schemas/` if it's exposed via API.
4. **Add a detection guard for the new version in `_detect_schema_version()`** — a marker
   column/table/constraint unique to your revision, inserted at the TOP of the
   newest-first return ladder. Skipping this silently mis-stamps untracked DBs.
5. Test both paths: `./opentr.sh reset dev` (full chain from scratch) **and** a
   rebuild-and-restart (migration applied on startup over an existing DB).

Version context: `v364_add_content_redaction`, `v365_add_prompt_shared_by`,
`v366_add_watch_sources`, `v367_add_cloud_seams`, `v369_superuser_role_invariant`
(the `role`/`is_superuser` CHECK), `v373_add_cluster_organization_id`,
`v374_add_tag_user_id` (per-user tag ownership — the one revision so far that both
backfills *and* splits rows, and that drops a pre-existing UNIQUE constraint),
`v375_add_chat_tables`, `v376_add_chat_projects` (chat projects; note its FK is ON
DELETE **SET NULL**, so deleting a project leaves its conversations ungrouped rather
than destroying them), `v377_harden_user_auth_invariants` (auth-type CHECK +
invitations), `v378_idp_group_mapping`, `v379_rename_keycloak_config_to_oidc` (a
**data-only** revision — no DDL, so its detection arm keys on the *absence* of the
retired config-key prefix), `v380_oidc_identity_columns` (`user.oidc_subject`,
`user.oidc_refresh_token`, `refresh_token.oidc_id_token`, the `auth_type` value swap,
and the removal of a duplicate CHECK that would otherwise have refused every OIDC
login), `v381_approval_state` (`user.approval_status` NOT NULL DEFAULT
`'approved'` + `approved_at`/`approved_by` + `ck_user_approval_status_valid`; its
detection arm requires **both** the column and the CHECK, because the enforcement
helpers read the column fail-safe and it is the constraint that keeps that sound),
`v382_scim_tokens` (`scim_token` table + `group_mapping`'s `source`/membership CHECKs
widened for `proxy`/`scim`), head currently `v383_saml_auth_type` (`auth_type` CHECK
widened for `'saml'` + `user.saml_subject`, mirroring `v380`'s identity-column shape
for a fourth provider).

**Renumbering note (2026-08).** This auth-identity chain originally used v375-v381,
branched off `v374_add_tag_user_id` independently of the RAG-chat chain
(`v375_add_chat_tables`/`v376_add_chat_projects`, issue #52/#360) — both sides revised
v374, producing two heads on merge. Reconciled by renumbering the auth chain to
v377-v383 (after the chat chain) rather than renumbering chat's, since the chat chain
had already reached production. Nothing about any revision's DDL changed — only the
seven files' names and their `revision`/`down_revision` strings, and everything that
referenced them (detection arms, consistency tests, this file).

## Gotchas

- **The `pg_advisory_lock(42)` guard is present but does not actually cover the migration.**
  It is taken inside a `with engine.connect()` block that then exits, and the same engine is
  `dispose()`d at the top of the run — closing the pooled session **releases the
  session-scoped lock before `command.upgrade()` runs**. The `finally` block unlocks on a
  *different* engine/session that never held it. Concurrent backend replicas starting
  together can still race. Treat this as unfixed; if you touch it, hold one dedicated
  connection open for the whole run.
- **`_detect_schema_version()` legitimately names the pre-`v380` column spellings.**
  Those probes describe the schema *as it was* at v031 and v170; they are the only
  possible fingerprints for those revisions. This file and `core/legacy_auth_env.py`
  are the two modules the OIDC naming invariant
  (`tests/unit/test_oidc_naming_invariant.py`) exempts on that basis.
- **A superseded revision's detection test compares chain position, not identity.**
  `assert _detect_schema_version(...) == REVISION` is only true while that revision is
  head, so each new revision silently turned its predecessor's test red (three were
  already failing that way). `tests/unit/_migration_detection.py:
  assert_detected_at_or_after` asserts what the test is actually for: the ladder must
  never stamp *lower* than the revision whose markers the schema carries.
- Only the **backend** runs migrations — Celery workers do not, and there is **no
  `RUN_MIGRATIONS` env gate**. A migration failure calls `SystemExit(1)`; the container
  aborts rather than serving a half-migrated schema.
- `_repair_skipped_v230()` runs after every successful migration — a permanent post-hook
  that re-applies v230's idempotent DDL for DBs upgraded through the pre-linearisation
  v250→v270 branch. Don't remove it.
- `Base` deliberately has **no `naming_convention`** — adding one would rename existing
  constraints, i.e. an unintended schema change.
- The runner widens `alembic_version.version_num` to `VARCHAR(128)` on every start because
  this project's revision IDs are long descriptive names, not hashes.
- Wrong-DB symptoms ("relation does not exist") almost always mean the stack was started
  with bare `docker compose` instead of `./opentr.sh start dev`. Relaunch before debugging.
