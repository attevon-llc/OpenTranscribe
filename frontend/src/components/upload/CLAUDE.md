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
- **Three copies of the size limit, and they disagree**: `MediaFilePanel`'s `MAX_FILE_SIZE`
  (15 GB) is dead — every branch dispatches `fileSelect` regardless, deliberately letting the
  parent render the error. `FileUploader` enforces 15 GB on the single-file path (extra warning
  above 2 GB), but `handleMultipleFiles` rejects at its own 2 GB `FILE_SIZE_LIMIT`. **The same
  5 GB file uploads when dropped alone and is rejected when dropped alongside another** — the
  reject is reported via a `skippedInvalidFiles` toast, not silent. Change all three together.
- Hardcoded English survives in `UploadStepExtraction` ("Recommended", the choice descriptions),
  `UploadStepModel` ("AI Summary"), and `UploadStepReview` ("Processing") — bugs, not the pattern.
