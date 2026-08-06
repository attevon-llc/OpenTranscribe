---
sidebar_position: 3
title: Upgrading
description: How to safely upgrade OpenTranscribe between versions
---

# Upgrading

This guide covers how to safely upgrade OpenTranscribe between versions, including pre-upgrade preparation, the upgrade process, and rollback procedures.

## Pre-Upgrade Checklist

Before upgrading, complete these steps:

1. **Back up the database** -- this is non-negotiable
   ```bash
   ./opentr.sh backup
   ```
2. **Note your current version**
   ```bash
   cat VERSION
   # or check the UI footer / API response
   curl -s http://localhost:5174/api/health | python3 -m json.tool
   ```
3. **Read the changelog** for the target version at [CHANGELOG.md](https://github.com/attevon-llc/OpenTranscribe/blob/master/CHANGELOG.md)
4. **Check for breaking changes** -- major version bumps or migration notes
5. **Test in staging first** if you have a staging environment

:::danger
Always back up your database before upgrading. Database migrations run automatically on startup and cannot be undone without a backup.
:::

## Standard Upgrade Process

### Using opentr.sh (Recommended)

```bash
# Pull latest images and restart
./opentr.sh update
```

This command pulls the latest Docker images from Docker Hub and recreates all containers. Database migrations run automatically when the backend starts.

### Manual Upgrade

```bash
# Pull new images
docker compose pull

# Restart services with new images
docker compose up -d --force-recreate
```

### Upgrading to a Specific Version

To pin to a specific release instead of `latest`:

```bash
# Pull specific version tags — substitute the release you want.
# Available tags: https://github.com/attevon-llc/OpenTranscribe/releases
docker pull davidamacey/opentranscribe-frontend:v0.4.1
docker pull davidamacey/opentranscribe-backend:v0.4.1

# Update image tags in docker-compose.prod.yml or .env, then restart
docker compose up -d --force-recreate
```

## Database Migrations

### How Migrations Work

OpenTranscribe uses Alembic for database schema migrations. On every backend startup, the system automatically:

1. Detects the current database schema version
2. Stamps untracked databases with the appropriate version
3. Runs any pending migrations to bring the database up to date

You do not need to run migrations manually -- they execute automatically.

### Current Migration Chain

The migration chain progresses through these versions:

| Migration | Description |
|-----------|-------------|
| `v010` | Baseline schema |
| `v020` | System settings |
| `v030` | LDAP authentication |
| `v031` | Keycloak and PKI auth |
| `v040` | FedRAMP compliance fields |
| `v050` | Search settings |
| `v060` | Transcript overlap |
| `v070-v073` | PKI security, segment constraints, status enum |
| `v080` | Auth configuration |
| `v090-v091` | Error categories, speaker suggestion source |
| `v100-v120` | Query performance indexes |
| `v130-v140` | Processing model tracking, word timestamps |
| `v150-v170` | File retention, local fallback, Keycloak refresh tokens |
| `v180-v190` | Speaker attributes, collection default prompts |
| `v200-v211` | Schema reconciliation, groups and sharing |
| `v220-v270` | Speaker clusters, auto labeling, quality metrics, ASR providers, avatars |
| `v280-v320` | Upload sessions, gender fields, speaker constraints, cluster names |
| `v330` | Shared configs and prompts |
| `v340` | User media sources |
| `v350` | Diarization disabled flag |
| `v351` | AI summary settings |
| `v352` | Requested Whisper model per-transcription |
| `v353` | Segment unique index fix |
| `v355` | Independent diarization provider settings |

### What to Do if Migrations Fail

If a migration fails on startup:

1. **Check the backend logs** for the specific error:
   ```bash
   docker compose logs backend | grep -i "alembic\|migration\|error"
   ```
2. **Restore your backup** if the migration left the database in a broken state:
   ```bash
   ./opentr.sh restore backups/opentranscribe_backup_YYYYMMDD_HHMMSS.sql
   ```
3. **Report the issue** -- migration failures are bugs. File an issue with the error output.

:::note
All OpenTranscribe migrations use idempotent SQL (`IF NOT EXISTS`, `DO $$ ... END $$` blocks), which means they are safe to re-run. If a migration partially completed, restarting the backend will attempt to finish it.
:::

## Rolling Back

### Reverting to a Previous Version

If an upgrade causes issues, you can roll back:

```bash
# 1. Stop all services
docker compose down

# 2. Restore the database backup you made before upgrading
docker compose up -d postgres
# Wait for postgres to be ready
until docker compose exec postgres pg_isready -U postgres; do sleep 2; done
docker compose exec -T postgres psql -U postgres opentranscribe < backups/opentranscribe_backup_YYYYMMDD_HHMMSS.sql

# 3. Pull the previous version images
docker pull davidamacey/opentranscribe-frontend:vPREVIOUS
docker pull davidamacey/opentranscribe-backend:vPREVIOUS

# 4. Tag them as latest (so compose uses them)
docker tag davidamacey/opentranscribe-frontend:vPREVIOUS davidamacey/opentranscribe-frontend:latest
docker tag davidamacey/opentranscribe-backend:vPREVIOUS davidamacey/opentranscribe-backend:latest

# 5. Start all services
docker compose up -d
```

:::warning
You must restore the database backup when rolling back. Newer migrations may have altered the schema in ways incompatible with older code.
:::

## Major Version Upgrades

Major version upgrades (e.g., 0.x to 1.x) may include breaking changes that require extra steps.

### Embedding Migration (v3 to v4)

When upgrading across the speaker embedding architecture change:

- **speakers_v3** uses 512-dimensional pyannote embeddings
- **speakers_v4** uses 256-dimensional WeSpeaker embeddings
- The `speakers` alias automatically points to the active index

The migration runs through the Admin UI:

1. Navigate to **Admin Settings > Speaker Embeddings**
2. Start the embedding migration -- this re-extracts embeddings for all speakers
3. Monitor progress in the migration panel
4. Once complete, the alias swaps atomically to the new index

:::tip
Embedding migration can take significant time depending on the number of speakers and media files. Plan accordingly and run during a maintenance window.
:::

### Breaking API Changes

These only affect you if you call the OpenTranscribe REST API directly — from a script,
an integration, or another service. The web UI ships with each release and is always in sync.

The authoritative, always-current schema is the deployment's own OpenAPI document at
`/api/openapi.json` (browsable at `/api/docs`). Check it against your client after any upgrade.

#### `GET /api/files/{uuid}` — `tags` is now an array of objects

`tags` on the **file-detail** response used to be an array of tag name strings. It is now an array
of tag objects, matching what `GET /api/tags` has always returned:

```json
// Before
"tags": ["Important", "Meeting"]

// After
"tags": [
  { "uuid": "019ec90a-3f41-7aaa-8000-0000000000a1", "name": "Important", "source": "manual" },
  { "uuid": "019ec90a-3f41-7aaa-8000-0000000000a2", "name": "Meeting",   "source": "auto_ai" }
]
```

**If you were reading `tags` as strings, read `tag.name` instead** — `file.tags.map(t => t.name)`
in JavaScript, `[t["name"] for t in file["tags"]]` in Python. The two patterns that break are
`", ".join(file["tags"])` and `"Important" in file["tags"]`.

`uuid` and `name` are always present. `source` is nullable: `"manual"` for a tag a user applied,
`"auto_ai"` for one applied by auto-labeling, `null` for tags predating the field.

What did **not** change:

- `GET /api/files` (the list endpoint) has **no** `tags` field, before or after.
- `GET /api/tags`, `POST /api/tags`, `GET /api/tags/unused`,
  `POST /api/tags/files/{uuid}/tags` and `DELETE /api/tags/files/{uuid}/tags/{tag_name}` are
  unchanged — they already returned objects, and the delete route is still keyed by tag **name**.
- Search results still carry `tags` as plain strings (the search index stores tag names).
- No routes, permissions, or tag-visibility rules changed. No database migration is involved.

### Other Major Upgrade Considerations

- **OpenSearch version changes**: May require reindexing all data
- **Model format changes**: New AI models download automatically on first use
- **Authentication changes**: Review auth settings after upgrading, especially for LDAP/Keycloak configurations
- **Configuration changes**: Compare your `.env` with `.env.example` to identify new required variables

## Verifying the Upgrade

After upgrading, verify everything is working:

### 1. Check Service Health

```bash
# All containers should be running and healthy
./opentr.sh status

# Or check directly
docker compose ps
```

### 2. Check Backend Logs

```bash
# Look for successful startup and migration messages
docker compose logs backend --tail=50

# Verify no migration errors
docker compose logs backend | grep -i "error\|failed\|exception" | head -20
```

### 3. Verify API Health

```bash
curl -s http://localhost:5174/api/health | python3 -m json.tool
```

### 4. Test Core Functionality

- Log in to the web UI at `http://localhost:5173`
- Verify existing transcripts are accessible
- Search for a known transcript to confirm OpenSearch is working
- Upload a short test file to verify the transcription pipeline

### 5. Check Version

Confirm the UI footer or API response shows the expected version number.

## Common Upgrade Issues

### Container Fails to Start

```bash
# Check logs for the failing service
docker compose logs <service-name> --tail=100

# Common fix: recreate the container
docker compose up -d --force-recreate <service-name>
```

### Migration Lock Timeout

If the backend hangs on startup waiting for a migration lock:

```bash
# Check for stuck advisory locks in PostgreSQL
docker compose exec postgres psql -U postgres opentranscribe -c "SELECT * FROM pg_locks WHERE locktype = 'advisory';"

# Restart the backend
docker compose restart backend
```

### Model Compatibility

New versions may require updated AI models. If transcription fails after upgrading:

```bash
# Clear the model cache to force re-download
rm -rf ${MODEL_CACHE_DIR:-./models}/huggingface/hub/
docker compose restart celery-worker
```

### New Environment Variables

If the backend logs show warnings about missing configuration:

```bash
# Compare your .env with the latest template
diff .env .env.example

# Add any missing variables from .env.example to your .env
```

### OpenSearch Index Incompatibility

If search stops working after an upgrade:

```bash
# Check OpenSearch health
curl -s http://localhost:5180/_cluster/health | python3 -m json.tool

# If indices need rebuilding, use the Admin UI "Reindex All" function
# Or via API:
curl -X POST http://localhost:5174/api/admin/reindex -H "Authorization: Bearer <token>"
```

### Permission Errors on Model Cache

After upgrading, the container user (UID 1000) may not have access to cached models:

```bash
# Fix permissions
./scripts/fix-model-permissions.sh

# Or manually
sudo chown -R 1000:1000 ${MODEL_CACHE_DIR:-./models}/
```
