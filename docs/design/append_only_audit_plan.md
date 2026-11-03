# Append-only audit records: stop the application role from rewriting or deleting audit rows. Design

Status: **IMPLEMENTED** on `feat/append-only-audit` (issue #1352; migration v453, chained off v451 until re-chained behind v452). Implementation notes are at the end of this file. Written 2026-11-01 against `master` @ `e6843db20`
(Alembic head `v449_add_speaker_identity_source`). Target release: **v0.7.0**. Line numbers drift:
search for the quoted symbol, not the number.

Companion: `docs/design/erasure_receipt_plan.md`. The two plans are independent, but the receipt
table it adds should be registered here as insert-only (§4.2, WP-A2).

## 0. Summary

Today one Postgres role does everything. The `postgres` image creates `POSTGRES_USER` (default
`postgres`) as a **superuser**. The backend, every Celery worker and Alembic all connect as that
role, and it owns every table. So no privilege check and no trigger can stop the application from
changing an audit row: a superuser or owner can `DISABLE TRIGGER`, `SET session_replication_role
= replica`, or drop the trigger.

Recommendation: **ship both layers, each for a different job.**

1. **Layer 1, always on (migration):** an immutability trigger on the protected tables. It rejects
   `DELETE`, `TRUNCATE` and any `UPDATE` outside a per-table allow-list of lifecycle columns. Two
   exceptions: an FK `ON DELETE SET NULL` that only nulls a declared actor column, and an explicit
   maintenance switch that only the table owner can use. In single-role mode this is an **accident
   guard** against ORM bugs and stray scripts, not a security boundary.
2. **Layer 2, opt-in (the security boundary):** a two-role split. `POSTGRES_USER` stays the owner
   and migration role. A new `POSTGRES_APP_USER` (`NOSUPERUSER NOCREATEROLE NOCREATEDB
   NOBYPASSRLS`, owns nothing) is what the backend and workers connect as at runtime. On protected
   tables it gets `SELECT, INSERT` plus column-level `UPDATE` on lifecycle columns only. It gets no
   `DELETE`, `TRUNCATE`, `TRIGGER` or `REFERENCES`. Grants are reconciled on every startup by one
   function, from one registry.

**Default: off on every install in v0.7.0** (decision D1). An operator turns it on by setting two
`.env` values and restarting. A super_admin status endpoint reports the **measured** state, from
`has_table_privilege`, not from the configuration.

## 1. Verified current state

### 1.1 Who runs migrations, and as which role

- `backend/alembic/env.py` builds its URL from `POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_HOST`/...
  and calls `config.set_main_option("sqlalchemy.url", ...)` **at import**. That overwrites the URL
  `app/db/migrations.py:get_alembic_config()` set from `settings.DATABASE_URL`. **Finding F1:**
  Alembic ignores `DATABASE_URL` and `POSTGRES_SSLMODE`, while the advisory lock (`lock_engine`),
  the detection ladder and the probe engines in `migrations.py` use `settings.DATABASE_URL`. A
  deployment that sets `DATABASE_URL` to a different server migrates one database and locks
  another. This plan fixes it (WP-A1), because the split needs one migration URL anyway.
- `app/core/config.py` assembles `DATABASE_URL` from `POSTGRES_*` when it is blank (around
  `if not self.DATABASE_URL:`).
- `app/db/base.py` builds the runtime `engine` from `settings.DATABASE_URL`. The backend runs
  migrations on startup (`main.py`, `RUN_MIGRATIONS_ON_STARTUP`). Celery workers never migrate.
- `docker-compose.yml`: `postgres:17.5-alpine`, and every service loads `env_file: .env`, so
  **every container holds the owner password** today.
- No `CREATE ROLE`, `GRANT` or `REVOKE` exists anywhere in `backend/alembic`, `backend/app`,
  `database/` or `scripts/`.
- No runtime DDL exists outside `app/db` and Alembic (searched for `ALTER TABLE`, `CREATE INDEX`,
  `CREATE TABLE`, `TRUNCATE`, `VACUUM`). The app role therefore needs DML only.
- The backup task runs `pg_dump --no-owner --no-acl` from a worker (`services/backup_service.py:
  run_pg_dump`). Restores run `psql`/`pg_restore` as `POSTGRES_USER` (`scripts/common.sh`). A
  restored database therefore has **no grants** for the app role until something re-applies them.

### 1.2 Where audit data actually lives (derived from code)

| Store | What | Mutated today? | In scope |
|---|---|---|---|
| OpenSearch `audit-logs-YYYY.MM` (`auth/audit.py:_audit_index_name`) | **The main audit trail**: auth, admin, content, privacy and erasure events | Never updated or deleted by app code | **No (D5)**. The DB split cannot protect it. The OpenSearch security plugin is off by default (`DISABLE_SECURITY_PLUGIN=true`), so any client on the network can delete an index |
| Audit fallback file `AUDIT_LOG_FALLBACK_PATH` (`/var/log/opentranscribe/audit-fallback.jsonl`, volume `audit_fallback_logs`) | Events written when OpenSearch is down | Append only | **No.** Owned by the app UID, so it can be truncated. The stdout stream (the `audit` logger's `StreamHandler`, captured by the Docker log driver or a SIEM) is the copy outside the app's reach. Documented, not changed |
| Erasure journal `DATA_DIR/gdpr/erasure-journal.jsonl` or objects | Ledger mirror that survives a restore | Append only | No (same reasoning). Documented |
| `auth_config_audit` | Auth configuration change log | Insert only. **v379 rewrote rows** (data migration) | **Yes, insert-only** |
| `support_access_use` | Per-request record of support-access use (#1122 §5.6, "append-only" by design) | Insert only. `grant_id` FK is `RESTRICT` | **Yes, insert-only** |
| `support_access_grant` | Grant lifecycle (decision, revoke) | UPDATE of lifecycle columns | **Yes, lifecycle** |
| `erasure_ledger` | Art. 17/30 ledger | UPDATE of status/attempt/counter columns (`erasure_ledger_service.record_outcome`, `record_resurrection`) | **Yes, lifecycle** |
| `takedown_notice`, `takedown_notice_file` | Legal notice record (#1254) | UPDATE of outcome columns (PATCH) | **Yes, lifecycle** |
| `takedown_notification` | Delivery log | UPDATE of `status`/`attempted_at`/`recipient_user_id` on retry (`takedown_notification_service`, around `row.status = status.value`) | **Yes, lifecycle** |
| `content_hold`, `content_hold_file` | Org holds; released, never deleted | UPDATE on release. `organization_id` **CASCADE** | **No, exempt with reason**: tenant data that goes with the tenant on `erase_organization`, and placement/release is already in the OpenSearch stream. Protecting it would make org erasure fail whenever a released hold exists |
| `legal_acceptance` | Acceptance evidence | Since v451: `user_id` `ON DELETE SET NULL`; the DB trigger `trg_legal_acceptance_guard` pseudonymises on that update and refuses every other rewrite, and refuses DELETE except the retention sweep (`opentranscribe.legal_retention_sweep` GUC, `retain_until` passed) | **Yes, lifecycle**. Lifecycle columns: `user_id`→NULL, `ip_address`→NULL, `user_agent`→NULL, `subject_erased_at`, `retain_until`, `organization_id`→NULL. Retention delete: `legal.acceptance_retention_sweep` |
| `usage_event` | Usage spine; cloud-edition seam table | Insert (idempotent) | **Exempt by default (D4)**. A downstream edition may own its lifecycle. It can opt in through the registry hook (§4.4) |
| future identity history (#1336) | Append-only speaker identity history | n/a | Register it when it lands |

### 1.3 Retention jobs that delete audit rows

**None exist.** `AUDIT_LOG_RETENTION_DAYS` (`core/config.py`, `.env.example`, default 365) is read
by nothing. The only references are config, `test_env_example_coverage.py` and
`test_fedramp_controls.py`, which asserts `hasattr` and `>= 90`: a test that cannot fail on
behaviour. `docs-site/docs/operations/security-hardening.md` ("Log Retention: Configure log
retention...") describes a control that does not exist. **Finding F2**, resolved by D3.

### 1.4 How FK actions interact with the two mechanisms

- Postgres runs referential actions (`ON DELETE SET NULL`/`CASCADE`) **as the owner of the
  referencing table** (`ri_triggers.c`, `SetUserIdAndSecContext(relowner)`). Revoking `UPDATE`
  from the app role therefore does **not** break erasure's actor-FK nulling. That is the
  behaviour we want.
- Ordinary row triggers **do** fire on those RI-issued statements. A blanket "no UPDATE" trigger
  would make `DELETE FROM "user"` fail for any admin who ever changed auth config, i.e. it would
  break GDPR erasure. The trigger must therefore allow the "only declared nullable FK columns go
  to NULL" shape (§4.3).
- `takedown_notification.link_id` → `takedown_notice_file.notice_id` → `takedown_notice` are
  `CASCADE`. No code deletes a notice (verified: no delete site), so with the trigger a cascaded
  delete raises. That is intended: deleting the record of a legal notice must be refused.

## 2. Alternatives considered

| Option | Stops a compromised app connection | Stops ORM/script accidents | Works in single-role installs | Cost | Verdict |
|---|---|---|---|---|---|
| A. Trigger only | **No** (owner or superuser bypasses) | Yes | Yes | Low | Not sufficient alone |
| B. Grant split only | Yes, for a role without owner credentials | Only in split mode | No (no effect) | Medium (credential plumbing, reconcile on restore) | The real boundary |
| C. A + B (recommended) | Yes in split mode | Yes, always | Yes (accident guard) | Medium | **Chosen** |
| D. Hash chain (each row stores `sha256(prev_hash ‖ row)`) | Detects but does not prevent; a tail truncation is invisible without an external anchor | n/a | Yes | High: serialized inserts, a verifier, an external anchor | Rejected for v0.7.0. Revisit with an off-host anchor |
| E. Row-level security policies | Yes, for non-owners, but the owner bypasses unless `FORCE` is set, and RLS cannot express "column X may change only to NULL" | Partly | No | Medium | Rejected: grants plus a trigger say the same thing more plainly |
| F. Ship audit rows to WORM storage | Strongest | n/a | Needs infrastructure | High | Out of scope. The OpenSearch stream and stdout already leave the host |

Why C over B alone: B protects nothing until an operator opts in, and an existing install runs
single-role for some time after v0.7.0. Layer A makes the rule visible and testable from day one
in every install, and gives one clear SQLSTATE (`42501 insufficient_privilege`) whichever layer
refused.

**Honest limit of B in the default layout:** the backend runs migrations, so it must hold the
owner credentials too. Code execution inside the backend process still reaches them. B protects
against SQL injection, ORM bugs and every **worker**, which are the processes that run ffmpeg,
yt-dlp and model code on untrusted media and need only the app credentials. Full separation needs
`RUN_MIGRATIONS_ON_STARTUP=false` plus a migrate job, so that the backend holds app credentials
only. Document both postures.

## 3. Decisions for David

- **D1. Default.** (a) off everywhere in v0.7.0, opt-in (recommended). (b) on for fresh installs
  (`opentranscribe.sh setup` generates `POSTGRES_APP_PASSWORD`), off for upgrades. Recommendation:
  (a) for v0.7.0, then (b) in v0.8.0 after one release of soak.
- **D2. Fail mode when requested but not effective** (for example the app role turns out to be a
  superuser, or owns a protected table). (a) refuse to start (recommended: the operator explicitly
  asked). (b) start and report `enforced: false`.
- **D3. `AUDIT_LOG_RETENTION_DAYS` (dead knob).** (a) make it real for the protected Postgres
  tables through a `SECURITY DEFINER` prune function with a hard floor (rows younger than
  `max(floor, setting)` can never be deleted by the app role); default `0` = keep forever, so no
  behaviour change on upgrade. (b) delete the setting and correct the docs. Recommendation: (a)
  with floor 365 days (D3a: confirm the floor value). OpenSearch index retention stays out of scope
  either way; the docs say so.
- **D4. `usage_event`.** Exempt (recommended; it is a seam table) or protect it as insert-only.
- **D5. OpenSearch audit stream.** Confirm it is out of scope for v0.7.0. Recommendation: file a
  follow-up issue for OpenSearch security-plugin roles (an index-only writer with no delete) in
  the prod/PKI overlay.
- **D6. Credential hygiene overlay** (§4.6: blank the owner password on every service except
  backend and postgres). In v0.7.0 (recommended) or later.

## 4. Design

### 4.1 Settings (WP-A1)

`app/core/config.py`:

- `POSTGRES_APP_USER: str = ""`, `POSTGRES_APP_PASSWORD: str = ""`.
- `MIGRATION_DATABASE_URL: str = ""`. Resolution, in the same validator that builds
  `DATABASE_URL`:
  - `MIGRATION_DATABASE_URL` blank → built from `POSTGRES_USER`/`POSTGRES_PASSWORD`, the owner.
    If `DATABASE_URL` was explicit and the split is off, it equals `DATABASE_URL`.
  - `POSTGRES_APP_USER` set and `DATABASE_URL` blank → `DATABASE_URL` is built from the app
    credentials.
  - `POSTGRES_APP_USER` set and `DATABASE_URL` explicit → configuration error at boot ("set
    MIGRATION_DATABASE_URL and point DATABASE_URL at the app role, or unset POSTGRES_APP_USER").
    No guessing.
- `settings.db_role_split_requested` (bool) = `POSTGRES_APP_USER != ""` or
  `MIGRATION_DATABASE_URL != DATABASE_URL`.
- `.env.example`: both new variables in the compliance block, commented, with one line on what
  they do. Keep `test_env_example_coverage.py` green.

Callers that must use `MIGRATION_DATABASE_URL`: `backend/alembic/env.py` (replace the hand-built
URL; this fixes F1; keep `load_dotenv()` and read the URL through `app.core.config.settings`),
every `create_engine(settings.DATABASE_URL)` in `app/db/migrations.py` (around lines 35, 1552,
1758, 1805, 1819, including `lock_engine`), and `backend/tests/conftest.py`
(`SQLALCHEMY_TEST_DATABASE_URL`). The suite performs DDL and seeds protected rows, so it runs as
the owner and enters the app role explicitly where a test needs it. Runtime `app/db/base.py` stays
on `DATABASE_URL`.

### 4.2 The registry (one source of truth)

New `backend/app/db/audit_protection.py` (pure data plus SQL renderers, about 150 lines):

```python
@dataclass(frozen=True)
class ProtectedTable:
    name: str
    mutable_columns: tuple[str, ...] = ()        # () = insert-only
    nullable_on_cascade: tuple[str, ...] = ()    # FK columns an ON DELETE SET NULL may blank
    reason: str = ""                             # why it is protected (docs + test output)

PROTECTED_TABLES: tuple[ProtectedTable, ...] = (...)
EXEMPT_TABLES: dict[str, str] = {"content_hold": "...", "content_hold_file": "...",
                                 "usage_event": "..."}
# legal_acceptance is PROTECTED (v451 already ships its own guard trigger; register it here with
# nullable_on_cascade=("user_id", "organization_id") and the lifecycle columns above).
def register_protected_table(t: ProtectedTable) -> None  # edition hook, before startup reconcile
```

Initial contents. These are a starting point: **derive the mutable columns by running the existing
service tests with the trigger on** (WP-A2 step 4). Never widen a set without a failing test that
needs it.

| Table | mutable_columns | nullable_on_cascade |
|---|---|---|
| `auth_config_audit` | () | `changed_by` |
| `support_access_use` | () | () |
| `support_access_grant` | `decided_by_user_id, decided_at, decision, starts_at, expires_at, revoked_at, revoked_by_user_id` (confirm against `support_access_lifecycle.py`) | `organization_id, subject_user_id, grantee_user_id, decided_by_user_id, revoked_by_user_id` |
| `erasure_ledger` | `status, completed_at, last_attempt_at, attempts, deferred_reason, legal_holds_outstanding, error_count, counters, resurrections_detected, last_resurrection_at` | `actor_user_id` |
| `takedown_notice` | `updated_at` (confirm) | `handled_by_user_id` |
| `takedown_notice_file` | `status, counter_notice_received_at, outcome_at, outcome_note, legal_hold_placed` (confirm) | `file_id, organization_id` |
| `takedown_notification` | `status, attempted_at, recipient_user_id` | `recipient_user_id` |
| `erasure_receipt` (if the receipt plan has landed) | () | () |

### 4.3 Layer 1: the immutability trigger (WP-A2, migration)

New revision. **Do not hardcode the number**: run `python3 scripts/release-tests/lib/alembic-head.py
backend`, name it `v<next>_audit_row_immutability`, set `down_revision` to the printed head.
Idempotent SQL only (`CREATE OR REPLACE FUNCTION`, `DROP TRIGGER IF EXISTS` then `CREATE TRIGGER`).

One generic function, `audit_row_guard()`, `LANGUAGE plpgsql`, **not** `SECURITY DEFINER`:

- `TG_OP = 'DELETE'` or `'TRUNCATE'` → allow only when `_maintenance_allowed()`, else
  `RAISE EXCEPTION ... USING ERRCODE = 'insufficient_privilege'`, with a message naming the table
  and `docs-site/docs/operations/audit-integrity.md`.
- `TG_OP = 'UPDATE'`: let `mutable = TG_ARGV[0]::text[]` and `nulls = TG_ARGV[1]::text[]`. Allow
  when `(to_jsonb(NEW) - mutable - nulls) = (to_jsonb(OLD) - mutable - nulls)` and every column
  in `nulls` either is unchanged or became NULL. Otherwise allow only when
  `_maintenance_allowed()`.
- `_maintenance_allowed()` := `current_setting('opentranscribe.audit_maintenance', true) = 'on'`
  **and** (`pg_has_role(current_user, (SELECT relowner FROM pg_class WHERE oid = TG_RELID),
  'MEMBER')` or the role is a superuser). In split mode the app role is neither, so setting the
  GUC buys it nothing (test A5).

Triggers per protected table: `BEFORE UPDATE OR DELETE ... FOR EACH ROW EXECUTE FUNCTION
audit_row_guard('{...}','{...}')` and `BEFORE TRUNCATE ... FOR EACH STATEMENT`. Render the trigger
arguments from the registry inside the migration. Copy the values into the revision as a
module-level constant, because a migration must not import a module that will change later, and
let the consistency test assert that the constant equals the registry at this revision.

Migrations must be able to repair audit data (v379 did): `migrations.py` connects its migration
engine with `options=-c opentranscribe.audit_maintenance=on` (append to
`build_libpq_options`'s output for **migration engines only**, never `base.py`). Row triggers fire
per affected row, so an idempotent re-run of v379 against an already-renamed database touches no
rows and does not need the GUC.

Detection arm: add `has_audit_row_guard` (exists `pg_trigger` named `trg_audit_guard_erasure_ledger`,
created last) at the TOP of `_detect_schema_version()`, requiring v449's marker too. Downgrade:
drop the triggers and the function (no data loss).

Also add the new revision to `backend/app/db/CLAUDE.md`'s version list.

### 4.4 Layer 2: role reconcile (WP-A3)

New `backend/app/db/roles.py`:

- `reconcile_app_role(conn, role: str, password: str | None, *, provision: bool = True) -> RoleReport`.
  Runs as the owner, in one transaction:
  1. If `provision`: `CREATE ROLE` when missing (`DO $$ ... IF NOT EXISTS (SELECT 1 FROM pg_roles
     ...)`), then `ALTER ROLE <ident> WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS
     NOREPLICATION PASSWORD <verifier>`. The verifier comes from
     `psycopg2.extensions.encrypt_password(password, role, conn, "scram-sha-256")`, so the
     plaintext never reaches a server log even with `log_statement=ddl`. Use `psycopg2.sql.Identifier`
     for the role name.
  2. `GRANT CONNECT ON DATABASE`, `GRANT USAGE ON SCHEMA public`.
  3. `GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public`,
     `GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public`, plus `ALTER DEFAULT PRIVILEGES FOR
     ROLE <owner> IN SCHEMA public` for both, so tables an edition's migration adds get the same.
  4. For each `PROTECTED_TABLES` entry: `REVOKE UPDATE, DELETE, TRUNCATE, TRIGGER, REFERENCES ON
     <t> FROM <role>`, then `GRANT UPDATE (<mutable_columns>) ON <t>` when the list is non-empty.
  5. Assert, and return in `RoleReport`: `rolsuper = false`, the role owns no table in `public`,
     and `has_table_privilege(role, t, 'UPDATE'/'DELETE'/'TRUNCATE')` is false for every protected
     table. Column privileges are checked with `has_column_privilege`.
- Called at the end of `run_migrations()`, after `_repair_skipped_v230()`, only when
  `settings.db_role_split_requested`. Reconcile on every start is what repairs a `--no-acl`
  restore (§1.1). Also expose it as `python -m app.db.roles reconcile` for
  `RUN_MIGRATIONS_ON_STARTUP=false` deployments; their migrate job runs it after `upgrade`.
- `POSTGRES_APP_ROLE_PROVISION=false` (managed databases where the role is created by IaC) skips
  step 1 but still runs steps 2 to 5.
- Never `DROP ROLE`. Turning the split off leaves a harmless unused role behind.

Boot check (`main.py` lifespan, backend **and** the Celery `worker_process_init` hook): when the
split is requested, run `measure_runtime_role(engine)` over the runtime engine: `current_user`,
`rolsuper`, ownership, and the privilege probes. On failure apply D2. The worker check matters
because a worker started with an old `.env` would otherwise silently keep running as the owner.

### 4.5 Status endpoint and UI (WP-A4)

- `GET /api/admin/security/audit-integrity` (super_admin, `get_current_active_superuser`): returns
  `{split_requested, enforced, runtime_role_is_superuser, runtime_role_owns_tables, tables: [{name,
  insert, update_columns, delete, truncate, guard_trigger_present}]}`. Every field is measured.
  Role names are not returned (fixed booleans only). Add the route to
  `docs/security/platform-access-matrix.tsv` (`scripts/generate-platform-access-matrix.py --write`)
  and check `test_route_privilege_tiers.py`.
- UI: one read-only status row in `frontend/src/components/settings/AuditContentAccessToggle.svelte`'s
  panel (or a sibling `AuditIntegrityStatus.svelte` in the same settings section, if that file's
  concern would otherwise grow): "Audit record protection: Enforced / Not enforced (accident guard
  only)" plus a docs link. API client method `AdminApi.getAuditIntegrity` in
  `frontend/src/lib/api/admin.ts`. The name must not match `/gdpr|erase/i`
  (`admin.lockedAccountsQuarantine.test.ts` guard).
- **i18n:** 4 keys (`settings.audit.integrity.title`, `.enforced`, `.notEnforced`, `.docsLink`),
  translated into all **12** locales (ar de en es fr it ja ko nl pt ru zh). Check `ar` renders RTL.
  Run `cd frontend && npm run check:i18n`; locale-only commits do not fire the hook. Verify in a
  browser in light and dark mode.

### 4.6 Compose and scripts (WP-A5, D6)

- New `docker-compose.db-roles.yml`. Every service gets `POSTGRES_APP_USER:
  ${POSTGRES_APP_USER:-}` / `POSTGRES_APP_PASSWORD: ${POSTGRES_APP_PASSWORD:-}`, and every service
  except `backend` and `postgres` gets `POSTGRES_PASSWORD: ""` and `MIGRATION_DATABASE_URL: ""`.
  Workers then cannot reach owner credentials.
- `opentr.sh` (dev/fresh) and `opentranscribe.sh` `get_compose_files()` auto-load it when
  `POSTGRES_APP_USER` is non-empty in `.env` or in the shell, with a `🔒 DB role split overlay
  AUTO-LOADED` banner like the NAS one. Verify with `./opentr.sh start dev --dry-run`.
- `--fresh` isolation: the overlay declares no `container_name` or ports. Add it to the written
  exemption table of `backend/tests/unit/test_opentr_fresh_aux_isolation.py` only if that test
  enumerates auto-loaded overlays. Read the test first.
- `scripts/common.sh:restore_database`: after a successful restore, print "the backend
  re-applies app-role grants on its next start". No code change is needed, because reconcile runs
  in `run_migrations()`.

### 4.7 Retention (WP-A6, only if D3 = a)

- In the WP-A2 migration (or its own revision): `CREATE OR REPLACE FUNCTION
  audit_prune(tbl regclass, keep_days int) RETURNS bigint SECURITY DEFINER SET search_path =
  pg_catalog, public`. It refuses any table not in a hardcoded protected-table list, computes
  `effective = greatest(keep_days, <FLOOR>)`, deletes rows older than that by the table's time
  column (a hardcoded map: `auth_config_audit.created_at`, `support_access_use.occurred_at`, ...),
  and sets `opentranscribe.audit_maintenance` locally. Owned by the owner. `REVOKE ALL ... FROM
  PUBLIC`, then `GRANT EXECUTE` to the app role in reconcile.
- `erasure_ledger`, `erasure_receipt` and the takedown tables are **never** prunable: Art. 30
  evidence and legal notices. Leave them out of the function's list.
- A beat task `audit.retention_sweep` (utility queue, daily) calls the function per table when
  `AUDIT_LOG_RETENTION_DAYS > 0`. Change the default to `0` and update `.env.example` and the docs.
  Make `test_fedramp_controls.py` assert behaviour (the floor holds) instead of `hasattr`.

## 5. Sequencing

1. WP-A1 settings and F1 (env.py/runner use `MIGRATION_DATABASE_URL`; conftest on the owner URL).
   No behaviour change with the split off.
2. WP-A2 registry plus trigger migration plus consistency test. Run the full backend suite
   (`./scripts/run-integration-tests.sh`) with the trigger on: every failure is either a real
   mutation (fix the code) or a lifecycle column to add with a reason.
3. WP-A3 reconcile plus boot check.
4. WP-A4 status endpoint plus UI plus i18n.
5. WP-A5 compose overlay plus script detection.
6. WP-A6 retention (if chosen).
7. Docs and CHANGELOG (§9).

## 6. Tests (each must be seen red first; use a `git archive HEAD` tree under `mktemp -d -t ot-redcheck.XXXXXX`)

All DB tests run against the live dev Postgres, as the owner, inside one transaction that is
rolled back. `CREATE ROLE` is transactional, so nothing persists. Test files go in
`backend/tests/unit/` unless noted.

| # | Test (`file::name`) | Proves | Red against |
|---|---|---|---|
| A1 | `test_audit_protection_registry.py::test_every_protected_table_exists_and_every_audit_shaped_table_is_classified` | Registry matches live schema. Any table named `%audit%`, `%_use`, `%ledger%`, `%notification%`, `%receipt%` is protected or exempt with a reason. The deriving query must match at least 5 tables (guard against matching nothing) | HEAD: module missing |
| A2 | `test_audit_row_guard.py::test_owner_update_of_immutable_column_is_refused` | Layer 1: as owner, `UPDATE auth_config_audit SET new_value='x'` → SQLSTATE 42501 | HEAD: the update succeeds |
| A3 | `::test_owner_delete_and_truncate_are_refused` (parametrized over the registry) | DELETE/TRUNCATE → 42501 | HEAD: succeed |
| A4 | `::test_lifecycle_column_update_is_allowed` | `erasure_ledger.status` change succeeds; `subject_user_uuid` change fails | Mutant: empty `mutable_columns` → first half red |
| A5 | `::test_maintenance_guc_works_for_owner_only` | Owner plus GUC → allowed. `SET LOCAL ROLE <test app role>` plus GUC → 42501 | Mutant: drop the ownership clause |
| A6 | `::test_user_delete_still_nulls_actor_columns` | Delete a user referenced by `auth_config_audit.changed_by`, `erasure_ledger.actor_user_id`, `takedown_notification.recipient_user_id` as the **app role**. Succeeds, rows survive with NULL | Mutant: remove the `nulls` allowance → red. This is the GDPR-erasure regression guard |
| A7 | `::test_cascade_delete_of_a_takedown_notice_is_refused` | Delete a notice → 42501 | HEAD: cascades silently |
| A8 | `test_db_roles_reconcile.py::test_app_role_cannot_update_delete_truncate_protected_tables` | Layer 2: create a NOLOGIN role, `reconcile_app_role(conn, role, None, provision=False)` plus create, `SET LOCAL ROLE`. INSERT works; UPDATE of an immutable column, DELETE and TRUNCATE → 42501 even with the trigger dropped inside the test transaction (proves the grant alone holds) | HEAD: module missing; mutant grant ALL → red |
| A9 | `::test_reconcile_restores_grants_after_no_acl_restore` | `REVOKE ALL` then reconcile → probes pass again; reconcile is idempotent (running it twice gives an equal report) | Mutant: skip step 3 |
| A10 | `::test_password_never_sent_in_clear` | Capture statements with a `before_cursor_execute` listener: contains `SCRAM-SHA-256$`, not the plaintext | Mutant: `PASSWORD %s` |
| A11 | `::test_reconcile_refuses_superuser_or_owner_app_role` | The report flags a superuser or owning role, and the boot check raises (D2 = a) | |
| A12 | `test_db_role_settings.py` (several cases) | URL resolution matrix of §4.1, including the explicit-`DATABASE_URL` error | HEAD: fields missing |
| A13 | `test_alembic_env_uses_migration_url.py` | env.py's URL equals `settings.MIGRATION_DATABASE_URL`, including `sslmode` (F1) | HEAD: env.py hand-builds from POSTGRES_* |
| A14 | `test_v<next>_migration_consistency.py` | Chain, head, vendor-neutrality, `assert_detected_at_or_after`, idempotent double upgrade, downgrade drops triggers, the revision constant equals the registry | Standard |
| A15 | `tests/api/test_audit_integrity_status.py` | super_admin only (403 for admin, 401 anonymous); `enforced` is false as owner and true when the engine is the app role (`SET ROLE` on a connection-bound session) | |
| A16 | Integration: a stack started with the split (`POSTGRES_APP_USER=ot_app POSTGRES_APP_PASSWORD=... ./opentr.sh start dev --fresh auditsplit --port-offset 100`, exported in the shell) | `SELECT current_user` from backend and from a celery worker is `ot_app`; the status endpoint reports `enforced: true`; `./scripts/run-dev-tests.sh --backend-only` with the `*_PORT` vars exported is green. **This is the oracle that no app path needs a forbidden privilege** | |
| A17 | (D3a) `test_audit_prune.py` | The app role deletes only rows older than the floor; a younger row survives with `keep_days=1`; ledger refused | |

Then run `python3 scripts/audit-tests.py backend/tests` to 0 open findings.

## 7. Verification steps

1. `./opentr.sh start dev`, then `./scripts/run-integration-tests.sh` green (Layer 1 on, split off).
2. `./opentr.sh reset dev` path on a `--fresh` stack (full chain), and a restart over an existing
   database (migration applied on startup).
3. A16 on a fresh stack. Then `./opentr.sh fresh-destroy auditsplit`.
4. Restore drill on the fresh stack: `./opentr.sh backup`, `restore` → backend restart → the
   status endpoint is `enforced` again.
5. `scripts/safe-precommit.sh run --all-files` and `--hook-stage pre-push`.
6. UI row checked in a browser, light and dark, `en` and `ar`.

## 8. Upgrade and rollback

- **Upgrade to v0.7.0, split off:** the migration adds the triggers. Nothing else changes. The
  risk is a code path that legitimately mutates a protected row and that the suite does not cover.
  It now fails loudly (42501). Mitigation: §5 step 2, plus a release-note line telling operators
  which error means what.
- **Enable:** set `POSTGRES_APP_USER`/`POSTGRES_APP_PASSWORD`, then `./opentranscribe.sh restart`
  (a recreate, so the env is re-read). The backend migrates as owner, reconciles, the boot check
  passes, and workers come up as the app role.
- **Disable:** unset both and recreate. Everything connects as the owner again. The role stays.
- **Roll back to v0.6.x:** `./opentranscribe.sh update --rollback` with the dump taken before the
  upgrade (restore drops the triggers along with everything else), or `alembic downgrade` by one
  revision (drops the triggers). v0.6.x ignores the new env vars and connects as `POSTGRES_USER`.
  Unset them first anyway.
- **Managed Postgres:** the owner needs `CREATEROLE`, or use `POSTGRES_APP_ROLE_PROVISION=false`
  with an IaC-created role.

## 9. Docs, CHANGELOG, seam

- New `docs-site/docs/operations/audit-integrity.md` (sidebar: next to `operations/security-hardening`
  in `docs-site/sidebars.ts`): what is protected and what is not (OpenSearch stream, fallback file,
  journal), the two postures (backend-migrates vs. migrate job), enable/disable, the status
  endpoint, the error message, restore behaviour.
- `security-hardening.md`: fix "Log Retention" (F2) to describe the real behaviour per D3, and link
  the new page. `configuration/environment-variables.md`: the new variables. `backend/app/db/CLAUDE.md`:
  a short "Roles" section (migration URL vs. runtime URL, reconcile hook, never `DROP ROLE`).
  `backend/alembic/CLAUDE.md`: "a data migration that rewrites a protected table runs under the
  maintenance GUC, which the runner sets".
- CHANGELOG `[Unreleased]` → **Security**: "Audit records in the database can no longer be changed
  or deleted by the application (immutability guard); optional database role split makes it
  enforceable (`POSTGRES_APP_USER`)". **Fixed**: Alembic now honours `DATABASE_URL`/SSL mode (F1);
  `AUDIT_LOG_RETENTION_DAYS` now does what it says (or is removed), per D3.
- **Cloud seam: bump `CLOUD_SEAM_VERSION` 12 → 13** (`backend/app/auth/constants.py`, plus a v13
  comment block). Reasons: the runtime role may no longer own tables, so an edition's runtime code
  that UPDATEs or DELETEs a protected table, or does DDL at runtime, fails. A new hook,
  `app.db.audit_protection.register_protected_table`, exists for edition tables. An edition's own
  additive migrations still run as owner. If the erasure-receipt plan lands in the same release,
  one bump with both changes described is fine. Separate PRs bump separately.

## 10. Risks

- An unknown legitimate mutation of a protected row in a path the suite misses → a 42501 in
  production. Mitigation: the full suite with the trigger on, plus A16.
- RI behaviour assumptions (§1.4) are pinned by A6. If a future Postgres changes them, A6 goes red.
- Superuser `POSTGRES_USER` still exists. An operator with shell access to the database can do
  anything. Out of scope, stated in the docs.
- Restores drop grants (`--no-acl`). Startup reconcile repairs them. A `RUN_MIGRATIONS_ON_STARTUP=false`
  deployment must run the CLI, so the docs and the migrate-job example must show it.
- Backend RCE still sees owner credentials in the default posture (§2). Documented.

## Decisions (owner delegated to industry best practice, 2026-11-01)

These are fixed; implement as written.

1. Role split: opt-in everywhere in v0.7.0, on for fresh installs in v0.8.0 (least privilege; no surprise change for existing installs).
2. Split requested but not effective (for example the app role is a superuser): refuse to start (fail closed).
3. `AUDIT_LOG_RETENTION_DAYS`: make it real. Default keep forever, hard floor 365 days, enforced in code; a value below the floor is rejected at startup.
4. `usage_event`: exempt (cloud-seam table), exemption written with its reason next to the protected-table list.
5. OpenSearch audit stream: out of scope for v0.7.0; file a follow-up issue for write-only OpenSearch roles.
6. Overlay that blanks the owner password on workers: ship in v0.7.0.

## Implementation notes (2026-11-01)

- Delivered as written. Deviations: `legal_acceptance` keeps its own v451 guard trigger (the generic
  trigger is not added to it); the app role keeps `DELETE` on it, which that guard still gates
  (`opentranscribe.legal_retention_sweep` plus `retain_until`). `erasure_receipt` is not registered here;
  register it in `audit_protection.PROTECTED_TABLES` (insert-only) and add it last-but-before
  `erasure_ledger` in the revision's `GUARDED` order when the receipt lane lands, then re-chain v453
  behind the receipt revision.
- Cloud seam: not bumped here (the coordinator sets the final number). The edition-facing facts to put
  in the seam notes: the runtime role may no longer own tables or UPDATE/DELETE a protected table,
  `app.db.audit_protection.register_protected_table` exists for edition tables, and an edition's own
  additive migrations still run as the owner.
- Rehearsal notes for the upgrade scenario: (1) the upgrade adds only functions and triggers, so a
  populated database needs no data step; (2) after the upgrade the status endpoint reports
  `enforced: false`, `guard_trigger_present: true` for every table; (3) with `POSTGRES_APP_USER` set
  on an existing database the first start creates the role and the next boot check reports
  `enforced: true`; (4) `./opentr.sh restore` of an older dump drops the grants, and the next backend
  start re-applies them (covered by `test_reconcile_restores_grants_after_a_no_acl_restore_and_is_idempotent`);
  (5) a rollback to a release without the feature needs `POSTGRES_APP_USER` unset, otherwise that
  release ignores it and connects as the owner, which is harmless.
- Verified on an isolated stack (`--fresh`): backend, a CPU worker and the GPU-family worker connect as
  the application role; workers hold no owner password; DELETE, TRUNCATE and a non-lifecycle UPDATE as
  that role fail with 42501; the measured status is `enforced`.
