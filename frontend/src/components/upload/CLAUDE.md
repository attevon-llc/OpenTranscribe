# frontend/src/components/upload

## Purpose

Thin presentational children of `FileUploader.svelte` (the coordinator), split out of that file:
the three media-source panels (file / URL / record) and the wizard steps of the upload stepper.

## Key files

- `MediaFilePanel.svelte` — drop zone + selected-file card. **Its type/size guards are advisory
  only**: every branch dispatches `fileSelect` regardless, and the parent decides and renders the
  error. Owns `#drop-zone` (its own drag listeners attach by `getElementById`).
- `MediaUrlPanel.svelte` — URL field, protected-media credentials, download-quality overrides.
  Exposes `getUrlPayloadExtras()` / `resetState()` as **component methods** (the parent holds a
  `urlPanelRef`); only values differing from the user's saved download settings are sent.
- `MediaRecordPanel.svelte` — in-browser recorder; state lives in `$stores/recording`, not in props.
- `UploadStepExtraction.svelte` — large-video "extract audio vs upload full" choice. The parent
  splices this step into/out of the `steps` array when a video crosses `extraction_threshold_mb`.
- `UploadStepTags.svelte` / `UploadStepCollections.svelte` — the optional/skippable steps; both
  import `upload-shared.css` (unscoped, so `.step-hint`/`.previous-banner` are global). Collections
  is the one child that calls the API itself (`POST /collections`).
- `UploadStepSpeakers.svelte` — min/max/num speaker inputs; clamps to ≥1 and flags `min > max`.
- `UploadStepModel.svelte`, `UploadStepReview.svelte` — model/summary toggles and final summary.

## Conventions / patterns

- The parent owns the step array, tab state, validation, `localStorage` previous-values
  (`opentr:uploadPreviousValues`), and submit; children take props + `createEventDispatcher`.
- **Nothing here shows upload progress.** Submit hands off to `uploadsStore.addFile/addFiles/
addRecording` → `$lib/services/uploadService`; the progress UI is `UploadManager.svelte`.

## How it connects

- Parent: `$components/FileUploader.svelte` (imports these as `./upload/...`). Stores:
  `$stores/uploads`, `$stores/recording`, `$stores/network` (offline banner), `$stores/toast`.
- Upload path: `POST /files/prepare` (`use_presigned: true`) → direct `PUT` to the presigned MinIO
  URL → `POST /files/complete`, falling back to legacy multipart `POST /files` if no `upload_url`
  comes back or the PUT throws. No chunking; progress is axios `onUploadProgress`, capped at 99%.

## Gotchas

- **E2E-guarded selectors owned here** (`backend/tests/e2e/test_upload.py`): `#drop-zone` (and
  `#drop-zone input[type=file]`), `.selected-file`, `.file-name`, `.file-remove` in
  `MediaFilePanel`; `#media-url` in `MediaUrlPanel`; `#min-speakers` / `#max-speakers` in
  `UploadStepSpeakers`. The same suite guards the parent's `.uploader-container`,
  `.step-indicator`, `.tab-navigation .tab-button`, `.message.error-msg`, and
  `.nav-btn.nav-next` / `.nav-submit` / `.nav-review-defaults`.
- **Size limits live in `$lib/utils/uploadLimits.ts` — never re-declare one in a component.**
  The hard reject is `getMaxUploadBytes()` — the LIVE, admin-configurable value from
  `GET /system/capabilities` (`settings.MAX_UPLOAD_BYTES` on the backend, `$stores/capabilities`
  on the frontend), not a hardcoded literal (issue G10: a hardcoded 15 GB silently went stale
  the moment an admin changed the env var). `DEFAULT_MAX_UPLOAD_BYTES` (15 GB, matching the
  backend's coded default) is only the fallback until that fetch resolves, or if it fails —
  never read as "no limit". `getMaxUploadBytes()` returns `null` when the admin has explicitly
  disabled the limit server-side (`MAX_UPLOAD_BYTES=0`). `LARGE_UPLOAD_WARNING_BYTES` (2 GB, NOT
  admin-configurable) is a warning on _every_ path, never a reject on any. Both the single-file
  and multi-file paths in `FileUploader` use `exceedsUploadLimit`/`warrantsLargeUploadWarning`.
  Until #298 there were three copies of the hard limit with two different values, so the same
  5 GB file uploaded when dropped alone and was rejected as "too large" when dropped alongside
  another. `MediaFilePanel` still validates nothing by design — it normalizes the MIME type and
  dispatches `fileSelect`; the parent owns rejection and error rendering.
- Every string in these steps is keyed (#284 A3.1 fixed the last hardcoded English in
  `UploadStepExtraction`, `UploadStepModel`, `UploadStepReview`, `UploadStepTags`,
  `UploadStepCollections` and `UploadStepSpeakers`). Keep it that way — only the active locale is
  loaded at runtime, so a bare English literal here is a live regression for 7 of 8 languages.
  New copy needs a key in all 8 locale files (`npm run check:i18n` gates it).
