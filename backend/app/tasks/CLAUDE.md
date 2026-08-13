# app/tasks — Celery tasks (the AI processing pipeline)

## Purpose

51 task modules. The core flow: upload to MinIO → metadata extraction → DB record →
Celery dispatch → WhisperX transcription (100+ languages, word-level timestamps, optional
translate-to-English) → PyAnnote diarization + voice fingerprinting → optional LLM
speaker-ID suggestions (**never auto-applied — manual verification only**) → optional LLM
summarization (BLUF, multi-section stitching, 12 languages) → DB write + OpenSearch
indexing → WebSocket notification.

## Key files

- `transcription/` — the 3-stage chain: `preprocess.py` (CPU: download, ffmpeg, MinIO temp)
  → `core.py` (GPU: Whisper + PyAnnote + DB save) → `postprocess.py` (CPU: embeddings,
  indexing, downstream dispatch). `dispatch.py` orchestrates the chain and routes
  `transcription.gpu_transcribe` / `cpu_transcribe` **at call time** — that's why they're
  deliberately absent from `task_routes`.
- `transcription/hooks.py` — cloud-edition seam. Exceptions are swallowed; **hangs are not**
  (hooks run inside the completion path holding an open `session_scope`). Any outbound call
  in a hook needs a tight timeout.
- `speaker_tasks.py` — a **re-export shim, NOT dead code**. Keeps legacy task-name routing
  alive; must stay in `celery_app`'s `include=` list.
- `rename_propagation_task.py` — `propagate_speaker_rename` / `propagate_title_rename`
  (**cpu** queue, issue #405). Chunk docs snapshot the speaker display name and the file title
  at index time; renames used to touch only the speaker and full-document indices, so chat's
  speaker scope (an exact `terms` match on the CURRENT name) silently lost every pre-rename
  chunk. Dispatch through `dispatch_speaker_rename` — it coalesces `(file_uuid, old_name)` pairs
  per file — and **capture the old name before the overwrite**: after the commit, Postgres
  cannot say what the chunks were indexed with.
- `ingest_artifacts_task.py` — `artifacts.generate_file_facts` (**nlp** queue, #383 Phase 2).
  Builds the deterministic ingest artifacts (statistics, extractive digest with per-sentence
  provenance, keyphrases) and upserts `file_facts`. It rides the nlp pool because that is the
  CPU-only enrichment pool, **not** because it calls a provider: unlike every other task on
  that queue it must NOT return early when no LLM is configured — the no-LLM deployment is
  exactly who it exists for (#403 D6). Dispatched fire-and-forget from
  `transcription/postprocess.enrich_and_dispatch`; logic lives in
  `services/ingest_artifacts/` (its own CLAUDE.md).
- `recovery.py` / `recovery_tasks.py` — `system.startup_recovery` and the periodic
  `cleanup.health_check` reclaim files stuck in PROCESSING with no live Celery task.
- `directory_sync_task.py` — LDAP reconciliation/deprovisioning, **cpu** queue.
  `directory.sync_check_schedule` runs from beat every 15 min, reads the DB-stored cron
  (`directory_sync.schedule`), and dispatches `directory.sync_run` when due — so changing the
  schedule needs **no beat restart**. It claims the window by stamping
  `directory_sync.last_run_at` *before* dispatch, and the run itself takes a Redis lock, so a
  double tick cannot start two passes. Policy and safety rules live in
  `services/directory_sync_service.py`; this module is only the scheduling shell.

## Conventions / patterns

- Queues (`core/constants.py:CeleryQueues`): `gpu`, `cpu`, `download`, `nlp`, `embedding`,
  `utility`, `redaction`, plus dynamic `cloud-asr`, `cpu-transcribe`, `gpu-transcribe`,
  `gpu-diarize`. **`task_create_missing_queues=False`** — a queue-name typo raises at
  dispatch instead of creating a phantom queue. `_validate_task_routes()` warns at worker
  startup for any registered task with no `task_routes` entry.
- Priorities are **per-queue** (`GPUPriority.X` is unrelated to `CPUPriority.X`); the scheme
  is documented in the comment block above `task_routes` in `core/celery.py`.
- Workers run `--pool=threads`, so `worker_process_init` does not fire — logging is wired via
  `setup_logging`. `request_id` is stamped on task headers at publish and cleared in
  `task_postrun`; never assume a ContextVar survives into the next task.

## How it connects

- `dispatch_upload_pipeline` — the shared post-upload tail — lives in
  **`app/api/endpoints/files/upload.py`**, not here. `services/watch_sources/processing.py`
  calls it so auto-import and manual upload share one path.
- Boundary smoothing is applied in `transcription/core.py` at the `finalize_segments()`
  chokepoint (`app/utils/segment_postprocess.py`). Deep detail: `app/transcription/CLAUDE.md`.
- Watch-source tasks: `watch_source_tasks.py`; deep detail in
  `app/services/watch_sources/CLAUDE.md`.

## Gotchas

- **`visibility_timeout` is NOT configured anywhere in this repo.** `broker_transport_options`
  only sets `priority_steps` / `queue_order_strategy`, so the Redis broker keeps its **3600 s
  default**. The transcription tasks are `acks_late=True` (`core.py`, `preprocess.py`,
  `postprocess.py`), meaning a run exceeding one hour is **redelivered to another worker →
  duplicate transcription of the same file.** This is real and unfixed; raise
  `visibility_timeout` past your longest task before increasing file-length limits.
- **Model loading is per-worker and must not leak across queues.** `PRELOAD_GPU_MODELS=true`
  only on the GPU workers; `PRELOAD_REDACTION_MODELS=true` only on `celery-redaction`
  (dedicated CPU service owning the `redaction` queue, run under `nice`). Importing a
  model-loading module into a task that runs on the wrong queue wastes 15+ GB of VRAM.
- Redaction detection is **not** unconditional: `_dispatch_redaction` returns early unless
  `resolve_effective_config(db, user_id).enabled`, and redaction is opt-out by default.
  Enabling redaction later triggers lazy detection on first file open. (Both the
  `postprocess.py` comment and the `redaction_task.py` docstring claimed "always" until #296.)
- Multi-GPU scaling: `./opentr.sh start dev --gpu-scale` sets `COMPOSE_PROFILES=gpu-scale`
  and loads `docker-compose.gpu-scale.yml` (N threads on `GPU_SCALE_DEVICE_ID`, tune
  `GPU_SCALE_WORKERS` to VRAM). **`GPU_SCALE_ENABLED` does not enable scaling** — only the
  `--gpu-scale` flag does; no compose file or startup script reads the variable. It *is* read
  in exactly one place, `tasks/utility.py` (~L199), to pick which GPU device IDs the
  system-stats task queries — so a stale value misreports which GPU is in use without
  changing any scheduling. Also note
  `GPU_SCALE_DEFAULT_WORKER` defaults to `0` in compose (default GPU worker disabled) but
  `1` in `.env.example` (both GPUs transcribe) — check which you actually have.
