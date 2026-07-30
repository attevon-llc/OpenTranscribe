# app/services/watch_sources — auto-import from local / S3 / SMB (issue #26)

## Purpose

Poll a local mounted folder, an S3-compatible bucket (boto3), or an SMB/CIFS share
(smbprotocol) and auto-import + transcribe new media through the same tail a manual upload
uses. Run with `./opentr.sh start dev --with-watch` (mounts `WATCH_HOST_PATH`, default
`./watch`, at `/watch`; sets container `WATCH_FOLDER_PATH=/watch`); `--with-smb-test` starts a
Samba test share. Seed data: `bash scripts/setup-watch-source-test-data.sh ./watch`.

## Key files

- `base.py` — `BaseWatchSourceClient` ABC (`test_connection`/`list_files`/`download_file`/
  `upload_file`) — adding a source type is purely additive, tasks talk only to this. Plus
  `RemoteFileInfo`, `parse_extensions`, and `create_client(source)`, the **only** place stored
  secrets are decrypted (`utils.encryption.decrypt_api_key`).
- `local_client.py` / `s3_client.py` / `smb_client.py` — the three backends.
- `processing.py` — `import_single_file` (materialize + delegate) and `ingest_prepared_file`
  (validate → fingerprint → dedup → MinIO → `MediaFile` → collections/tags → dispatch).
- `multipart.py` — `parse_part` / `detect_groups` / `stitch_files` (ffmpeg concat).
- `folder_browser.py` — backs the UI local-folder picker.

## Conventions / patterns

- **DB-backed, no-restart config.** The only env vars are the physical mount
  (`WATCH_FOLDER_PATH`, `WATCH_TEMP_DIR`). Per-source connection/credentials/schedule live on the
  `watch_source` row; the four global knobs (`watch.enabled`, `watch.file_stability_seconds`,
  `watch.max_concurrent_imports`, `watch.fs_events_enabled`) are `SystemSettings` read via
  `services/watch_settings_service.py`, defaults `DEFAULT_WATCH_*` in `core/constants.py`.
  Secrets are AES-256-GCM (`encrypted_s3_secret_key`, `encrypted_smb_password`), never returned.
- **Dedup is 3-layer on the imohash fingerprint**: within-source `remote_path` tracking row →
  cross-source `WatchSourceFile.imohash` → cross-pipeline `check_duplicate_by_imohash` against
  `media_file.imohash`. Fingerprints come from `services/imohash_service.py` (the real `imohash`
  package; the one-time `tasks/imohash_recompute.py` backfill, gated by
  `imohash_package_recompute_complete`, regenerated all rows — **breaking** vs the old blake2b).
- One bad file never aborts a scan: `import_single_file` catches, rolls back, then writes the
  error status in a **fresh session** (`_record_error`) so the rollback can't eat it.
- Local paths are validated with `os.path.realpath` against `WatchSource.resolved_local_path`;
  symlinks are never followed.

## How it connects

- `tasks/watch_source_tasks.py`: beat `watch_source.scan_all` (every minute, utility queue,
  due-check vs `polling_interval_minutes`) → `watch_source.scan_single` (**download** queue,
  per-source lock) → `watch_source.stitch_and_import` (**cpu** queue) /
  `watch_source.send_notification` + hourly `watch_source.cleanup_temp` (utility).
- Import tail is `api/endpoints/files/upload.py::dispatch_upload_pipeline` — identical to manual
  upload. Gallery gets `file_created`; scans emit `watch_source_scan` over WS.
- `models/`, `schemas/`, `api/endpoints/watch_sources.py`; migration `v366_add_watch_sources`
  (4 tables). Frontend: `components/settings/{WatchSourcesSettings,WatchSourceModal,EmailConfigModal}.svelte`
  (stepper modal; `svelte-multiselect` for tags/collections), `lib/api/watchSourcesApi.ts`.
- `services/watch_email_service.py` (SMTP / M365 Graph / Exchange) is **separate** from the
  password-reset `email_service.py`; delivery is **experimental**, unverified against a live provider.
- Tests: `tests/unit/test_watch_multipart_detection.py`, `tests/api/test_watch_sources_endpoints.py`,
  `tests/e2e/test_watch_sources_e2e.py`; live in-container run `./scripts/test-watch-e2e.sh`.

## Gotchas

- **The stability check is local-only.** `LocalWatchClient.list_files` skips files modified within
  `watch.file_stability_seconds` (30 s) as "still writing"; S3 and SMB have no equivalent. Symptom:
  a just-landed local file misses the first scan.
- **`watch.max_concurrent_imports` is not concurrency** — it's a per-scan cap
  (`standalone[:max_imports]` in `_perform_scan`) and imports run **serially inline** inside
  `scan_single`. Raising it lengthens one task; it does not parallelize.
- **`min_modified` is never passed by the scanner.** `_perform_scan` calls `list_files` without it
  and age-filters in Python so each too-old file gets a persisted `skipped_old` row. Don't "optimize"
  it into the client call — you'd lose the user-visible skip record.
- **`smb_client.download_file`'s size verification is dead code**: the `raise RuntimeError(...)` sits
  inside the `try` whose `except Exception` only logs at debug, so truncated downloads pass silently.
- **`watch.fs_events_enabled` has no consumer.** `watchdog` is a declared dependency and the flag is
  settable from the admin API, but no Observer exists in `app/` — **polling is the only mechanism**.
- Terminal `WatchSourceFile` statuses (`imported`, `skipped_*`, `stitched_part`) permanently exclude
  that `remote_path`. Fixing a rejected file in place will NOT re-import it.
- `delete_after_import` applies to `local` only; remote originals are never deleted.
  `LocalWatchClient.download_file` is a no-op returning the size (read in place) — which is why
  `stitch_and_import`'s temp reaper only unlinks paths under `watch_temp_dir`.
- Multipart: a lone `_P###` part imports standalone; a group spanning more than
  `multipart_time_window_hours` splits back to standalone; an **incomplete** group still stitches
  once `multipart_wait_scans` elapse. ffmpeg stream-copies only when ffprobe signatures match,
  else it re-encodes to H.264/AAC.
- `organization_id` is copied from the `watch_source` row (captured at creation), never inferred
  from the owner's memberships — keep it that way (issue #262c).
