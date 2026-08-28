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
  cannot say what the chunks were indexed with. Three rules the follow-up round added:
  - **Key on the CANONICAL indexed label,
    `app/utils/speaker_labels.py::canonical_speaker_label_for_row`** — the SAME resolver the
    chunk-index writers (`search_indexing_task`, `reindex_task`) call. Keying on `display_name`
    alone missed a *cleared* label (a legal `{"display_name": ""}`, which reverts to the
    diarizer label) and an edit to `name` alone; keying on the older `display_name or name`
    chain (removed in issue #605) additionally missed a confident LLM/embedding
    `suggested_name` — the indexer trusts one at `confidence >= 0.75` and eight
    repair/propagation call sites kept computing the pre-suggestion value, so a rename
    computed the wrong `old_names`, the `update_by_query` matched nothing, and it logged
    `status: success` while the drift survived. `SpeakerRenameTracker.record`/`.flush` is the
    seam every writer of `suggested_name`/`confidence` must route through too, not just a
    `display_name` write — five writers previously dispatched nothing at all when a
    suggestion alone moved the canonical label.
  - **Both tasks re-resolve their target from Postgres at run time**, so two renames dispatched
    close together converge instead of inverting. `A→B` and `B→C` are unordered on an 8-way
    queue; if `B→C` ran first it matched nothing and `A→B` then wrote **B**, recreating #405's
    own bug. Pass `speaker_id` so the task can re-read.
  - **`version_conflicts` is read and retried.** `conflicts="proceed"` only means "do not abort
    the whole update_by_query"; nothing re-examines the skipped documents, so a concurrent
    title+speaker rename left a subset of chunks stale while reporting success.
  - Scope differs on purpose: **title covers the whole file plane** (a digest inherits `title`
    and renders it as a citation), **speaker covers the chunk plane only** (a digest has no
    `speaker` field and its prose bakes the name — regeneration, not rewriting, is the fix).
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
- `erasure_reconciliation.py` — `gdpr.erasure_reconcile`, **utility** queue, daily 04:40.
  Finishes GDPR Art. 17 erasures that a legal hold deferred, and re-erases subjects a
  backup restore brought back. `takedown_service.release_file` calls its
  `notify_hold_released` for latency; **the schedule is the guarantee** — a hold can also
  be cleared by a DB edit or a restore, and a hook alone has a silent failure mode.
  Design rationale (what the ledger must never store, and why `org_member` entries are
  never auto-re-erased) lives in `app/services/CLAUDE.md`.
- `directory_sync_task.py` — LDAP reconciliation/deprovisioning, **cpu** queue.
  `directory.sync_check_schedule` runs from beat every 15 min, reads the DB-stored cron
  (`directory_sync.schedule`), and dispatches `directory.sync_run` when due — so changing the
  schedule needs **no beat restart**. It claims the window by stamping
  `directory_sync.last_run_at` *before* dispatch, and the run itself takes a Redis lock, so a
  double tick cannot start two passes. Policy and safety rules live in
  `services/directory_sync_service.py`; this module is only the scheduling shell.
- `account_lifecycle.py` — FedRAMP AC-2 account-inactivity expiration, **utility** queue.
  `account.inactivity_sweep` runs from beat daily (04:25), locked, no due-check split (unlike
  `directory_sync_task`'s admin-configurable interval, this sweep's cadence is fixed and it
  either runs or is a no-op via `ACCOUNT_EXPIRATION_ENABLED`). Policy and safety rules live in
  `services/account_lifecycle_service.py`; this module is only the scheduling shell.

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
- **NEVER hold a DB session across slow non-DB work.** Three phases, always: a short read
  session returning **plain data**, the slow work with **no session open**, then a short
  write session. This is the single most repeated defect in this package — see below.

## The session-lifetime rule (read before writing any task)

A task that wraps its whole body in one `with session_scope() as db:` and then does MinIO,
ffmpeg, a model load, an LLM call, SMTP or OpenSearch **inside** it holds an open transaction
for the duration. `session_scope` is not at fault; it simply never gets to exit.

**This has wedged the live database twice, on two different workers, in one day.** The CPU
worker sat `idle in transaction` for 48 minutes (a 3-hour task, killed by the hard time
limit); the NLP worker for 1h26m. Both on a full-entity `transcript_segment` SELECT.

Why it matters beyond the hang — a plain SELECT takes `ACCESS SHARE` for the life of the
transaction, so:

1. **It queues `ALTER TABLE`.** That is an Alembic upgrade hanging mid-release, and dev runs
   migrations automatically on backend startup. It is how the bug was found: DDL migration
   tests started failing with `psycopg2.errors.LockNotAvailable`.
2. **It pins the VACUUM horizon** on `transcript_segment`, the largest table in the product.
3. **It consumes a pool connection permanently.**

**The fix pattern** (`speaker_attribute_task.py` is the worked example — read it):

- Read phase returns **plain data, never ORM instances**. An instance escaping the session can
  lazy-load later and silently reopen a transaction, reintroducing the bug invisibly.
- Narrow the query to the columns you use, and `outerjoin` rather than letting `seg.speaker`
  lazy-load per row.
- Where a callee takes `db` and does the slow work itself, change it to take the data — or to
  open its own short session. Passing `db` down is how the idiom spreads.
- Watch for objects that **hold** an ORM row (`LocalWatchClient` did) and for ORM attribute
  reads after scope exit. `db.expunge()` turns a silent lazy load into a loud
  `DetachedInstanceError`.

**`ThreadPoolExecutor` does not bound anything.** `__exit__` calls `shutdown(wait=True)` with
no timeout, so a per-future `result(timeout=30)` is decorative — one wedged child holds the
block, and the transaction, indefinitely.

**The gate: `scripts/audit-session-lifetime.py`.** Nine AST detectors (subprocess/ffmpeg,
object storage, OpenSearch, HTTP, LLM, model load, SMTP, thread pool, plus the
interprocedural one), an allowlist at `scripts/session-lifetime-allowlist.txt` keyed
`<file>::<scope>::<category>` with a **mandatory** reason, count-aware so one line buys one
finding, and a **stale entry fails the run** — the file can only shrink. `--selftest` after
touching a detector; `backend/tests/unit/test_session_lifetime_audit.py` runs the same cases
under pytest *and* mutation-checks every rule, because a detector that matches nothing
reports zero findings and reads exactly like a clean codebase.

> **The interprocedural rule exists because a body-scan is not enough.** `scan_single`'s
> `session_scope` wrapped `_perform_scan(db, ...)`; the remote listing, the per-file download
> and the MinIO upload were all one frame further down, so the first AST sweep — which only
> looked *inside* `with` bodies — reported it clean. The rule therefore also flags any
> function that both **accepts a `Session`** and does slow work, whichever end the leak is at.

**Testing it**: `tests/unit/test_task_session_lifetime.py` swaps the module's `session_scope`
for a depth-tracking stand-in and has each slow-call stub report the open-scope depth at the
moment it runs. Assert `>= 2` scopes opened, so a task that never touches the DB cannot pass.
A structural "does it call session_scope" test is not enough.

## How it connects

- `dispatch_upload_pipeline` — the shared post-upload tail — lives in
  **`app/api/endpoints/files/upload.py`**, not here. `services/watch_sources/processing.py`
  calls it so auto-import and manual upload share one path.
- Boundary smoothing is applied in `transcription/core.py` at the `finalize_segments()`
  chokepoint (`app/utils/segment_postprocess.py`). Deep detail: `app/transcription/CLAUDE.md`.
- Watch-source tasks: `watch_source_tasks.py`; deep detail in
  `app/services/watch_sources/CLAUDE.md`.

## Gotchas

- **`visibility_timeout` is set in `core/celery.py`'s `broker_transport_options`** —
  `CELERY_VISIBILITY_TIMEOUT` (default `21600`, 6h), not the Redis broker's 3600s default. This
  used to be unset, so `acks_late=True` transcription tasks (`core.py`, `preprocess.py`,
  `postprocess.py`) running past one hour were redelivered to another worker and transcribed
  twice — fixed, single source of truth is `core/celery.py`. Raising a file-length limit still
  needs this value to stay above the longest job it enables.
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
