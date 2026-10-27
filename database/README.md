<div align="center">
  <img src="../assets/logo-banner.png" alt="OpenTranscribe Logo" width="300">

  # Database
</div>

OpenTranscribe uses PostgreSQL. **The Alembic migration chain in `backend/alembic/versions/` is the
sole authority for the schema.** This directory holds only a legacy reference file.

## Contents

```
database/
├── init_db.sql    # Legacy reference snapshot. NOT used to create the schema.
└── README.md      # This file
```

`init_db.sql` opens with a "REFERENCE ONLY" header: it is preserved for documentation and is far
behind the current models (it defines a few dozen tables; the SQLAlchemy models define many more).
It is not mounted by the Compose files and nothing applies it. Alembic revisions still mention it
because releases up to v0.3.3 bootstrapped from it, and the migration runner can recognise such a
database and stamp it. Do not edit it to change the schema, and do not treat its column lists as
current.

## Where the schema actually lives

| What | Where |
|------|-------|
| Schema authority (revisions) | `backend/alembic/versions/` (`v010_baseline.py` is the first) |
| ORM models (the table list; table names are singular, e.g. `user`, `media_file`, `transcript_segment`, `speaker`, `tag`, `collection`, `task`) | `backend/app/models/` |
| API schemas | `backend/app/schemas/` |
| Startup migration runner and schema-version detection | `backend/app/db/migrations.py` |
| Seed data and the bootstrap admin | `backend/app/initial_data.py` |
| Written overview of the tables | `docs/database-schema.md` (check it against the models before relying on it) |
| Model-versus-live-schema comparison | `scripts/check-schema-drift.py` |

The first `super_admin` is created by `app/initial_data.py` at startup. In a hardened environment
it uses `INITIAL_ADMIN_EMAIL` / `INITIAL_ADMIN_PASSWORD`, or a generated password logged once;
only a relaxed (development) environment seeds the documented dev credential. There is no separate
creation script. Further super_admins are promoted from the admin UI.

## How migrations run

`run_migrations()` in `backend/app/db/migrations.py` runs on backend startup (unless
`RUN_MIGRATIONS_ON_STARTUP=false`, for deployments that run a separate migrate job) under a
PostgreSQL advisory lock, and handles three cases:

1. **Empty database:** `alembic upgrade head`.
2. **Existing database with no Alembic version table:** detect which revision its schema matches
   (`_detect_schema_version()`), `stamp` it, then upgrade.
3. **Already tracked:** apply pending revisions.

Running `alembic upgrade head` by hand is only for production-style operations; in development
the backend does it on every start.

## Changing the schema

The authoritative procedure is in `backend/app/db/CLAUDE.md`; `backend/alembic/CLAUDE.md` covers
authoring the revision file. In short:

1. Add a new idempotent revision in `backend/alembic/versions/` (`IF NOT EXISTS`, guarded `DO`
   blocks). Take a revision number that does not collide with other open branches, and derive the
   current head from the `down_revision` graph rather than from any list written in prose.
2. Update the SQLAlchemy model in `backend/app/models/`.
3. Update the Pydantic schema in `backend/app/schemas/` if it is exposed through the API.
4. Add a detection guard for the new revision to `_detect_schema_version()` (a marker unique to
   the revision, at the top of the newest-first ladder), or untracked databases are mis-stamped.
5. Test both paths: a full chain from scratch (`./opentr.sh reset dev`) and a restart over an
   existing database.

Do not autogenerate migrations inside the hot-reloading dev container.

## Operations

```bash
./opentr.sh start dev                         # migrations run automatically at backend startup
./opentr.sh backup                            # database backup (see ./opentr.sh for destination)
./opentr.sh restore backups/<file>.sql        # restore a backup
./opentr.sh reset dev                         # DESTRUCTIVE: asks for confirmation, then deletes
                                              # all data and rebuilds from the full migration chain
./opentr.sh data-paths                        # resolved live data paths; check before any reset
python backend/scripts/db_inspect.py          # inspect the tag tables (see backend/scripts/README.md)
```

`./opentr.sh reset` deletes data. If a NAS or bind-mount overlay is active it can be the live
dataset: run `./opentr.sh data-paths` first, and use `./opentr.sh start dev --fresh <name>` for any
throwaway or experimental database instead.

## References

- [`backend/app/db/README.md`](../backend/app/db/README.md) - database layer layering and sessions
- [`backend/alembic/README.md`](../backend/alembic/README.md) - Alembic setup
- [PostgreSQL documentation](https://www.postgresql.org/docs/)
- [Alembic documentation](https://alembic.sqlalchemy.org/)
