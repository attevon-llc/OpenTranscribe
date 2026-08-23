# frontend/src/components/settings/watchSources

## Purpose

The presentational children split out of `../WatchSourcesSettings.svelte` (was 767 lines),
plus the two modals that make the watch-source backend fully reachable: per-file management
(#489) and per-source email notifications (#490).

## Key files

- `../WatchSourcesSettings.svelte` — **the coordinator, and it stays outside this folder.**
  That is the repo precedent (`UserFileStatus.svelte` + `components/fileStatus/`,
  `FileUploader.svelte` + `components/upload/`) and it keeps the settings-panel import
  convention (`$components/settings/...`) and `SettingsModal.svelte`'s import unchanged.
  It owns every API call and all source-of-truth state.
- `WatchSourceCard.svelte` — one source row. Dispatches
  `toggle`/`test`/`scan`/`edit`/`delete`/`files`/`notifications`.
- `EmailConfigList.svelte` — the deployment-wide mailers (super_admin).
- `GlobalWatchSettingsForm.svelte` — global watch settings (super_admin); `settings` is bound.
- `WatchSourceFilesModal.svelte` + `WatchSourceFilesTable.svelte` — #489.
- `WatchSourceEmailLinksModal.svelte` — #490.

## Conventions / patterns

- Children fetch nothing. Props in, `createEventDispatcher` out — except the two modals,
  which are **feature coordinators**: they own their own fetch/paging/filters because that
  state belongs to the modal's lifetime, not the panel's.
- Import via `$components/...`, `$lib/...`, `$stores/...`; `$t` for every user-visible string.
- Light/dark parity via CSS custom properties; never a hardcoded hex.

## Gotchas

- **`.source-card` is E2E-guarded** (`backend/tests/e2e/test_watch_sources_e2e.py`). Renaming
  it breaks Playwright.
- **The two email tiers are different rights and different gates.** Managing a _config_
  (`EmailConfigList`) is **super_admin** — it holds mailbox credentials. _Linking_ a config to
  your own source (`WatchSourceEmailLinksModal`) is **owner-level**, so that modal must NOT be
  gated on `isSuperAdmin`, and its picker reads the source-scoped
  `GET /{uuid}/emails/available` rather than the super_admin `GET /email-configs`.
- **Retry queues; it does not import.** The backend resets the rows and dispatches one scan,
  which may find another already running (a Redis lock per source), may not reach the file
  within `max_imports_per_scan` (default 5), and only re-imports files still present at their
  remote path. Copy says "queued", the row shows `pending`, and the modal listens for the
  `watch-source-scan` window event so the real outcome replaces it. Do not reword this to
  claim the file was retried.
- **`retry_count` means two things.** Failed import ATTEMPTS on an ordinary row, SCANS WAITED
  on a `waiting_for_parts` row (the multipart stitch timer). `WatchSourceFilesTable` labels it
  per row; one heading for both misreports one of them.
- **An unknown status must render its raw value, never blank.** Deployments carry statuses
  this UI does not enumerate (`skipped_too_large` is written by the document ingest path and
  is not an enum member yet — #547), and a blank cell reads as missing data.
- **Retry is offered only for `RETRYABLE_FILE_STATUSES`** (`$lib/api/watchSourcesApi`), which
  mirrors the backend set. Widening it here just surfaces a button the API refuses.
- The delete-record copy deliberately stops at "the next scan will re-check it" and does not
  promise a duplicate skip: that is true for media and false for documents until #546 lands.

## How it connects

Mounted by `../WatchSourcesSettings.svelte`, which `SettingsModal.svelte` routes to.
Backend: `backend/app/api/endpoints/watch_sources.py`.
