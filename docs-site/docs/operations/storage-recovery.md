---
sidebar_position: 4
title: Storage Recovery
description: Rebuild the database from surviving MinIO media when the database is lost but the object storage is intact
---

# Storage Recovery

OpenTranscribe keeps your **media** in object storage (MinIO) and all the
**derived data** — transcripts, segments, speakers, search indices — in
PostgreSQL and OpenSearch. If you ever lose the database but the media in MinIO
survives, you can **rebuild the catalog in place** without re-downloading or
re-uploading a single file.

:::info[When you'd use this]
- A database volume was deleted, corrupted, or restored from a stale backup, but the MinIO bucket is intact.
- You're migrating to a fresh database and want to re-attach existing media.
- Restoring a Postgres backup is always the **first** choice (it preserves transcripts, speakers, and edits). Storage recovery is for when no good database backup exists — it re-derives everything by reprocessing the media.
:::

## How it works

The re-ingestion tool scans the MinIO media prefix (`media/<user_id>/...`) and,
for every object that has **no** corresponding `MediaFile` row, creates one
whose `storage_path` points at the **existing object key**. Nothing is copied
or moved — the new row simply re-attaches to the bytes already in the bucket.
Each recovered file then flows through the normal processing pipeline
(transcription, diarization, indexing), so the transcripts and search data are
rebuilt from the source media.

It is **idempotent**: objects already referenced by a row are skipped, so the
command is safe to run more than once (for example, to pick up files added
since the last run).

## Running it

Run inside the backend container so it reaches the configured MinIO and
PostgreSQL:

```bash
./opentr.sh shell backend
# then, in the container:
python -m app.scripts.reingest_minio [--dry-run] [--limit N] \
    [--user-email admin@example.com] [--no-dispatch] [--throttle N]
```

| Flag | Effect |
|------|--------|
| `--dry-run` | Report what would be registered; create no rows, dispatch nothing. Always start here. |
| `--limit N` | Register at most `N` new files (useful for a staged recovery). |
| `--user-email` | Owner for the recovered files (default `admin@example.com`). |
| `--no-dispatch` | Register + fingerprint only; don't fire the processing pipeline (you can reprocess later from the UI). |
| `--throttle N` | Sleep `N` seconds between dispatches. Default `0` — the single GPU worker serializes the transcription queue anyway, so a throttle only paces the CPU-side thumbnail/preprocess fan-out. |

A typical recovery:

```bash
# 1. See what would happen
python -m app.scripts.reingest_minio --dry-run

# 2. Recover a small batch first to confirm the pipeline is healthy
python -m app.scripts.reingest_minio --limit 5

# 3. Recover everything
python -m app.scripts.reingest_minio
```

Recovered files get a real content fingerprint (imohash), so the normal
duplicate-detection layers keep working and a later manual upload of the same
media won't create a second copy.

## YouTube metadata recovery (optional)

For media originally imported from YouTube, the surviving
`user_<id>/youtube_<VIDEO_ID>/` thumbnail prefixes preserve the original
video IDs even though the title/description linkage lived in the lost database.
Two Celery tasks help recover what they can **without re-downloading any
video** (so there's no risk of rate-limiting or IP blocks):

- `recovery.youtube_metadata_fetch` — fetches title/duration per surviving
  video ID via metadata-only requests (rate-limited, ~1 every 5 s, resumable
  via a sidecar object in MinIO).
- `recovery.youtube_metadata_backfill` — re-attaches titles to recovered files
  by matching on duration (±2 s, unique-match-only in both directions, so an
  ambiguous match is left untitled rather than mistitled).

:::note[Honest expectation]
Durations collide on a large library, so duration-matching auto-titles only the
files with distinctive lengths. Everything else is recovered and processed with
its object-key filename as a placeholder title — lossless, just unlabelled.
:::

## Preventing the loss in the first place

Storage recovery is a safety net, not a substitute for backups. Pair it with:

- **[Scheduled database backups](./backup-restore.md#automated-backups-in-app-recommended)** — the in-app, no-cron backup system (local folder or off-host S3 bucket).
- **[Fresh / isolated deployments](./fresh-deployments.md)** — never experiment against live data; `./opentr.sh data-paths` shows exactly which host paths hold live data, and live-data marker files guard the bind mounts against accidental cleanup.
