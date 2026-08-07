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
(the `role`/`is_superuser` CHECK), `v373_add_cluster_organization_id`, `v374_add_tag_user_id` (per-user tag ownership —
the one revision so far that both backfills *and* splits rows, and that drops a
pre-existing UNIQUE constraint), `v375_add_chat_tables`, head currently
`v376_add_chat_projects` (chat projects; note its FK is ON DELETE **SET NULL**, so
deleting a project leaves its conversations ungrouped rather than destroying them).

## Gotchas

- **The `pg_advisory_lock(42)` guard is present but does not actually cover the migration.**
  It is taken inside a `with engine.connect()` block that then exits, and the same engine is
  `dispose()`d at the top of the run — closing the pooled session **releases the
  session-scoped lock before `command.upgrade()` runs**. The `finally` block unlocks on a
  *different* engine/session that never held it. Concurrent backend replicas starting
  together can still race. Treat this as unfixed; if you touch it, hold one dedicated
  connection open for the whole run.
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
