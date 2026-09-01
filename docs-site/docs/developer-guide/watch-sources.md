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
  `watch.max_imports_per_scan`, `watch.fs_events_enabled`, `watch.fs_events_mode`,
  `watch.fs_events_poll_seconds`. `watch.max_imports_per_scan` is a
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

1. **Same path** — the `(watch_source_id, remote_path)` unique constraint.
2. **Same content, any source** — match `watch_source_file.imohash` against every imported
   tracking row, recording `duplicate_same_source` or `duplicate_other_source` by where the
   match landed.
3. **Cross-pipeline** — `utils/file_hash.check_duplicate_by_imohash` against `media_file.imohash`
   (manual uploads, URL imports, prior watch imports).

:::warning[Fixed in v0.6.0]
Layer 2 filtered `watch_source_id != source.id`, excluding the source being scanned — so one
source holding the same recording under two names imported it twice, and
`SkipReason.DUPLICATE_SAME_SOURCE` was defined but produced by no code path. Regression:
`backend/tests/unit/test_watch_source_dedup.py`.
:::

:::note[Documents are not covered by layer 3]
`Document` carries a `file_hash` (an imohash, written by both ingest paths) that nothing reads,
so a watch-imported document is deduplicated against nothing outside its own source. Tracked in
[#546](https://github.com/attevon-llc/OpenTranscribe/issues/546) for the document-ingestion
branch — the column exists, so no migration is needed.
:::

`services/imohash_service.py` wraps the real **`imohash`** package (`hashfile` / `hashfileobject`)
plus a seekable MinIO ranged-read shim, so a fingerprint is computed from ~3 small windows
regardless of file size and is identical whether the file is local, a stream, or a MinIO object.

:::note[Breaking change]
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

:::warning[`retry_count` means two things — fixed in v0.6.0]
`_handle_group` uses `retry_count` as the **wait-scan counter**, while `_record_error`
increments the same column as a **failure count**. A file that had failed twice as a standalone
import and later joined a group therefore entered the wait already "aged": with the default
`multipart_wait_scans=3` it made `(waited + 1) >= wait_scans` true on the *first* grouping scan,
and an incomplete recording was stitched — silently truncated, then transcribed as if whole.

The counter now resets on entry into `waiting_for_parts` only, so an established wait still ages
and the missing-parts timeout still fires. Regression:
`backend/tests/unit/test_watch_multipart_wait_counter.py`. Any UI showing this column must label
it per row; `WatchSourceFilesTable` does.
:::

## Filesystem events (issue #294)

`watch.fs_events_enabled` used to be a setting with no consumer — the admin could enable it and
still wait a full `polling_interval_minutes` (15 by default). `services/watch_sources/fs_events/`
is the consumer. It is an **accelerator, not a mechanism**: it never imports anything, it only
makes the existing `watch_source.scan_single` fire sooner, and the every-minute `scan_all` poll
is untouched and remains the safety net.

### Where it runs

In **celery-beat**, started from Celery's `beat_init` signal (`core/celery.py`). Beat is the one
service that is single-instance by design, so exactly one observer set exists per deployment.
`docker-compose.watch.yml` therefore mounts the watch folder into `celery-beat` as well as the
backend and the download/cpu workers.

### Choosing an observer — and why `auto` is not just `Observer()`

The backend always runs in a **Linux container**, so watchdog will always offer
`InotifyObserver` for any path. That is exactly the trap: inotify is a kernel-local mechanism
and does not see

- host-side writes through a **macOS** Docker bind mount (VirtioFS / gRPC-FUSE),
- writes to a **Windows** drive under Docker Desktop/WSL2 (9p / drvfs),
- a **remote writer** on any network mount — NFS, SMB/CIFS, a NAS — on any host OS.

A naive observer would look correct on a Linux dev box and silently do nothing for everyone
else. `fs_events/detection.py` answers the question twice, cheapest first:

1. `classify_path` reads the filesystem type backing the directory from `/proc/self/mountinfo`
   and rejects the network/passthrough families outright.
2. `probe_delivery` is authoritative: with the observer already running, it creates a
   dot-prefixed `.opentranscribe-fsprobe-*` file in the watched directory and waits to be told
   about it. The file is deleted immediately (and any orphan from a crashed process is swept on
   the next start); the scanner's file-stability window means a scan can never see it. A
   directory that cannot be written to counts as "undecidable" → treated as unsupported.

Either negative answer falls back to watchdog's `PollingObserver`, which works on every
filesystem at the cost of a stat sweep every `watch.fs_events_poll_seconds` (15 s default).
`watch.fs_events_mode` overrides the policy globally: `auto` (default), `native`, `polling`, or
`off`.

### Debounce, locking, reconciliation

- **Debounce** — writing one large recording produces hundreds of events, so events fold into a
  per-source timer that fires once the source has been *quiet* for
  `file_stability_seconds + 5 s`. That margin is load-bearing: `LocalWatchClient.list_files`
  skips files younger than the stability window, so an earlier dispatch would scan and find
  nothing. A `max_defer` cap (5 min) still fires under continuous churn, and a cooldown floors
  the interval between event-driven dispatches.
- **Locking** — each dispatch is taken under the shared Redis task lock
  (`watch_source:fs_dispatch:{id}`), and `scan_single` keeps its own per-source lock, so no
  duplicate scans even with multiple supervisors.
- **Reconciliation** — the watched set is re-read from the database every 30 s (sources are
  added, edited, and disabled at runtime); a changed path/recursion/filter restarts that watch.
- **Failure** — every failure path publishes a status and degrades to polling. Nothing here can
  raise into the beat process.

### Surfacing the actual mode

The observer runs in beat while the API runs in the backend container, so the supervisor
publishes a small JSON blob per source to Redis (`watch_source:fs_status:{id}`) with a 120 s TTL
refreshed on every reconcile. `GET /watch-sources` reads them in one `MGET` and returns
`fs_events` on each source (`mode` = `native` / `polling` / `error` / `unavailable`, plus the
human-readable `detail`, `fs_type`, counters, and timestamps). A missing key means nothing is
watching, and the UI says "every N min" instead of guessing — which is also what happens on its
own if the beat container stops.

## Email

`services/watch_email_service.py` (kept separate from the password-reset `email_service.py`)
dispatches by provider: `smtplib` STARTTLS/SSL for SMTP and Exchange, and MSAL → Microsoft
Graph `sendMail` for M365. **Delivery is experimental** — verify against a real provider.

## API & frontend

- API: `api/endpoints/watch_sources.py` (CRUD, test, scan, paginated file history with status +
  filename filters, batch retry, batch delete, folder browse, capabilities, multipart-regex
  tester, email-config CRUD + test, per-source email links, admin global settings), registered
  in `api/router.py` under `/watch-sources`.
- Frontend: `lib/api/watchSourcesApi.ts`,
  `components/settings/{WatchSourcesSettings,WatchSourceModal,EmailConfigModal}.svelte`, and the
  children in `components/settings/watchSources/` (card, email-config list, global-settings
  form, files modal + table, email-links modal — see that folder's `CLAUDE.md`). The editor is a
  stepper; tags/collections use `svelte-multiselect`. Registered in
  `stores/settingsModalStore.ts` + `SettingsModal.svelte`; live scan refresh via a
  `watch_source_scan` WebSocket event.

### Retry is batch-shaped, and that is load-bearing

`POST /{uuid}/files/retry` takes a list of uuids even for one row. `scan_single` holds a Redis
lock per source, so a per-file endpoint would dispatch one scan per file and **every one after
the first would silently no-op**. One reset pass plus one dispatch is both correct and what
"retry all failed" needs.

The handler resets each eligible row to `pending` and clears `skip_reason`/`error_message` —
that is what makes a *terminal* row importable again, since `_get_or_create_tracking_row`
refuses to reuse a terminal row. It does **not** touch `retry_count`: `_record_error` already
counts attempts, and incrementing here would double-count one.

Refused states, each preventing a different concrete harm: `imported` (would duplicate),
`importing`/`downloading` (races `_claim_import`), `waiting_for_parts` (`retry_count` there is
the multipart wait counter, not an attempt count), `stitched_part` (already folded into a
stitched recording). A disabled source is a 409 for the whole request, since `_load_scan_plan`
would refuse the scan anyway.

The set lives in `schemas/watch_source.RETRYABLE_FILE_STATUSES` and is mirrored in
`lib/api/watchSourcesApi.RETRYABLE_FILE_STATUSES`, so the UI hides a button the API would
refuse.

### The email-link tiers are asymmetric on purpose

Creating and editing an `EmailNotificationConfig` is **super_admin** — it holds mailbox
credentials. Linking an existing config to a source is **owner-level**. That asymmetry made the
per-source panel unbuildable until v0.6.0: an owner could `POST /{uuid}/emails` but
`GET /email-configs` is super_admin, so there was no way to discover what to link.

`GET /{uuid}/emails/available` closes it, source-scoped rather than as a second listing under
`/email-configs` — that prefix is in `SUPER_ADMIN_PREFIXES`, and a non-super_admin route beneath
it fails `tests/unit/test_route_privilege_tiers.py`, correctly. `EmailConfigOption` is a minimal
projection (`uuid`, `name`, `provider`, `is_enabled`, `has_default_recipients`) and its key set
is asserted **exactly**, so widening it cannot quietly expose the deployment's mail hostnames to
every authenticated user.

`EmailLinkResponse` carries `config_is_enabled` / `config_has_default_recipients` for the same
reason the picker cannot supply them: the picker excludes configs already linked, so those two
facts would be unavailable for exactly the rows whose warnings need them.

## Testing

- Unit (GPU-free, host pytest): `backend/tests/unit/test_watch_multipart_detection.py`,
  `test_imohash_package_parity.py`, `test_watch_cross_pipeline_dedup.py`,
  `test_watch_fs_event_detection.py` (mount classification, probe, event filtering, debounce),
  `test_watch_fs_event_supervisor.py` (mode selection/fallback, reconciliation, and the
  "a broken observer degrades to polling instead of raising" invariant).
- UI E2E (Playwright): `backend/tests/e2e/test_watch_sources_e2e.py` (panel, stepper,
  create→list→delete).
- Full live E2E incl. real GPU transcription: `./scripts/test-watch-e2e.sh` runs
  `backend/scripts/e2e_watch_sources.py` inside the backend container (the host harness sets
  `SKIP_CELERY/REDIS/S3`). It synthesizes speech with ffmpeg's `flite` filter, imports it, waits
  for the `MediaFile` to reach `COMPLETED`, and asserts the transcript contains the spoken
  words — plus beat due-logic, dedup, S3 (self-seeded MinIO bucket), and multi-part stitching.

## Infrastructure

`docker-compose.watch.yml` mounts `WATCH_HOST_PATH` into the backend + download + cpu workers
+ celery-beat (the FS-event observer);
`docker-compose.smb-test.yml` is a Samba test share. `opentr.sh` adds `--with-watch` and
`--with-smb-test`. Seed data: `scripts/setup-watch-source-test-data.sh`.
