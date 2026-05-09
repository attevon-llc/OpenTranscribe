---
description: Open a psql shell against the running OpenTranscribe Postgres container, or run a one-off SQL query if the user provides one as $ARGUMENTS.
---

# Database Shell

Open an interactive `psql` against the OpenTranscribe Postgres container, or run a query non-interactively.

## Behavior

- If `$ARGUMENTS` is empty: tell the user to run the interactive command themselves with `! ./opentr.sh shell postgres` (Claude can't drive an interactive psql session).
- If `$ARGUMENTS` is a SQL statement: execute it via `docker compose exec -T postgres psql -U postgres -d opentranscribe -c "$ARGUMENTS"` and return the output formatted.

## Helpful queries

Common diagnostic queries the user might want:

```sql
-- Migration version
SELECT version_num FROM alembic_version;

-- Active users
SELECT id, email, role, is_active FROM "user" ORDER BY created_at DESC LIMIT 20;

-- Recent files
SELECT id, filename, status, created_at FROM media_file ORDER BY created_at DESC LIMIT 10;

-- Failed Celery tasks (if you have a task table)
SELECT * FROM task WHERE status = 'failed' ORDER BY created_at DESC LIMIT 10;
```

## Notes

- The Postgres container is `opentranscribe-postgres`. DB name and user come from `.env` (default `opentranscribe` / `postgres`).
- For schema introspection use `\d <table>` (interactive only) or query `information_schema.columns`.
- **Do not write destructive SQL** (`DROP`, `DELETE`, `TRUNCATE`, `UPDATE` without `WHERE`) without explicit user confirmation.
