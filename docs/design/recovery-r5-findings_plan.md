# Recovery findings from a multi-worker load test — plan

One branch (`fix/recovery-r5-findings`), one PR. Test-first: every new test is run and seen to
fail on `master` before the change that makes it pass (except where the finding turns out to be
already handled, in which case the test proves that and is labelled as such).

| Issue | Scope here |
|-------|------------|
| #1178 | Late progress write resurrects a reclaimed run; reclaim 0.1 s after creation |
| #1179 | Deliveries of a worker OOM-killed and restarted in place (same hostname) |
| #1073 | Host-RAM admission guard for GPU task concurrency (partial: step 2 stays open) |

#1180 (throughput investigation) is out of scope.

## #1178 — superseded run resurrects a reclaimed Task row

### Root cause (read from the code)

`dispatch_transcription_pipeline` / `dispatch_batch_transcription`:

1. `create_task_record` commits the Task row with `updated_at = NULL` (the column has only an
   `onupdate`, no default).
2. Two more commits (media-file tracking, `update_media_file_status`) follow, then
   `update_task_status(..., "in_progress", progress=0.0)` finally sets `updated_at`.
3. `mark_queued(task_id)` runs only after all of that (in the batch path, after every file of
   the batch was built).

`transcription_retry.recover_lost_runs` (the orphan sweep's second half, every 60 s) selects
transcription rows with `updated_at IS NULL OR updated_at < now - BROKER_ORPHAN_STALE` and
probes the liveness markers. A row caught between (1) and (3) has `updated_at IS NULL`, no lease
and no queued marker, so it reads `DEAD` and is reclaimed — 0.1 s after creation. The original
pipeline message is published anyway; its preprocess stage passed the ownership check (or was
already past it), and its `update_task_status(..., "in_progress", progress=0.2)` then rewrote
the failed row to `in_progress`, where it stays forever.

### Fix

1. **Conditional status writes** (`app/utils/task_utils.update_task_status`): for a
   `transcription` row, a write that sets an ACTIVE status (`pending`, `in_progress`) is a single
   `UPDATE task SET ... WHERE id = :id AND status IN ('pending','in_progress')`. Zero rows
   matched → the write is dropped, the media file is not touched, and a DEBUG line is logged.
   The row id IS the run id (`Task.id` = application task id), so "owned by the writing run"
   is the `WHERE id = :id`; a replaced run always gets a new id.
   * Scoped to `task_type == "transcription"` on purpose: other task types (search indexing,
     speaker clustering) mark their row `failed` and then `self.retry()` under the SAME id, and
     their next attempt legitimately moves that row back to `in_progress`.
   * Terminal writes (`completed`/`failed`/`skipped`) are unchanged.
2. **Reclaim grace period**: new `TaskRecoveryConfig.TRANSCRIPTION_RECLAIM_GRACE`
   (`TRANSCRIPTION_RECLAIM_GRACE_SECONDS`, default 120, clamped to ≥
   `TRANSCRIPTION_HEARTBEAT_TTL_SECONDS`). A run younger than this (by `Task.created_at`) is
   never reclaimed as lost:
   * `recover_lost_runs` adds `Task.created_at < now - grace` to its query;
   * `recover_lost_run` (the single choke point every DEAD path goes through, including the
     health check's `_recover_stuck_transcription`) re-checks it on the locked row and returns
     `None` inside the window.
3. `create_task_record` stamps `updated_at` at creation, so a fresh row no longer matches the
   `updated_at IS NULL` arm at all (belt and braces with 2).

### Tests (`backend/tests/unit/test_reclaimed_run_stays_terminal.py`)

* `test_a_late_progress_write_does_not_resurrect_a_reclaimed_run` — a transcription row failed
  by recovery stays `failed` (progress, `completed_at`, error message untouched) after the old
  run writes `in_progress`/0.2.
* `test_a_current_run_still_records_progress` — the guard does not block the live run.
* `test_a_same_id_retry_of_another_task_type_may_reopen_its_row` — non-transcription same-id
  retry still works.
* `test_a_freshly_dispatched_run_is_not_reclaimed_inside_the_grace_window` — a row created
  now, `updated_at` NULL, no markers: `recover_lost_runs` acts on nothing.
* `test_recover_lost_run_refuses_a_run_inside_the_grace_window` — the choke point itself.
* `test_a_run_past_the_grace_window_is_still_reclaimed` — the grace does not disable recovery.
* `test_the_grace_is_never_shorter_than_the_heartbeat_ttl` — config clamp.
* `test_a_new_task_row_has_updated_at` — `create_task_record` stamps it.

## #1179 — worker restarted in place with the same hostname

### Investigation

The orphaned-delivery sweep (`app/core/broker_orphans.py`) never keys liveness on a hostname:

* a transcription stage is judged by its run lease (`transcription_heartbeat:<run>`, keyed by
  run id, refreshed by a thread inside the process that executes the stage) — it dies with the
  process and lapses within `TRANSCRIPTION_HEARTBEAT_TTL_SECONDS`;
* "held by a live worker" is the set of Celery **message ids** the live workers report via
  `inspect active/reserved/scheduled` — a restarted process starts with an empty request
  buffer, so it never reports its predecessor's message ids, whatever its node name;
* untracked tasks: per-task-id heartbeat (`task_heartbeat:<id>`), same property.

So the restarted process cannot make its predecessor's deliveries look owned. The plan is to
**prove** that with tests rather than change the identity model:

* Unit (`backend/tests/unit/test_broker_orphans_restart_in_place.py`): drive
  `reclaim_orphaned_deliveries` with a fake broker where the inspect replies come from the SAME
  node name (`celery@gpu-worker-0`) as the dead process, reporting only the new process's
  message; assert the predecessor's stage and untracked deliveries are requeued and the new
  process's delivery is left alone. Also assert `held_by_live_workers` returns message ids, not
  node names.
* Live (`backend/tests/integration/test_orphaned_delivery_recovery_live.py`,
  `test_a_worker_restarted_in_place_does_not_shield_its_predecessors_stage`): a real worker
  with a fixed node name is SIGKILLed mid-stage, a new worker is started with the SAME node
  name, and the reaper — using the real `inspect` broadcast, not `held_ids=set()` — requeues the
  stage and the new worker runs it.

If either test fails on `master`, switch to process identity (hostname + pid / boot nonce in
the lease value) instead. PR text and an issue comment state the conclusion, and list what the
observation could have been instead (a message prefetched by a live, busy worker is reported by
`inspect reserved` and correctly left alone; `celery_queue_oldest_unacked_age_seconds` counts it).

## #1073 (partial) — host-RAM admission guard for GPU task concurrency

### Problem

Step 1 (#1112) only applies to `GPU_CONCURRENT_REQUESTS=auto`. An explicitly configured
concurrency (e.g. `--concurrency=8`/`GPU_CONCURRENT_REQUESTS=8`) on a 16 GB host runs 8 GPU
tasks at once and is OOM-killed; nothing caps concurrent tasks by host RAM. Its default per-task
figure (4096 MB, sized for a 4-hour file with in-process diarization) also caps a 32 GB host at
7, below the 12 a load test ran successfully.

### Fix

New module `backend/app/transcription/host_memory_admission.py`:

* `host_memory_cap()` → `max(1, (budget - GPU_HOST_BASELINE_MB) // GPU_PER_TASK_HOST_MB)`, the
  budget from `TranscriptionConfig._host_memory_budget_mb()` (cgroup v2 `memory.max`, then v1
  limit, never above `/proc/meminfo` `MemTotal`). Returns `None` when nothing is readable.
* `configure(configured)` → effective cap `= min(configured, host cap)`; logs one INFO line with
  every input (configured, budget + source, reserve, per-task estimate, host cap, effective) and a
  WARNING when the cap is below the worker's thread concurrency; sets the worker gauges.
* `task_slot(stage)` → FIFO counting gate (same shape as `vram_budget.VramBudget`); a wait longer
  than `GPU_VRAM_ADMISSION_TIMEOUT_S` raises `HostMemoryAdmissionTimeoutError`
  (a `TranscriptionAbortedError`, so the task layer requeues it). No-op until configured or when
  `GPU_HOST_MEMORY_ADMISSION=false`.
* The static VRAM formula is deliberately NOT part of the cap for an explicit setting: it
  assumes 4 GB/task and would cap a 24 GB card at 4, while VRAM is already admission-controlled
  per stage (`vram_budget`). For `GPU_CONCURRENT_REQUESTS=auto` the configured value is already
  `min(VRAM-based, host-based)`.

Wiring:

* `app/core/celery.preload_models` (GPU worker branch): `host_memory_admission.configure(...)`
  with the threads-pool concurrency (prefork: per-process concurrency is 1; not gated).
* `transcribe_gpu_task` and `diarize_gpu_task`: take a slot inside the existing
  `try` (via an `ExitStack` in the `with` header), so a timeout reaches the abort handler and is
  requeued.
* Gauges on the worker metrics registry (`app/core/worker_metrics.py`, served on
  `WORKER_METRICS_PORT`): `gpu_worker_concurrency_configured`,
  `gpu_worker_concurrency_host_memory_cap`, `gpu_worker_concurrency_effective`.

Default change: `GPU_PER_TASK_HOST_MB` 4096 → 2048. Derivation from the load test: 16 GB hosts
were OOM-killed at 8 concurrent tasks and ran at 6; 32 GB hosts ran 12. With the 2560 MB
reserve, 2048 MB/task predicts 8 × 2048 + 2560 = 18.9 GB (> 16 GB, OOM — matches), 6 tasks =
14.8 GB (fits), 12 tasks = 27.1 GB (fits 32 GB). So a 16 GiB budget gives
(16384 − 2560) // 2048 = 6 and 32 GiB gives 14 (capped by the configured 12). Workloads
dominated by multi-hour files with in-process diarization should set 4096.

New env: `GPU_HOST_MEMORY_ADMISSION` (default `true`). Documented in `.env.example` and
`docs-site/docs/configuration/environment-variables.md`.

### Tests (`backend/tests/transcription/test_host_memory_admission.py`)

* 16 GiB cgroup, configured 8 → effective ≤ 6; 32 GiB, configured 12 → 12.
* cgroup v2 limit preferred; `max` falls back to MemTotal.
* `GPU_PER_TASK_HOST_MB` / `GPU_HOST_BASELINE_MB` override.
* Gate admits up to the cap, blocks the next, admits it when a slot frees, times out with
  `HostMemoryAdmissionTimeoutError` (a `TranscriptionAbortedError`).
* Disabled / unconfigured → no-op.
* Startup log line contains the decision; gauges set.
* `transcribe_gpu_task` holds a slot while it runs (wiring test).

### Not done here (remaining #1073)

Step 2 — stop decoding whole files into memory (stream / window the decode for Whisper and the
diarizer). Stays open on #1073.

## Verification

* Throwaway Postgres (`postgres:16`) migrated with `alembic upgrade head`, same env as the CI
  `backend-tests` job; targeted tests, then the full `pytest tests/` suite.
* Live orphan test: throwaway `redis:7-alpine`, `REDIS_PORT=<port> pytest -m integration
  tests/integration/test_orphaned_delivery_recovery_live.py`.
* `scripts/safe-precommit.sh run --all-files` and `--hook-stage pre-push`.
