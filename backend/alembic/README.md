<div align="center">
  <img src="../../assets/logo-banner.png" alt="OpenTranscribe Logo" width="200">

  # Alembic Database Migration Management
</div>

## Database Authority

Alembic migrations in `backend/alembic/versions/` are the **sole authority** for the database schema in all environments. The baseline migration (`v010_baseline.py`) creates the full schema from scratch and subsequent migrations handle schema evolution.

`database/init_db.sql` does still exist, but it is **legacy reference only and is not used for schema** — do not treat it as authoritative or keep it in sync.

## Current Migration Chain

The chain spans v010 (baseline) through the current head. The revision COUNT is deliberately
not written down here: it was recorded as "59" and was 74 by the time anyone checked, which is
the same way `expected-schemas.tsv` died. Derive it —
`scripts/release-tests/lib/alembic-head.py` walks the `down_revision` graph, and
`ls backend/alembic/versions/*.py | wc -l` is the crude version. Run
`alembic history` for the authoritative list and `alembic heads` to confirm exactly one head —
the ranges below are a rough map, and the chain is neither numerically contiguous nor
guaranteed to match filename order (see `CLAUDE.md` in this directory for the `v270` branch).

| Range | Description |
|-------|-------------|
| v010 | Baseline schema (all core tables) |
| v020-v073 | System settings, auth methods (LDAP, Keycloak/OIDC, PKI), FedRAMP controls, search settings, transcript overlap, queued/downloading statuses |
| v080-v140 | Auth config, error categories, speaker suggestion source, performance and FK indexes, processing model tracking, word timestamps |
| v150-v211 | File retention, local-fallback flag, speaker attributes, collection default prompt, schema reconciliation, groups and sharing |
| v220-v260 | Speaker clusters, auto-labeling, clustering indexes, cluster quality |
| v270-v353 | ASR provider support, upload sessions, password reset, gender, speaker constraints, shared configs/prompts, user media sources, diarization/AI-summary settings, requested Whisper model |
| v355-v373 | Diarization settings, pipeline timing, imohash, content redaction, prompt sharing, watch sources, cloud seams, UUID guard, superuser role invariant, quarantine, audit and cluster org scoping |
| v374-v393 | Per-user tags, chat, auth identity hardening (OIDC, SCIM, SAML, approval), IdP group mapping, tag sharing, erasure ledger, file facts, redaction coverage, overlap timing |
| v397-v448 | Tenant-owned tags/collections/speaker names, playback renditions, support access, requested transcription options, processing provenance, speaker correction carry-over, legal acceptance, takedown origin and notices, organization policy, collection visibility, content holds, one active task row per file step |

## Startup Runner (`app/db/migrations.py`)

On backend startup (the FastAPI lifespan in `app/main.py`), `run_migrations()` automatically:
1. Takes a PostgreSQL advisory lock so concurrent replicas do not race
2. Detects the current database schema version (for databases that predate Alembic tracking)
3. Stamps untracked databases with the appropriate version
4. Runs `alembic upgrade head` to bring the DB to the current version

So migrations apply automatically on every deploy and no manual `alembic upgrade head` is needed. A migration failure aborts startup. Set `RUN_MIGRATIONS_ON_STARTUP=false` only where a separate migrate job owns migrations; readiness then checks the schema is at head. Celery workers never run migrations.

## Migration Workflow

All schema changes must use Alembic migrations:

1. Find the current head: `python3 scripts/release-tests/lib/alembic-head.py backend` (from the repo root)
2. Create a new, hand-written migration file in `backend/alembic/versions/`, named `v<NNN>_<description>.py` with `revision` equal to the filename
   - Use idempotent SQL with `IF NOT EXISTS` for safety
   - Set `down_revision` to the current head
   - Do not use `alembic revision --autogenerate` output as-is (hash id, non-idempotent)
3. Update SQLAlchemy models in `backend/app/models/` to match
4. Update Pydantic schemas in `backend/app/schemas/` if needed
5. Add a detection guard for the new revision in `_detect_schema_version()` in `backend/app/db/migrations.py`
6. Test with `./opentr.sh reset dev` (drops DB, runs full chain from v010) and by restarting over an existing database

The full procedure and the traps (number collisions between parallel branches, detection arms) are in `app/db/CLAUDE.md` and `alembic/CLAUDE.md`.

### Running Alembic by hand
`alembic.ini` uses `script_location = alembic`, relative to `backend/`. Run commands inside the backend container, where the database environment is set (`./opentr.sh shell backend`, then `alembic current`, `alembic history`, `alembic heads`).

### Example Migration Pattern
```python
# backend/alembic/versions/v<NNN>_add_new_column.py  (NNN = next free number)
revision = "v<NNN>_add_new_column"
down_revision = "<current head revision id>"

def upgrade():
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'media_file' AND column_name = 'new_column'
            ) THEN
                ALTER TABLE "media_file" ADD COLUMN new_column VARCHAR(256);
            END IF;
        END $$;
    """)

def downgrade():
    op.execute('ALTER TABLE "media_file" DROP COLUMN IF EXISTS new_column')
```

## Reset vs Restart

- `./opentr.sh reset dev` — **Deletes all data**, drops DB, reruns full migration chain (v010 to the current head)
- `./opentr.sh restart-backend` — Restarts backend container; migrations run automatically, no data loss

## Maintaining Consistency

- Never edit migration files that have already been deployed
- Keep SQLAlchemy models (`app/models/`) in sync with migrations
- Document breaking schema changes
- Speaker UUIDs provide cross-video speaker identity — do not alter UUID columns in migrations
- `env.py` imports `app.models` so `Base.metadata` is populated; keep that import
