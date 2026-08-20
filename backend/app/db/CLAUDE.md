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
- `base.py` — `engine`, `SessionLocal`, `Base`, `get_db()`, `build_libpq_options()`.
  `session_utils.py` — `session_scope()` (the contextmanager tasks use),
  `get_refreshed_object`.
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
widened for `proxy`/`scim`), `v383_saml_auth_type` (`auth_type` CHECK
widened for `'saml'` + `user.saml_subject`, mirroring `v380`'s identity-column shape
for a fourth provider), `v384_add_chat_reasoning_content` (nullable
`chat_message.reasoning_content` — persists a provider's separately-streamed
reasoning/"thinking" text for the collapsible reasoning display; single-marker
revision, no CHECK involved), `v389_add_erasure_ledger`
(the GDPR Art. 17 ledger — note its detection arm keys on
`ck_erasure_ledger_counters_numeric` rather than on the table, because a table
without that CHECK can store the personal data the ledger exists not to retain and
therefore *should* re-run the revision), `v390_add_file_facts`,
`v391_add_recorded_date_provenance`, `v392_add_redaction_coverage`,
`v393_add_overlap_timing_columns`, `v394_add_document_tables`,
`v395_add_watch_source_file_document_id`, head currently
`v396_add_document_chunk_redaction_cache`. **Derive the head, never trust this sentence** —
`scripts/release-tests/lib/alembic-head.py` walks the `down_revision` graph.

**Renumbering note 3 (2026-08-19) — a THIRD instance of the same fork shape.** The
document-ingestion lane (`feat/doc-ingestion`, issue #362) originally took `v393`, `v394`, and
`v395` for its three revisions (`add_document_tables`, `add_watch_source_file_document_id`,
`add_document_chunk_redaction_cache` respectively), all chained off
`v392_add_redaction_coverage`. Independently, `master` published
`v393_add_overlap_timing_columns` — also chained off `v392_add_redaction_coverage` — and that
number reached `master` first. Same mechanism as note 2: different filenames, so the merge
of `master` into this branch was clean at the file level and the fork existed only in the
`down_revision` graph. Reconciled by renumbering the document chain up one slot each
(`v393`→`v394`, `v394`→`v395`, `v395`→`v396`), since master's `v393` was already published;
`v393_add_overlap_timing_columns` itself was **not** renamed or edited. The same four places
moved per revision (filename, `revision`/`down_revision`, the detection arm here, `REVISION`
in the consistency test), plus every prose reference to the old numbers across
`app/tasks/document_tasks.py`, `app/services/watch_sources/document_ingest.py`,
`app/models/document.py`, `tests/unit/test_user_deletion_fk_coverage.py`,
`docs-site/docs/developer-guide/documents.md`, `docs/handoff/*.md`, and this file.
⚠️ `v393_add_overlap_timing_columns` has **no detection-arm marker of its own** in
`_detect_schema_version()` — a pre-existing gap on `master`, out of scope for this
renumbering — so the document chain's lowest arm (`v394_add_document_tables`) still falls
back to `v392`'s fingerprint rather than to a `v393`-specific one when the document tables
are absent.

**Renumbering note 2 (2026-08-13) — and it happened EXACTLY the way note 1 warns.**
The `#403` RAG chain added `v389_add_file_facts` while `chore/test-suite-perf-and-quality-overhaul`
added `v389_add_erasure_ledger`, both chained off `v388_add_user_group_organization_id`.
**Git merged both files CLEANLY** — different filenames, no textual overlap — so the
20-file conflict list said nothing, and the fork existed only in the `down_revision`
graph. Reconciled by renumbering the RAG chain (v389→v390, v390→v391, v391→v392) since
the erasure ledger's number was already published on its own branch. Four places move
per revision: the filename, `revision`/`down_revision`, the detection arm here, and
`REVISION` in its consistency test.

⚠️ **A rename sweep does not finish the job.** `test_v390_migration_consistency`
asserted `down_revision == "v388_add_user_group_organization_id"` — a string that stayed
**valid** while ceasing to be **correct**, so no search for the old identifiers found it.
Only running the suite did. Likewise a live database stamped at a renumbered revision
holds a `version_num` that no longer exists: re-stamp to the common ancestor and let the
idempotent chain re-apply, which is what the ladder already computes.

**Renumbering note 1 (2026-08).** This auth-identity chain originally used v375-v381,
branched off `v374_add_tag_user_id` independently of the RAG-chat chain
(`v375_add_chat_tables`/`v376_add_chat_projects`, issue #52/#360) — both sides revised
v374, producing two heads on merge. Reconciled by renumbering the auth chain to
v377-v383 (after the chat chain) rather than renumbering chat's, since the chat chain
had already reached production. Nothing about any revision's DDL changed — only the
seven files' names and their `revision`/`down_revision` strings, and everything that
referenced them (detection arms, consistency tests, this file).

## Never hold a transaction across slow work (issue #440)

Open the session, do the database work, close it — then do the HTTP call / model inference /
file I/O. A session left open while the process does something else holds `ACCESS SHARE` on
every table it touched (which queues any `ALTER TABLE` behind it, including a migration) and
pins the VACUUM horizon for the whole cluster. 35 leaks of this shape were found and fixed;
one had a `celery-cpu-worker` connection `idle in transaction` for **48+ minutes**.

Two things keep it from coming back, and neither replaces the other:

- **`scripts/audit-session-lifetime.py`** — 9 AST detectors, wired into `.pre-commit-config.yaml`
  (self-test first) and its own phase in `run-integration-tests.sh`. Allowlist entries need a
  written reason and a **stale entry fails the run**, so an exemption cannot outlive its subject.
  ⚠️ **A session opened any way `_SESSION_OPENERS` does not name is INVISIBLE to all nine
  detectors** — not under-reported, unreachable. When the chat turn was phased (`e486f948`)
  `answer_aggregation` began taking a `session_factory` instead of a `Session`, and neither
  `with session_factory() as db:` nor its `_short_session` wrapper was recognised, so that
  subsystem scored 0 findings because nothing could fire in it. Both names are in the set now.
  Adding a new way to open a session means adding its name here **and** a must-fire plus a
  must-stay-clean self-test case — then verifying by MUTATION: delete the name and the
  self-test must break. Reading the code is not verification; a name that matches nothing
  looks exactly like a clean subsystem.
- **`DB_IDLE_IN_TRANSACTION_TIMEOUT_MS`** (default 5 min, 0 disables) — the server-side backstop.
  `base.py:build_libpq_options` puts `idle_in_transaction_session_timeout` in the shared engine's
  `connect_args`, so Postgres terminates a backend holding an open transaction and running no
  query. It **cannot interrupt a slow query, only an idle one**, which is exactly why it is safe
  to ship on: legitimate long statements are untouched. The migration engines in `migrations.py`
  are built separately and are outside it by construction, so a long `ALTER TABLE` and the
  advisory-lock holder are never at risk.

`tests/unit/test_idle_in_transaction_backstop.py` proves the GUC really terminates an idle
transaction **against a live server, with its own control** (same idle duration, no GUC → the
connection survives) plus a second control that a `pg_sleep` inside a statement is *not* killed.
Configuration-only assertions would have passed against a backstop that did nothing.

## Gotchas

- **The `pg_advisory_lock(42)` guard covers the migration — keep it that way.** Fixed in
  issue #284 A1.4: `run_migrations()` takes the lock on a **dedicated `lock_engine` /
  `lock_conn`** held open across `command.upgrade()`, and the `finally` unlocks on that same
  session (`migrations.py:1058-1074`, `1128-1134`). The bug it replaced is the reason the
  shape matters: `pg_advisory_lock` is **session-scoped**, so taking it inside a
  `with engine.connect()` block — or on a connection returned to a pool that
  `engine.dispose()` then closes — drops the lock *before* the upgrade runs, and unlocking
  from a fresh connection is a no-op because a session cannot release a lock it never held.
  Concurrent replicas then race Alembic. Pinned by
  `tests/unit/test_celery_reliability.py:109` (lock acquired before the upgrade, on
  `lock_conn`, with the `engine.dispose()` ordered so it cannot touch the lock engine) and
  `:124` (no second engine for the unlock). If you refactor the runner, do not move the
  acquire into a context manager and do not introduce an `unlock_engine`.
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
