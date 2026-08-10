# OpenTranscribe Database Migration Strategy

## Overview

OpenTranscribe uses Alembic for database migrations. **Migrations run automatically on backend startup** — no manual intervention required for most users.

## How It Works

When the backend container starts, it automatically:

1. **Detects the database state** (fresh, legacy, or already migrated)
2. **Stamps the appropriate version** if Alembic tracking doesn't exist
3. **Applies any pending migrations**

### Upgrade Scenarios

| Scenario | What Happens |
|----------|--------------|
| Fresh install | Tables created, Alembic stamps as current version |
| Upgrade from v0.1.0 | Alembic stamps baseline, applies full migration chain |
| Upgrade from v0.3.x | Alembic applies pending v0.4.0 migrations |
| Already at head | No-op |

---

## Upgrading to v0.4.0

For existing users, the upgrade is automatic:

```bash
# 1. Backup your database (recommended before any upgrade)
./opentr.sh backup

# 2. Pull new version
git pull origin main

# 3. Restart services - migrations run automatically on startup
./opentr.sh stop
./opentr.sh start dev  # or: ./opentr.sh start prod
```

The backend detects your current schema version and applies every pending
migration up to the current head.

> **The head moves.** This document was written for v0.4.0, when the head was
> `v355_add_diarization_settings`; it is now well past that. Rather than
> restating a value that goes stale, read it from the repo:
>
> ```bash
> python3 scripts/release-tests/lib/alembic-head.py backend
> ```
>
> or from a running stack — `/health/ready` reports `schema_revision` and
> `schema_head` on every deployment.

---

## Schema Changes: v0.3.x → v0.4.0

### Migrations added in v0.4.0 (v330 – v355)

| Migration | Description |
|-----------|-------------|
| `v330_add_shared_configs_and_prompts` | Shared ASR/LLM configs and per-collection prompts |
| `v340_add_user_media_sources` | User-configurable media source preferences |
| `v350_add_diarization_disabled` | Per-file diarization disable flag |
| `v351_add_ai_summary_settings` | Per-file and per-user AI summary enable/disable |
| `v352_add_requested_whisper_model` | Requested Whisper model override per file |
| `v353_fix_segment_unique_index` | Unique index fix for transcript segments |
| `v355_add_diarization_settings` | Per-user diarization settings (min/max speakers, provider) |

### Key Schema Additions in v0.4.0

**`v330` — Shared configs and per-collection prompts:**
- `shared_asr_config` table: ASR configurations shareable across users
- `shared_llm_config` table: LLM configurations shareable across users
- `collection.default_prompt` column: per-collection summarization prompt

**`v350` — Diarization disable:**
- `media_file.diarization_disabled` (BOOLEAN): skip diarization for a specific file

**`v351` — AI summary settings:**
- `media_file.ai_summary_enabled` (BOOLEAN): per-file summary enable/disable
- `user_settings.ai_summary_auto_generate` (BOOLEAN): per-user auto-generate toggle

**`v352` — Requested Whisper model:**
- `media_file.requested_whisper_model` (VARCHAR): per-file model override (set at upload or reprocess)

**`v355` — Diarization settings:**
- `user_settings.diarization_provider` (VARCHAR): local, cloud, off
- `user_settings.min_speakers` / `max_speakers` (INTEGER): default speaker detection range

### No Breaking Changes
- No columns removed or renamed from existing tables
- No data type changes to existing columns
- All existing data is preserved

---

## PyAnnote v4 Embedding Migration

OpenTranscribe v0.4.0 uses PyAnnote v4 WeSpeaker embeddings (256-dim, index `speakers_v4`) as the active speaker index. Users upgrading from v0.3.x (which used `speakers_v3`, 512-dim pyannote/embedding) need to re-extract embeddings for existing speakers.

### Automatic Detection

On startup, the backend checks whether the `speakers` OpenSearch alias points to `speakers_v4`. If it still points to `speakers_v3`, the admin UI shows a migration banner.

### Migration via Admin UI

1. Navigate to Settings → Admin → Embedding Migration
2. Click "Start PyAnnote v4 Migration"
3. The migration runs as a background Celery task on the embedding worker
4. Progress is shown in real-time via WebSocket
5. When complete, the `speakers` alias is atomically swapped to `speakers_v4`

### Migration via API

```bash
# Start migration (admin only)
curl -X POST http://localhost:5174/api/admin/embedding-migration/start \
  -H "Authorization: Bearer <admin-token>"

# Check progress
curl http://localhost:5174/api/admin/embedding-migration/status \
  -H "Authorization: Bearer <admin-token>"
```

### Migration Details

- Uses the `celery-embedding-worker` (dedicated worker, does not block transcription)
- Processes speakers in batches of 50 with atomic Lua increment counters in Redis
- Re-extracts embeddings from the original audio stored in MinIO
- Falls back gracefully if a file's audio is no longer available
- Migration can be paused and resumed
- After migration, `speakers_v3` index is retained for 30 days before cleanup

### Rollback

If the v4 migration produces poor results, the alias can be reverted:

```bash
# Via admin API
curl -X POST http://localhost:5174/api/admin/embedding-migration/rollback \
  -H "Authorization: Bearer <admin-token>"
```

This atomically swaps the `speakers` alias back to `speakers_v3`.

---

## Manual Migration Commands (Advanced)

```bash
# Enter backend container
./opentr.sh shell backend

# Check current version
alembic current

# Show migration history
alembic history --verbose

# Manually upgrade to head
alembic upgrade head

# Manually downgrade one step
alembic downgrade -1

# Stamp without running migration (use with caution)
alembic stamp v355_add_diarization_settings
```

---

## Verification After Upgrade

After upgrading to v0.4.0, verify the migration succeeded:

```sql
-- Check Alembic version
SELECT * FROM alembic_version;
-- Should show: v355_add_diarization_settings

-- Verify new columns exist
SELECT column_name FROM information_schema.columns
WHERE table_name = 'media_file'
  AND column_name IN ('diarization_disabled', 'ai_summary_enabled', 'requested_whisper_model');

-- Verify diarization settings in user_settings
SELECT column_name FROM information_schema.columns
WHERE table_name = 'user_settings'
  AND column_name IN ('diarization_provider', 'min_speakers', 'max_speakers');
```

---

## Full Migration History

| Migration | Description | Version |
|-----------|-------------|---------|
| `v010_baseline` | v0.1.0 baseline schema | v0.1.0 |
| `v020_add_system_settings` | Global system settings table | v0.2.0 |
| `v030_add_ldap_auth` | LDAP authentication support | v0.2.0 |
| `v031_add_keycloak_pki_auth` | Keycloak and PKI auth support | v0.2.0 |
| `v040_add_fedramp_compliance` | FedRAMP compliance fields | v0.2.0 |
| `v050_add_search_settings` | Search configuration settings | v0.2.0 |
| `v060_add_transcript_overlap` | Speaker overlap detection | v0.3.0 |
| `v070_pki_security` | PKI security enhancements | v0.3.0 |
| `v071_add_transcript_segment_unique_constraint` | Transcript segment dedup | v0.3.0 |
| `v072_add_queued_downloading_statuses` | New file status values | v0.3.0 |
| `v073_convert_filestatus_enum_to_varchar` | FileStatus enum → varchar | v0.3.0 |
| `v080_add_auth_config` | Runtime auth configuration | v0.3.0 |
| `v090_add_error_category` | Error categorization | v0.3.0 |
| `v091_add_speaker_suggestion_source` | Speaker suggestion source tracking | v0.3.0 |
| `v100_optimize_query_performance` | Query indexes | v0.3.0 |
| `v110_add_missing_fk_indexes` | FK indexes | v0.3.0 |
| `v120_add_remaining_fk_indexes` | Remaining FK indexes | v0.3.0 |
| `v130_add_processing_model_tracking` | Model used per file | v0.3.0 |
| `v140_add_word_timestamps` | Word-level timestamp storage | v0.3.0 |
| `v150_add_file_retention_settings` | File retention policies | v0.3.0 |
| `v160_add_allow_local_fallback` | ASR local fallback flag | v0.3.0 |
| `v170_add_keycloak_refresh_token` | Keycloak refresh token | v0.3.0 |
| `v180_add_speaker_attributes` | Speaker gender/attribute fields | v0.3.0 |
| `v190_add_collection_default_prompt` | Per-collection prompts | v0.3.0 |
| `v200_schema_reconciliation` | Schema reconciliation | v0.3.0 |
| `v210_add_groups_and_sharing` | Groups and file sharing | v0.3.0 |
| `v211_add_sharing_constraints_and_indexes` | Sharing constraints | v0.3.0 |
| `v220_add_speaker_clusters` | Speaker cluster tables | v0.3.0 |
| `v230_add_auto_labeling` | Auto-labeling for clusters | v0.3.0 |
| `v250_add_speaker_clustering_indexes` | Speaker cluster indexes | v0.3.0 |
| `v260_add_cluster_quality_metrics` | Cluster quality scoring | v0.3.0 |
| `v270_add_asr_provider_support` | Cloud ASR provider configs | v0.3.0 |
| `v270_add_profile_avatar` | Speaker profile avatars | v0.3.0 |
| `v280_add_upload_sessions` | Chunked upload sessions | v0.3.0 |
| `v290_add_password_reset_tokens` | Password reset flow | v0.3.0 |
| `v300_add_gender_confirmed` | Gender-confirmed flag | v0.3.0 |
| `v310_add_speaker_constraints` | Speaker uniqueness constraints | v0.3.0 |
| `v320_add_cluster_suggested_name` | Cluster LLM-suggested name | v0.3.0 |
| `v330_add_shared_configs_and_prompts` | Shared configs, per-collection prompts | **v0.4.0** |
| `v340_add_user_media_sources` | User media source preferences | **v0.4.0** |
| `v350_add_diarization_disabled` | Per-file diarization disable | **v0.4.0** |
| `v351_add_ai_summary_settings` | AI summary enable/disable | **v0.4.0** |
| `v352_add_requested_whisper_model` | Per-file model override | **v0.4.0** |
| `v353_fix_segment_unique_index` | Segment index fix | **v0.4.0** |
| `v355_add_diarization_settings` | Per-user diarization settings | **v0.4.0** |

---

## Technical Reference

- **Config**: `backend/alembic.ini`
- **Migrations**: `backend/alembic/versions/`
- **Auto-migration code**: `backend/app/db/migrations.py`
- **Startup integration**: `backend/app/main.py` (lifespan handler)
