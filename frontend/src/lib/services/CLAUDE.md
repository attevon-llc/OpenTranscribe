# src/lib/services — stateful singletons

## Purpose

Long-lived objects that own state a component must not own: an upload queue, a WASM
runtime, a Web Worker, a TTL cache. Import via `$lib/services/...`.

## The layering rule

- `$lib/utils` — pure functions, no state.
- `$lib/api` — stateless typed wrappers around one endpoint family (one call → one response).
- `$lib/services` — **stateful**: lifecycle, queue, worker, or cache that must survive across
  components and route changes.

If a new module has no state and no lifecycle it belongs in `api/` or `utils/`, not here —
unless it is a step of the upload flow rather than a presentational helper (`stallWatchdog.ts`,
`fileFingerprint.ts`).
Every module here exports **one shared instance** (`export const xService = …`); never construct
a second one in a component. (`AudioExtractionService` is also exported as a class — for tests only.
`stallWatchdog.ts` is the other exception: a per-request factory, not a singleton.)

## Key files

- `uploadService.ts` — the upload queue singleton: max 3 concurrent, 3 retries with exponential
  backoff, persisted to `localStorage['upload_queue']`. Tries a **presigned PUT
  straight to MinIO, then silently falls back** to the legacy multipart `POST /files`.
- `multipartUploader.ts` — `uploadInParts()`: executes the presigned **multipart** plan the
  backend returns for large objects (issue #327). A per-upload factory, not a singleton, like
  `stallWatchdog`. See gotchas.
- `stallWatchdog.ts` — `createStallWatchdog()`: the timeout control for file bodies. See gotchas.
- `audioExtractionService.ts` — in-browser video→audio via FFmpeg.wasm (`-c:a copy`, no re-encode),
  core loaded from `frontend/static/ffmpeg/` (gitignored, ~2.3 MB — compiled by the
  `build-ffmpeg.js` prebuild step via `frontend/ffmpeg-wasm-build/`, so a clean checkout still
  ships it). That core is a minimal, self-compiled, **LGPL-2.1+-only** build (issue #473) — zero
  external codec libraries, zero `--enable-gpl`/`--enable-nonfree`, only the demuxers/muxers this
  service's two commands need — replacing a prior CDN fetch of the generic `@ffmpeg/core`, which
  turned out to be GPL 2+ (built with `--enable-gpl --enable-libx264` etc), not just
  license-ambiguous. The metadata-read command carries `-vn` for this reason: the minimal core
  has no video codec parsers compiled in, and the `null` muxer needs a parser for every stream it
  copies (video included) even though it discards the output — video stream info was never
  parsed by this service anyway. `Dockerfile.prod` compiles the same core as its own build stage
  (kept in sync by hand — see the comment in each Dockerfile), so prod images ship fully
  self-contained. Extractions run **one at a time** through an internal queue.
  **Import it with a dynamic `import()`** (see `FileUploader.svelte`) — a static import drags the
  `@ffmpeg/*` wrapper into whichever route chunk references it, and extraction is an opt-in path.
- `fileFingerprint.ts` — `fingerprintFile()`: the client-side **imohash**, byte-identical to the
  backend's `services/imohash_service.py`. Stateless, like `stallWatchdog.ts`, and here for the
  same reason: it is part of the upload flow, not a presentational helper. See gotchas.
- `llmService.ts` — TTL-cached `/llm/status` (60 s when available, 10 s after a failure).
  `$stores/llmStatus` wraps it — components should read the store, not the service.
- `configService.ts` — once-only module-level cache of `/system/config/protected-media-auth`.

## How it connects

- `$stores/uploads` subscribes to `uploadService.addEventListener` and re-exports derived stores;
  components talk to the store, not the service.
- `audioExtractionService` pushes progress into `$stores/websocket.addNotification`, so client-side
  extraction appears in the same notification panel as backend jobs.
- Anything caching per-user state must be reset on logout via `$lib/session/clearUserState.ts`
  (`uploadService.reset()` is wired in through `uploadsStore.reset()`).

## Gotchas

- These are `.ts` files: use `get(t)(...)` / `get(store)`. `$store` syntax is `.svelte`-only.
- **One fingerprint: `fingerprintFile` from `$lib/services/fileFingerprint`.** Never call
  `file.arrayBuffer()` on a user-selected file — it materialises the whole thing in memory and Chrome
  throws `NotReadableError` above ~4 GB, against a UI that advertises 15 GB. That is exactly what the
  deleted `sha256Hasher.ts` / `sha256Worker.ts` did, and because the hash was optional and its error
  swallowed, the largest uploads silently skipped duplicate detection entirely (#342). `fingerprintFile`
  reads a bounded 48 KiB (head + middle + tail, 16 KiB each) whatever the file size, so no Worker is
  needed and memory never tracks file size.
- **The client fingerprint IS the backend's fingerprint.** `fileFingerprint.ts` implements imohash with
  the same parameters as `backend/app/services/imohash_service.py` (16 KiB windows, 128 KiB threshold)
  and `fileFingerprint.test.ts` pins vectors generated by the Python package. Change one side and you
  must change the other, or client and server stop agreeing on what "same file" means. It is a
  **sampling** fingerprint — not collision-resistant, never use it for security-sensitive equality.
- **A fingerprint that cannot be computed must be visible.** `fingerprintFile` throws `FingerprintError`;
  `uploadService.fingerprintOrWarn` sets `UploadItem.dedupSkipped`, warns to the console and raises a
  toast, and `UploadProgress.svelte` badges the row. Do not restore the empty `catch` — silence was the
  bug, not the missing hash.
- Extracted audio deliberately fingerprints the **source video**, not the audio blob it uploads
  (`ExtractedAudioMetadata.originalFingerprint`): ffmpeg does not produce byte-identical audio twice,
  so fingerprinting the blob would never match a previous extraction.
- `audioExtractionService.ts` and `configService.ts` still use relative imports (`../types/…`,
  `../../stores/…`, `../axios`). Everything else uses `$lib`/`$stores` — don't copy that.
- **Never put a total-request `timeout` on a request whose body is the file.** That caps how long a
  _healthy_ upload may take: the old 5-minute cap against the app's 15 GB limit failed every upload
  slower than ~50 MB/s, then burned all 3 retries repeating it. File bodies go through
  `uploadService.sendBody()`, which arms a `createStallWatchdog()` (abort only when no bytes have
  moved) and passes `watchdog.signal` to axios. `CONTROL_REQUEST_TIMEOUT_MS` is for the small JSON
  calls (`/files/prepare`, `/files/complete`, `/files/process-url`) only.
- A watchdog abort surfaces as `UploadStalledError`, **not** an axios `CanceledError`: `axios.isCancel`
  means "the user cancelled" throughout `uploadService` and suppresses retry. A stall must also not
  fall through to the legacy multipart path — re-sending the body through the API container stalls too.
- The presigned→legacy fallback swallows the error with no log; a broken presign looks like a slow
  upload. `configService` likewise swallows its fetch error and marks itself loaded, so "config
  failed" is indistinguishable from "no protected hosts".
- **The multipart path deliberately has no legacy fallback.** It is chosen for objects too large
  to push through the API container, so falling back would do the exact thing it exists to avoid —
  and a mid-transfer failure is _resumable_, which re-sending from zero is not. `uploadService`
  parks a `MultipartSession` on the `UploadItem` for the whole transfer; the queue's normal retry
  then resumes from the parts the bucket already holds instead of re-hashing and re-preparing.
  The session is in-memory only (a `File` cannot be persisted), so resume covers retries within
  the session, not a page reload. A 404/409 from `/files/multipart/parts` means the upload is
  gone server-side and is the one case that restarts from `/prepare`.
- **Giving up on a multipart upload must abort it** — object storage bills for the parts of an
  incomplete upload. `cancelUpload`, the out-of-retries branch, and `reset()` (logout, before the
  cookie goes away) all fire `DELETE /files/{fileId}`. `part_size`, `part_count` and the batch
  size are all backend decisions; `multipartUploader` only executes them.
- Persistence only restores `url`-type uploads — File/Blob sources can't survive a reload (by design).
- `audioExtractionService` is a **deliberate** exception to the thin-frontend rule (it avoids uploading
  multi-GB video), like `$lib/export/`. Don't extend the precedent to business logic.
- `uploadService.formatTimeRemaining` is a compact `Xh Ym` ETA that returns `''` — it is on the explicit
  do-NOT-migrate list in `$lib/utils/CLAUDE.md`. Leave it alone.
- Tests cover `fileFingerprint`, `stallWatchdog`, `multipartUploader` and — since `uploadService.test.ts`
  landed — the queue's orchestration decisions (fallback eligibility, multipart session survival,
  abandoned-upload release, duplicate handling). This line used to say the queue was untested.
- **A duplicate 409 from `POST /files` is a SUCCESSFUL dedup, not an upload failure.** The
  presigned `prepare` catches most duplicates before a body is sent (`is_duplicate`), but the
  backend re-checks and answers `409 {detail: {message, duplicate_file_uuid}}` for the two cases
  it cannot: the same content uploaded between prepare and POST, and a fingerprint skipped for
  the pre-check that still reached the form data. `duplicateFromConflict` routes that into the
  existing duplicate path; a 409 _without_ the uuid is some other conflict and must still fail,
  or a real error gets reported as a file safely stored.
