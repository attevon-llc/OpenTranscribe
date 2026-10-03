# Recovery, cancellation and worker-metrics fixes — plan

Four independent fixes, one PR each, each branched from `origin/master`. Every fix is
test-first: the new test is run and seen to fail before the code change.

| PR | Issue | Branch |
|----|-------|--------|
| A | #1162 infra-requeue counter never cleared for cancelled files | `fix/infra-requeue-clear-on-terminal` |
| B | #1163 file cancelled mid-stage recorded as failed | `fix/cancel-mid-stage-classified-cancelled` |
| C | #1161 per-task Celery worker metrics | `feat/celery-worker-task-metrics` |
| D | #1160 flaky lockout re-probe boundary test | `test/lockout-reprobe-boundary-deterministic` |

## A — #1162: clear the infrastructure-requeue counter on every terminal transition

**Problem.** `task_liveness.INFRA_REQUEUES_KEY` (sorted set, member = file uuid) is cleared only
by `postprocess.finalize_transcription` (success) and `transcription_retry._fail_file`. Every
other terminal write leaves the entry behind: `cancellation.finish_cancelled`,
`cancellation.reconcile_cancellation`, `task_utils._finalize_unconfirmed_cancellation`,
`dispatch.on_pipeline_error` ("Transcription pipeline failed unexpectedly"), the
`task_recovery_service` ERROR paths, and so on. `transcription_files_infra_requeued` then counts
those files forever, and a re-run inherits the stale count.

**Fix — one choke point.** Every terminal status write, from any module, is an ORM write of
`MediaFile.status`. Register SQLAlchemy session listeners (on the `Session` class, so every
session in the API and in workers is covered):

* `after_flush`: for each flushed `MediaFile` whose `status` history gained a terminal value
  (`COMPLETED`, `ERROR`, `CANCELLED`), remember its uuid in `session.info`.
* `after_commit`: `clear_infra_requeues(uuid)` for each remembered uuid (after the commit, so a
  rolled-back write never resets the budget).
* `after_rollback`: forget the remembered uuids.

Where: new module `backend/app/core/infra_requeue_reset.py` (listener + registration), registered
from `backend/app/models/media.py` after `MediaFile` is defined so any process that can write a
file status has it. Non-transcription writers of a terminal status (rediarize, YouTube import)
also clear the entry; that is harmless because no transcription run is in flight for that file.
The retry path never writes a terminal status between attempts (`_retry_file` writes `PENDING`;
worker-loss requeues do not touch the status), so the poison-loop budget still accumulates.

**Belt and braces — gauge.** `update_transcription_lease_metrics` stops using a bare `ZCOUNT`:
`task_liveness.files_requeued_at_least(threshold)` returns the member uuids, and the gauge counts
only those whose `MediaFile.status` is not terminal. Stale entries written before this fix (or by
a missed path) can no longer hold the alert up.

**Tests** (`backend/tests/unit/test_infra_requeue_terminal_clear.py`, real rows on the
savepoint-backed `db_session`, dict-backed Redis from `tests/unit/_fake_liveness_redis.py`):

* user cancel confirmed by the worker (`finish_cancelled`) removes the entry;
* `reconcile_cancellation` (CANCELLING → CANCELLED) removes it;
* `cancel_active_task` when the flag cannot be armed (immediate CANCELLED) removes it;
* `on_pipeline_error` ("Transcription pipeline failed unexpectedly") removes it;
* a recovery ERROR path via `update_media_file_status(ERROR)` removes it;
* a non-terminal write (`PENDING`, `PROCESSING`, `CANCELLING`) keeps it;
* a rolled-back terminal write keeps it;
* the gauge ignores files in a terminal state and counts a PROCESSING one.

## B — #1163: an exception after a cancel request finalizes as cancelled

**Problem.** A cancel arms the run's flag and moves the file to `CANCELLING`. If the running stage
then raises anything other than `TranscriptionCancelledError` (storage fetch error, model error,
connection reset because the stage was being torn down), the generic `except Exception` path runs
`_handle_transcription_failure` → `finish_failed_run` → ERROR + error notification, and the stage
re-raises so `dispatch.on_pipeline_error` also writes ERROR / "Transcription pipeline failed
unexpectedly". User cancellations are counted as pipeline failures.

**Status convention (kept).** The codebase records a confirmed cancel as Task `status="failed"`
with `error_message="Task cancelled by user"` (`cancellation.CANCELLED_BY_USER`) — there is no
`cancelled` task status, and the Tasks UI/API and `run_ownership` treat `failed` as terminal.
Adding a new task status is out of scope; instead the run is distinguishable by:
`MediaFile.status == CANCELLED`, `MediaFile.error_category IS NULL`,
`Task.error_message == "Task cancelled by user"`, and no error notification. The stage returns
the `{"status": "cancelled", ...}` chain payload (a normal return), so Celery records the stage as
SUCCESS, not FAILURE, and `on_pipeline_error` never runs for it.

**Fix.**

1. `cancellation.finalize_cancelled_run(task_id, file_id, file_uuid, user_id)` — the body of
   `finish_cancelled` extracted so both the checkpoint path and the failure path share one
   implementation; also clears `error_category`.
2. `transcription_retry.finish_failed_run`: if `_cancel_requested(media_file, task_id)` (the
   run's flag, or the file is CANCELLING/CANCELLED while still pointing at this run), finalize as
   cancelled and return the new `RunOutcome.CANCELLED` — no ERROR, no retry, no notification.
3. `context._handle_transcription_failure`: on `CANCELLED` return the cancelled payload.
4. Stages return that payload instead of re-raising: preprocess (`_mark_pipeline_error`),
   GPU (`core._finish_failed_or_aborted`), diarize (`diarize_task`), CPU (`cpu_task`).
   `run_ownership.superseded_or_none` passes a cancelled payload straight through so the next
   stage forwards it to `finalize_transcription`, which already no-ops and releases temp audio.
5. `dispatch.on_pipeline_error`: if the run was cancelled, finalize as cancelled (idempotent)
   instead of ERROR; never overwrite a `CANCELLED` file with ERROR.
6. `postprocess.finalize_transcription` failure path: if the run was cancelled, finalize as
   cancelled instead of writing "Post-processing error".

**Tests** (`backend/tests/unit/test_cancel_mid_stage_outcome.py`): with the cancel armed and the
file CANCELLING, make each stage raise and assert file `CANCELLED`, Task
`error_message == "Task cancelled by user"`, `error_category is None`, no error notification, and
the stage returns the cancelled payload:

* preprocess / download (storage fetch raises);
* GPU stage (model raises);
* diarize stage;
* CPU stage;
* `on_pipeline_error` with the cancel armed;
* postprocess failure with the cancel armed;
* control: the same exception with no cancel still fails the file (ERROR) — unchanged.

## C — #1161: per-task Celery worker metrics

Contract (other components depend on these names):

* env `WORKER_METRICS_PORT` — int; unset/empty = disabled (default). Documented on the
  environment-variables docs page.
* `celery_task_total{task, outcome}`, outcome ∈ {success, failure, retry, revoked}.
* `celery_task_runtime_seconds{task}` histogram, buckets 0.5 s … 2 h.
* `task` = registered task name (bounded; names not registered on the app map to one fixed
  bucket; no ids in labels).
* Recorded via Celery signals in the worker; exposed by an HTTP listener on
  `WORKER_METRICS_PORT` in each worker; prefork-safe; recording failures swallowed at debug.
* Update the "worker-process trap" note in `backend/app/core/metrics.py`.

Tests: handlers increment the right labels; listener off by default; cardinality bounded;
histogram observes runtime. The prefork design choice is documented in the PR and the module.

## D — #1160: deterministic lockout re-probe boundary test

Find the shared-state cause (the test patches `time.monotonic` process-wide, so any other thread
calling the store resolver during the frozen window perturbs it; module globals between reset and
call). Make the code under test read a module-local clock seam the test patches, so the test no
longer depends on process-wide state, without weakening any assertion. Same for the session-store
twin if it shares the pattern. Prove stability by running the file 30× in a loop.

## Verification per PR

* Targeted tests first (seen failing, then passing), then the neighbouring suites
  (`tests/unit/test_transcription_retry_policy.py`, cancellation and liveness tests for A/B).
* `scripts/safe-precommit.sh run --files <changed>` (both stages) before each commit.
* Push once; `gh pr create` to `master`; watch CI with `gh pr checks <n> --watch`.
