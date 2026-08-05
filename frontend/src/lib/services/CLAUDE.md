# src/lib/services — stateful singletons

## Purpose

Long-lived objects that own state a component must not own: an upload queue, a WASM
runtime, a Web Worker, a TTL cache. Import via `$lib/services/...`.

## The layering rule

- `$lib/utils` — pure functions, no state.
- `$lib/api` — stateless typed wrappers around one endpoint family (one call → one response).
- `$lib/services` — **stateful**: lifecycle, queue, worker, or cache that must survive across
  components and route changes.

If a new module has no state and no lifecycle it belongs in `api/` or `utils/`, not here.
Every module here exports **one shared instance** (`export const xService = …`); never construct
a second one in a component. (`AudioExtractionService` is also exported as a class — for tests only.
`stallWatchdog.ts` is the other exception: a per-request factory, not a singleton.)

## Key files

- `uploadService.ts` — the upload queue singleton: max 3 concurrent, 3 retries with exponential
  backoff, persisted to `localStorage['upload_queue']`. Tries a **presigned PUT
  straight to MinIO, then silently falls back** to the legacy multipart `POST /files`.
- `stallWatchdog.ts` — `createStallWatchdog()`: the timeout control for file bodies. See gotchas.
- `audioExtractionService.ts` — in-browser video→audio via FFmpeg.wasm (`-c:a copy`, no re-encode),
  core loaded from `frontend/public/ffmpeg/`; extractions run **one at a time** through an internal queue.
  **Import it with a dynamic `import()`** (see `FileUploader.svelte`) — a static import drags the
  `@ffmpeg/*` wrapper into whichever route chunk references it, and extraction is an opt-in path.
- `sha256Hasher.ts` — client for `$lib/workers/sha256Worker`, with a main-thread `crypto.subtle` fallback.
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
- **One hasher: `hashFileSHA256` from `$lib/services/sha256Hasher`.** It runs in a Web Worker (with a
  main-thread fallback for jsdom/SSR/old browsers) so the UI stays responsive on multi-GB files —
  never call `file.arrayBuffer()` + `crypto.subtle.digest` directly. `audioExtractionService` did
  exactly that until #302, on **video** files, which are the largest inputs the app accepts (15 GB):
  it buffered the whole file into memory and blocked the event loop for the length of the hash.
  `uploadService` previously wrapped it in a local `calculateFileHash`; that wrapper is gone and both
  call sites now use `hashFileSHA256` directly.
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
- Persistence only restores `url`-type uploads — File/Blob sources can't survive a reload (by design).
- `audioExtractionService` is a **deliberate** exception to the thin-frontend rule (it avoids uploading
  multi-GB video), like `$lib/export/`. Don't extend the precedent to business logic.
- `uploadService.formatTimeRemaining` is a compact `Xh Ym` ETA that returns `''` — it is on the explicit
  do-NOT-migrate list in `$lib/utils/CLAUDE.md`. Leave it alone.
- Tests here cover `sha256Hasher` and `stallWatchdog` only; the queue itself is untested.
