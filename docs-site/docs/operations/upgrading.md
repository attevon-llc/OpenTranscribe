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
   ./opentranscribe.sh backup
   ```
   Or dump directly, if you don't have the script on hand:
   ```bash
   docker compose exec -T postgres pg_dump -U postgres opentranscribe \
     > "opentranscribe-backup-$(date +%Y%m%d-%H%M%S).sql"
   ```
   (In a git clone of the repo, `./opentr.sh backup` does the same thing.)
2. **Note your current version**
   ```bash
   ./opentranscribe.sh version
   # or, without the script:
   curl -s http://localhost:5174/api/version
   curl -s http://localhost:5174/api/health | python3 -m json.tool
   ```
3. **Read the changelog** for the target version at [CHANGELOG.md](https://github.com/attevon-llc/OpenTranscribe/blob/master/CHANGELOG.md)
4. **Check for breaking changes** -- major version bumps or migration notes
5. **Test in staging first** if you have a staging environment

:::danger
Always back up your database before upgrading. Database migrations run automatically on startup and cannot be undone without a backup.
:::

## Standard Upgrade Process

### Using opentranscribe.sh (Recommended)

```bash
# Pull the newest images for your pinned version and restart
./opentranscribe.sh update

# Move to a specific release
./opentranscribe.sh update --version v0.5.0

# Also refresh compose files, scripts and the .env template
./opentranscribe.sh update-full
```

:::note `opentranscribe.sh`, not `opentr.sh`
`opentranscribe.sh` is the management script the installer places next to your
compose files — it is what you have. `opentr.sh` is the *development* script and
only exists in a git clone of the repository. This page previously said
`./opentr.sh update`, which no installed deployment can run.
:::

`update` pulls images and restarts in phases, polling the backend's own `/health`
rather than letting Compose decide when the backend is ready. That matters on an
upgrade: running the migration chain on a populated database can take minutes,
and Compose's dependency resolver would otherwise give up and terminate the
backend mid-migration.

Migrations run automatically when the backend starts. When it is finished, the
script prints the version that is actually running.

### Manual Upgrade

```bash
# Pull new images
docker compose pull

# Restart services with new images
docker compose up -d --force-recreate
```

### Upgrading to a Specific Version

Every service image resolves `${OT_IMAGE_TAG:-latest}`, so pinning is one
setting rather than an edit to each service:

```bash
# Recommended — sets OT_IMAGE_TAG in .env, then pulls and restarts in phases
./opentranscribe.sh update --version v0.5.0

# Equivalent by hand
echo 'OT_IMAGE_TAG=v0.5.0' >> .env
docker compose pull && docker compose up -d --force-recreate
```

A fresh install pins itself to the release it installed, so a deployment tracks a
known version rather than whatever `:latest` happens to be. See the
[releases page](https://github.com/attevon-llc/OpenTranscribe/releases) for
available versions.

### Returning to the previous version

```bash
./opentranscribe.sh update --rollback
```

This re-pins the image tag recorded by the last `update --version`.

:::danger Images roll back; the database does not
The migration chain is one-way. Rolling images back does **not** revert schema
changes, and an older image may not be able to read the newer schema. A real
rollback means restoring the backup you took before upgrading. `update` refuses a
downgrade unless you pass `--rollback` or `--force-downgrade`.

`update --rollback` also checks, before touching anything, whether the version
you are rolling back to can even read the database as it currently stands — if
it cannot, it refuses (exit 1) and tells you to restore a pre-upgrade backup
first. Override with `--force-downgrade` if you are certain.
:::

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
| `v360-v362` | Pipeline timing instrumentation (durable wall-clock metrics, imohash fingerprinting) |
| `v363` | AWS Transcribe dual-credential support (access key ID column) |
| `v364` | Content redaction columns (PII / profanity / toxicity) |
| `v365` | Prompt sharing attribution |
| `v366` | Watch Sources auto-import tables (issue #26) |
| `v367`, `v371` | Cloud-edition multitenancy seams + a schema-shape repair for pre-release deployments |
| `v368` | Defensive guard: native `uuid` column type on every identifier |
| `v369`, `v377` | Superuser/role invariant hardening — see [Breaking Changes (v0.5.0)](#breaking-changes-v050) |
| `v370` | Media file quarantine / takedown columns |
| `v372-v373` | Organization-scoped audit events and speaker clusters (issue #262) |
| `v374` | Per-user tag ownership (security fix for cross-user tag-name disclosure) |
| `v375-v376` | RAG chat tables and chat projects (issues #52, #360) |
| `v378` | Directory (LDAP/OIDC) group-to-in-app-group mapping |
| `v379-v380` | OIDC config/identity rename — see [The OIDC surface is renamed](#the-oidc-surface-is-renamed--configuration-keys-routes-and-the-admin-tab) |
| `v381` | Administrator approval state for newly provisioned accounts |
| `v382` | SCIM 2.0 bearer tokens |
| `v383` | SAML auth type and identity column (issue #35) |
| `v384` | Chat reasoning-content column (collapsible "thinking" display) |
| `v385` | Drop orphaned tables left by removed features (issue #398) |
| `v386-v387` | Tag sharing with users/groups, plus actor-FK and CHECK-constraint repairs |
| `v388` | Tenant scope for `user_group` (issue #262) |
| `v389` | Erasure ledger — durable record of GDPR Art. 17 erasure requests (#442) |
| `v390` | Deterministic ingest artifacts for the no-LLM summary tier (#383/#403) |
| `v391` | `media_file.recorded_date` and its provenance (#403) |
| `v392` | `media_file.redaction_coverage` — which detectors a scan actually ran (#403) |
| `v393` | Timing columns for transcribe-diarize overlap and progressive presentation |

### What to Do if Migrations Fail

If a migration fails on startup:

1. **Check the backend logs** for the specific error:
   ```bash
   docker compose logs backend | grep -i "alembic\|migration\|error"
   ```
2. **Restore your backup** if the migration left the database in a broken state:
   ```bash
   ./opentranscribe.sh restore backups/opentranscribe-backup-YYYYMMDD-HHMMSS.sql
   ```
   This replaces the database entirely (drop + recreate + replay + verify) rather than
   layering the backup over the broken schema — a plain `psql < backup.sql` into an
   already-populated database fails silently (see
   [Restore Procedures](./backup-restore.md#restoring-the-database)). It stops the
   backend/Celery services, prompts for confirmation (`--yes` to skip), takes a safety
   dump of the current (broken) database first, then — **only if the backup's own
   schema version matches the version that was just running** — restarts services.
   On a mismatch it completes the restore but leaves services **stopped**, and prints
   the two next moves (roll the app back to match the backup, or explicitly opt in to
   letting the current version migrate the backup forward). This is what stops a
   restore from silently re-migrating an older backup forward before you get to look
   at it — see [Rolling Back](#rolling-back) below, and
   [Restore Procedures](./backup-restore.md#restoring-the-database) for the
   `--migrate-forward` / `--no-restart` flags.
   Note this restores **PostgreSQL only** — MinIO and OpenSearch are not rolled back in
   lockstep, so reindex from Admin → Search afterwards if needed.
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

# 2. Restore the database backup you made before upgrading (drops + recreates + replays +
#    verifies — see Restore Procedures in Backup & Restore for what this does). Because
#    this backup predates the upgrade, its schema head will not match whichever image is
#    still pinned in .env — restore detects that and leaves services STOPPED for you
#    rather than restarting the (still-newer) image over it.
docker compose up -d postgres
./opentranscribe.sh restore backups/opentranscribe_backup_YYYYMMDD_HHMMSS.sql

# 3. Re-pin the image tag to the previous version BEFORE starting anything — this is
#    what `update --rollback` does (recommended over the manual pull/tag/up sequence
#    below): it also refuses if the target version cannot read the current schema.
./opentranscribe.sh update --rollback
#    or, for a version not tracked by --rollback's recorded target:
#      docker pull davidamacey/opentranscribe-frontend:vPREVIOUS
#      docker pull davidamacey/opentranscribe-backend:vPREVIOUS
#      docker tag davidamacey/opentranscribe-frontend:vPREVIOUS davidamacey/opentranscribe-frontend:latest
#      docker tag davidamacey/opentranscribe-backend:vPREVIOUS davidamacey/opentranscribe-backend:latest
#      docker compose up -d
```

:::warning
You must restore the database backup when rolling back. Newer migrations may have altered the schema in ways incompatible with older code.
:::

:::danger The old order corrupted the restore (issue #610)
This recipe used to restore the database, THEN re-pin the image — steps 3-5 pulled and
tagged the previous version only *after* `./opentranscribe.sh restore` had already restarted
whatever was running. Because the `.env` image tag hadn't moved yet at that point, the
service that restarted was the **newer, still-pinned** image — which runs its own
migrations on startup, and silently migrated the just-restored, deliberately-older
backup straight back to the newer schema before you ever got to step 3. Every operator
who followed the old recipe got the corruption. Re-pinning the image **before** starting
anything (`update --rollback`, step 3 above) is what fixes it; `opentranscribe.sh restore` itself
now also refuses to restart into that trap on its own (see the note in
[What to Do if Migrations Fail](#what-to-do-if-migrations-fail) above).
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

### Breaking Changes (v0.5.0)

These affect operators upgrading to v0.5.0 regardless of whether you call the REST API
directly.

#### Six deployment-configuration panels now require `super_admin`, not `admin`

**ASR provider**, **Engine configuration**, **Backups**, **Media Mirror**, **Watch sources**,
and the **Redaction policy** floor now require the `super_admin` role instead of `admin`. They
configure how the deployment runs, and several store infrastructure credentials (S3 keys, SMB
passwords, SMTP passwords) that a team-level admin has no reason to read or replace.

:::danger ACTION REQUIRED if a plain `admin` manages any of those six panels
Promote that account to `super_admin` (Settings → Users → Role → Super Admin) **before
upgrading**, or hand the work to an existing super admin. Nothing else changes tier: user
accounts, tasks, search, and speaker maintenance stay at `admin`. Creating additional super
admins from the UI is new in this release — the role selector previously offered only `user`
and `admin`.
:::

#### The OIDC surface is renamed — configuration keys, routes, and the admin tab

Config keys are now `oidc_*`, the admin tab is **OIDC**, and the routes are
`/api/auth/oidc/login` and `/api/auth/oidc/callback`. No identity provider needs
reconfiguring (the registered redirect URI still points at the SPA's `/login` page), and every
`KEYCLOAK_*` environment variable keeps working permanently — the legacy spelling even wins
when both are set. Stored database configuration is renamed automatically by migration `v377`.

What does break: a script that writes `PUT /api/admin/auth-config/keycloak`, reads a
`keycloak_*` key out of `GET /api/admin/auth-config`, or calls
`/api/auth/keycloak/{login,callback}` directly. `GET /api/auth/methods` now reports `"oidc"`
in `methods`; its `keycloak_enabled` field is retained for one minor release so a cached SPA
bundle keeps rendering the SSO button, then removed.

#### `POST /api/auth/token/refresh` now requires the CSRF header for cookie-authenticated clients

Minting a new session from the refresh cookie alone is no longer CSRF-exempt — that's exactly
what a forged cross-site request would target. Browsers are unaffected; the SPA already
double-submits the token, and the CSRF cookie's lifetime was extended to match the refresh
cookie's. **A non-browser API client that sends cookies must now also send `X-CSRF-Token`**;
clients using `Authorization: Bearer` are exempt as before.

#### `PKI_TRUSTED_PROXIES` is now required whenever PKI is enabled

Header-sourced PKI authentication is refused when no trusted proxy is allow-listed, instead of
being accepted with a warning. Hardened deployments already refused to *start* in that
configuration, so this only changes development and evaluation stacks that enabled PKI through
the admin UI. Set it to the address the backend sees the reverse proxy arrive from.

#### `GET /api/auth/methods` no longer always advertises `local`

`methods` previously contained `"local"` unconditionally. It now reflects `local_enabled`, so a
deployment whose identity lives entirely in an external IdP reports only the methods it
actually accepts. The response also gained `local_enabled` and `allow_registration` fields.

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
./opentranscribe.sh status

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

After upgrading, the container user (UID 1000, GID 999 — `appuser` is created with
`useradd -u 1000` but `groupadd -r`, issue #580) may not have access to cached models:

```bash
# Fix permissions
./scripts/fix-model-permissions.sh

# Or manually
sudo chown -R 1000:999 ${MODEL_CACHE_DIR:-./models}/
```
