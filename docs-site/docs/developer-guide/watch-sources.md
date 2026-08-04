---
sidebar_position: 5
---

# Watch Sources (Developer Guide)

Architecture and internals of the Watch Sources auto-import feature (issue #26). For usage see
the [user guide](../user-guide/watch-sources.md).

## Data model

Migration `v366_add_watch_sources` creates four tables:

- **`watch_source`** — one row per configured source, discriminated by `source_type`
  (`local` / `s3` / `smb`). Holds per-type connection fields, processing options
  (polling interval, age skip, extensions, recursive, auto-transcribe, min/max speakers,
  collections, tags), multi-part settings, and last-scan status columns. Secrets are stored
  AES-256-GCM encrypted (`encrypted_s3_secret_key`, `encrypted_smb_password`).
- **`watch_source_file`** — every file the scanner has seen, with its content fingerprint
  (`imohash`), import `status`, `skip_reason`, multi-part `part_group`/`part_number`, and a
  link to the created `media_file`. Unique on `(watch_source_id, remote_path)` — the within-source
  dedup key and idempotency guarantee.
- **`email_notification_config`** — reusable, admin-managed SMTP / M365 / Exchange config
  (encrypted secrets).
- **`watch_source_email`** — junction linking a source to email configs with per-link recipients
  and success/error toggles.

Models live in `backend/app/models/{watch_source,email_notification_config}.py`; Pydantic
schemas in `backend/app/schemas/{watch_source,email_notification}.py`.

## Configuration: DB-backed, no restart

The **only** environment variables are the physical mount paths (`WATCH_HOST_PATH` → container
`WATCH_FOLDER_PATH`, and `WATCH_TEMP_DIR`). Everything else is data:

- Per-source connection, credentials, and schedule live on the `watch_source` row (the UI).
- Global tuning knobs are `SystemSettings` keys read through
  `services/watch_settings_service.py` with coded defaults (`DEFAULT_WATCH_*` in
  `core/constants.py`): `watch.enabled`, `watch.file_stability_seconds`,
  `watch.max_imports_per_scan`, `watch.fs_events_enabled`. `watch.max_imports_per_scan` is a
  **per-scan cap, not a concurrency limit** — imports run serially inline, so raising it
  lengthens a single scan rather than parallelizing it. (Renamed from
  `watch.max_concurrent_imports`, which promised parallelism the code never implemented; the
  old key is still read as a fallback so existing deployments keep their configured value.)

This means an operator reconfigures everything from the admin UI; changes apply on the next
scan with no redeploy.

## Client abstraction

`services/watch_sources/base.py` defines `BaseWatchSourceClient` (`test_connection`,
`list_files`, `download_file`, `upload_file`, `close`) and a `create_client(source)` factory.
Implementations:

- `local_client.py` — `Path.rglob` with symlink/`..` traversal guards and a file-stability
  skip; downloads are no-ops (read in place).
- `s3_client.py` — `boto3`, paginated `list_objects_v2`, ranged/resumable downloads.
- `smb_client.py` — `smbprotocol` (`smbclient.walk` / `open_file`), chunked transfer.

Adding a new source type is purely additive: implement the interface and extend the factory.

## Deduplication & imohash

Dedup is three layers, all on the imohash content fingerprint:

1. **Within source** — the `(watch_source_id, remote_path)` unique constraint.
2. **Across sources** — match `watch_source_file.imohash` in other sources.
3. **Cross-pipeline** — `utils/file_hash.check_duplicate_by_imohash` against `media_file.imohash`
   (manual uploads, URL imports, prior watch imports).

`services/imohash_service.py` wraps the real **`imohash`** package (`hashfile` / `hashfileobject`)
plus a seekable MinIO ranged-read shim, so a fingerprint is computed from ~3 small windows
regardless of file size and is identical whether the file is local, a stream, or a MinIO object.

:::note Breaking change
Switching from the previous hand-rolled blake2b stand-in to the `imohash` package changes every
existing `media_file.imohash`. `tasks/imohash_recompute.py` runs once on first startup (gated by
the `imohash_package_recompute_complete` system-settings flag, dispatched from `main.py` lifespan)
to regenerate all rows; an admin button can re-trigger it.
:::

## Import pipeline

`services/watch_sources/processing.py:import_single_file` materializes the file (no-op for
local), then `ingest_prepared_file` runs: magic-byte validate → imohash → 3-layer dedup → MinIO
upload (`user_{id}/file_{id}/{name}`) → create `MediaFile` (owned by the source's user) →
collections/tags → commit → `file_created` WS event → `dispatch_upload_pipeline` (the *same*
post-upload tail manual uploads use: thumbnail + waveform + transcription chain). This reuse is
deliberate — watch imports and manual uploads share one ingest path.

## Tasks & scheduling

`tasks/watch_source_tasks.py`, wired into `core/celery.py` (`include`, `task_routes`,
`beat_schedule`):

- `watch_source.scan_all` — beat, every minute (utility queue). Dispatches `scan_single` only
  for enabled sources whose `polling_interval_minutes` is due. Guarded by a Redis lock.
- `watch_source.scan_single` — download queue. Lists, records age-skips, imports standalone
  files inline (bounded by `max_imports_per_scan`), dispatches complete multi-part groups to
  `stitch_and_import`, updates scan status, and fires `send_notification` when email is linked.
  Per-source Redis lock.
- `watch_source.stitch_and_import` — cpu queue. Downloads parts, `multipart.stitch_files`
  (ffmpeg stream-copy when codecs match, else re-encode), ingests the result, marks parts
  consumed.
- `watch_source.send_notification` (utility) and `watch_source.cleanup_temp` (utility, hourly).

Multi-part detection (`services/watch_sources/multipart.py`) groups files by a configurable
regex (default `^(.+?)_P(\d{3})(\.[^.]+)$`) within a time window; incomplete groups wait a
bounded number of scans before stitching what arrived.

## Email

`services/watch_email_service.py` (kept separate from the password-reset `email_service.py`)
dispatches by provider: `smtplib` STARTTLS/SSL for SMTP and Exchange, and MSAL → Microsoft
Graph `sendMail` for M365. **Delivery is experimental** — verify against a real provider.

## API & frontend

- API: `api/endpoints/watch_sources.py` (CRUD, test, scan, paginated file history, folder
  browse, capabilities, multipart-regex tester, email-config CRUD + test, admin global
  settings), registered in `api/router.py` under `/watch-sources`.
- Frontend: `lib/api/watchSourcesApi.ts` and
  `components/settings/{WatchSourcesSettings,WatchSourceModal,EmailConfigModal}.svelte`. The
  editor is a stepper; tags/collections use `svelte-multiselect`. Registered in
  `stores/settingsModalStore.ts` + `SettingsModal.svelte`; live scan refresh via a
  `watch_source_scan` WebSocket event.

## Testing

- Unit (GPU-free, host pytest): `backend/tests/unit/test_watch_multipart_detection.py`,
  `test_imohash_package_parity.py`, `test_watch_cross_pipeline_dedup.py`.
- UI E2E (Playwright): `backend/tests/e2e/test_watch_sources_e2e.py` (panel, stepper,
  create→list→delete).
- Full live E2E incl. real GPU transcription: `./scripts/test-watch-e2e.sh` runs
  `backend/scripts/e2e_watch_sources.py` inside the backend container (the host harness sets
  `SKIP_CELERY/REDIS/S3`). It synthesizes speech with ffmpeg's `flite` filter, imports it, waits
  for the `MediaFile` to reach `COMPLETED`, and asserts the transcript contains the spoken
  words — plus beat due-logic, dedup, S3 (self-seeded MinIO bucket), and multi-part stitching.

## Infrastructure

`docker-compose.watch.yml` mounts `WATCH_HOST_PATH` into the backend + download + cpu workers;
`docker-compose.smb-test.yml` is a Samba test share. `opentr.sh` adds `--with-watch` and
`--with-smb-test`. Seed data: `scripts/setup-watch-source-test-data.sh`.
