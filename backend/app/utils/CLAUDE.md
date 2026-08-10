# backend/app/utils — shared helpers

## Purpose

Low-dependency helpers shared by endpoints, services, and Celery tasks. DB-touching query
builders are allowed here (`db_helpers`), but anything with real domain logic belongs in
`app/services`. These modules load in **all three worker flavors** (API, GPU, CPU/redaction),
so keep heavy imports lazy.

## Key files — single-home rules

- `time_format.py` — **the only home for backend timestamp formatting**
  (`format_timestamp_simple`, `format_srt_timestamp`). Display-level formatting of durations /
  status / sizes belongs to `services/formatting_service.py`.
- `websocket_notify.py` — `send_ws_event(user_id, type, data)` is **THE** way any synchronous
  code (endpoint, task, service) pushes a notification. It publishes to Redis; `api/websockets.py`
  fans out to the connected sockets. Returns bool, never raises.
- `uuid_helpers.py` — the hybrid-ID + permission chokepoint. `get_*_by_uuid`,
  `get_file_by_uuid_with_permission` (admin bypass → takedown 404 → public → tenant gate → owner
  → shares, in that order), `require_resource_owner`.
- `db_helpers.py` — `apply_tenant_scope` (SQL-plane default-deny tenant filter mirroring
  `api/deps_context.scope_to_context`), user file/tag/speaker query builders, tag-cache busting.
- `auth_decorators.py` — `require_file_ownership`, `require_admin`, `AuthorizationHelper`.
  **kwargs-only**: they read `db`/`current_user`/`file_id` out of `kwargs` and raise `ValueError`
  if the caller passed positionally. Prefer FastAPI `Depends` for new endpoints.
- `error_handlers.py` — `handle_database_errors` (rolls back the session in `kwargs["db"]`) and
  `ErrorHandler` builders for opaque 5xx. `pagination.py` — `paginate()` replaces the
  count+offset+limit boilerplate (counts with `order_by(None)`).
- `encryption.py` — AES-256-GCM (v3) with legacy Fernet auto-detect. Every stored secret (ASR/LLM
  keys, S3/SMB creds, OIDC refresh/ID tokens) goes through it.
- `uuid7.py` — RFC 9562 UUIDv7, the `default=` for every model `uuid` column (index locality).
- `scratch_volume.py` — cross-worker WAV handoff at `/scratch/opentranscribe`. **Presence of the
  mount is the feature flag** — there is no enable/disable env var.
- `task_lock.py` — Redis lock preventing overlapping periodic tasks. `task_utils.py` — task
  records, status transitions, stuck-file recovery. `error_classification.py` — permanent vs
  retriable, gating retry decisions.
- `hardware_detection.py` — the CUDA/MPS/CPU authority (`should_use_hybrid_mode`).
- `text_preprocessing.py` — **topics/collections extraction ONLY**; never use it for
  summarization or speaker ID (it strips the grammar those need).
- `segment_dedup.py`, `segment_postprocess.py`, `diarization_merge.py`, `diarization_metrics.py`
  — pipeline math; see `backend/app/transcription/CLAUDE.md`.
- `benchmark_timing.py` / `vram_profiler.py` / `nvml_monitor.py` — instrumentation gated on
  `ENABLE_BENCHMARK_TIMING` / `ENABLE_VRAM_PROFILING`, so production pays zero overhead.

## Conventions / patterns

- No `app.api` imports from here (`db_helpers` types `RequestContext` under `TYPE_CHECKING`).
  Import-linter also forbids `cloud` and managed-edition vendor imports — see
  `backend/app/core/CLAUDE.md`.
- Optional heavy deps (pyannote.metrics, meeteval, torch) are imported **inside** the function
  that needs them so these modules stay importable on CPU-only workers and in fast unit tests.
- Best-effort side paths (cache invalidation, WS publish, metrics) log and swallow — they must
  never break the write they accompany.

## Gotchas

- **Both dedup columns now hold the same *kind* of value, and neither is collision-resistant.**
  `MediaFile.imohash` (via `services/imohash_service.py`) is the server-computed sampled
  fingerprint; `MediaFile.file_hash` is the *client-declared* one, and since issue #342 the
  browser computes it with the same imohash algorithm rather than SHA-256 — whole-file SHA-256
  threw `NotReadableError` above ~4 GB and the swallowed error silently disabled duplicate
  detection on the largest uploads. `file_hash.py:check_duplicate_by_fingerprint` matches
  **either** column, so rows predating the change (SHA-256 in `file_hash`, imohash in `imohash`)
  keep deduplicating. Never use either for security-sensitive equality.
- `encryption.py` transparently decrypts legacy Fernet ciphertext — don't assume a single format.
