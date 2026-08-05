# backend/alembic — writing a migration revision

## Purpose

Alembic is the sole schema authority; `database/init_db.sql` is **legacy reference only, not
used for schema**. The 5-step change procedure (revision → model → schema → detection guard →
test both paths) lives in `app/db/CLAUDE.md` — **do not duplicate it here**. This file is about
authoring the revision file itself.

## Key files

- `versions/` — 60 revisions, `v010_baseline` … head `v374_add_tag_user_id`.
- `env.py` — builds the URL from `POSTGRES_*` env (`load_dotenv()`), `target_metadata =
  Base.metadata`. No `compare_type`, no naming convention.
- `script.py.mako` — **stock alembic template**: it emits neither the `v###` id nor idempotent
  SQL. `alembic.ini`'s `script_location = alembic` is **cwd-relative** (run from `backend/`).
- `README.md` — prose, partly stale (claims the chain ends at v355).

## Conventions / patterns

- **Revision ids are descriptive, not hashes**: `v<NNN>_<snake_description>`, filename ==
  revision id. Pick the next free number, hand-write the file, and set the `revision` /
  `down_revision` string literals yourself. `alembic revision --autogenerate` produces a hash id
  and non-idempotent `op.add_column` — never ship its output as-is. (The runner widens
  `alembic_version.version_num` to `VARCHAR(128)` on every start to fit these names.)
- **All SQL must be idempotent.** 56 of 59 revisions are raw `op.execute` (only 3 use
  `op.add_column`/`create_table`); 49 use `IF NOT EXISTS`, 41 wrap DDL in
  `DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM information_schema.columns …) THEN … END IF; END $$;`.
  Reason: the startup runner stamps *untracked* production DBs by schema fingerprint, so a
  revision routinely re-runs against a database that already has part of its changes. Read
  `v373` for the plain additive shape, `v371` for the guarded rename/backfill shape, and
  `v374` for a data-splitting backfill (its `BACKFILL_SQL` is a module-level constant so the
  consistency test can replay it against seeded rows — the `DO $$` block is written to be
  re-runnable, which is also what makes it idempotent).
- `downgrade()` mirrors with `DROP … IF EXISTS`. Repair revisions may deliberately implement
  only the additive half — say so in the docstring (see `v371`).
- Docstring first: **why**, which deployments are affected, and what "community edition"
  behaviour is. These docstrings are the change log for the schema.
- Core stays vendor-neutral: the CI seam guard greps for `clerk|stripe`. A migration mentioning
  them fails the build (`tests/unit/test_v373_migration_consistency.py` asserts this).

## How it connects

- Every new revision **must** get a detection arm in `_detect_schema_version()`
  (`app/db/migrations.py`), inserted at the TOP of the newest-first ladder and keyed on a marker
  column/table/constraint unique to the revision. Skip it and untracked DBs are mis-stamped to
  the previous version and never get your DDL.
- Pair the revision with a consistency test modelled on
  `tests/unit/test_v37{2,3}_migration_consistency.py`: asserts `down_revision`, that the
  revision is head, vendor-neutrality, and that `_detect_schema_version()` returns it against
  the live post-migration schema.
- The known `pg_advisory_lock(42)` race in the startup runner is documented in
  `app/db/CLAUDE.md` — unfixed, don't re-diagnose it here.

## Gotchas

- **Never reuse a revision id that has reached anyone's DB.** `v367_add_cloud_seams` was
  rewritten in place after weeks on public master; Alembic won't re-run an id a DB already
  recorded, which is why
  `v371_repair_cloud_seams_columns` exists purely to repair the divergent shape. Always add a
  new revision instead.
- **The chain is not numerically contiguous and once branched.** `v270_add_profile_avatar`
  revises `v260`, and `v270_add_asr_provider_support` revises `v270_add_profile_avatar` — two
  files sharing the `v270` prefix. Trust `down_revision`, not the filename number: run
  `alembic history` before choosing a parent, and check `alembic heads` returns exactly one.
- That pre-linearisation `v250 → v270` branch could skip `v230_add_auto_labeling`, so
  `_repair_skipped_v230()` runs as a **permanent post-hook after every successful migration**
  and imports `versions/v230_add_auto_labeling.upgrade` directly. Don't rename, restructure, or
  delete that revision module.
- Dev applies migrations automatically on backend startup; `alembic upgrade head` by hand is
  **production-only**. Test a new revision both ways: `./opentr.sh reset dev` (full chain from
  scratch) and a rebuild-and-restart over an existing DB.
