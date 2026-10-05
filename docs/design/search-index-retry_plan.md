# Search indexing: retry until indexed, durable sweep, visible state (#1182) - plan

One branch (`fix/search-index-retry`), one PR. Test-first: each new test is run and seen to fail on
`master` before the change that makes it pass.

## Problem

`index_transcript_search_task` retries 3 times (30/60/120 s). Under load every attempt hits the
OpenSearch client's default 10 s read timeout, the `search_indexing` task row ends `failed`, and
nothing ever re-indexes the file. The transcript is delivered but missing from search, silently.

## Existing machinery checked

- `search_index_maintenance` (beat, every 6 h) already re-indexes completed files with no chunks,
  but it scans the whole index, is per-user and hours-slow, and never looks at the task row.
- `services/search/reindex_dispatch.dispatch_transcript_reindex` is the one debounced dispatch
  entry point; it gains an optional `priority` and is reused by the new sweep.
- The Redis broker is configured with `priority_steps=range(10)`, so per-message priority works
  (0 is popped first). `EmbeddingPriority` only has `PIPELINE_CRITICAL = 2`; a new
  `PIPELINE_RETRY = 1` mirrors `CPUPriority.PIPELINE_RETRY` / `GPUPriority.TRANSCRIPTION_RETRY`.
- Metrics: `app/core/metrics.py` (API-process collectors, DB state sampled at scrape through
  `backup_metrics.refresh_job_metrics`) and `app/core/worker_metrics.py` (per-task Celery outcome
  counters on the worker port).

## Changes

1. **Timeout / idempotent retry** (`services/opensearch_service/client.py`, new
   `OPENSEARCH_INDEX_TIMEOUT_S=60`, `OPENSEARCH_INDEX_TIMEOUT_RETRIES=2`): helper
   `call_idempotent_write(fn, **kw)` passes `request_timeout` and retries only
   `ConnectionTimeout`. Used by the three chunk-index `bulk` calls and the full-document `index`
   call (all use explicit `_id`s, so a repeat is a no-op overwrite). Client-wide timeout and
   search/query timeouts are untouched (a client-level `retry_on_timeout` would also re-run
   searches, which is why it is not used).
2. **Retry until success** (`tasks/search_indexing_task.py`, `services/search/index_retry.py`):
   exponential backoff `base * 2^n` capped per attempt, with full jitter; the attempt budget is
   derived from `SEARCH_INDEX_RETRY_HORIZON_S` (default 6 h). Retries are re-published with
   `EmbeddingPriority.PIPELINE_RETRY`, ahead of first attempts. A deleted file is a terminal,
   non-retryable error.
3. **Visible state**: while retries remain the `search_indexing` row is set to `pending` with an
   explanatory message (not `failed`), so the status modal shows work still outstanding. It is
   `failed` only after the horizon is exhausted.
4. **Durable sweep** (`tasks/search_index_sweep_task.py`, beat every 10 min, utility queue):
   completed files with transcript segments whose latest `search_indexing` row is `failed`, or that
   have none, and with no active row. Bounded batch (`SEARCH_INDEX_SWEEP_BATCH_SIZE`), per-file
   Redis cooldown (`SEARCH_INDEX_SWEEP_COOLDOWN_S`), `with_task_lock`, dispatches through
   `dispatch_transcript_reindex(priority=PIPELINE_RETRY)`. No-op when `OPENSEARCH_ENABLED=false`.
   Files with no row are only considered within `SEARCH_INDEX_SWEEP_LOOKBACK_HOURS` so an upgrade
   does not re-index the whole library.
5. **Metrics**: `search_indexing_failures_total{reason}`, `search_indexing_retries_total` (worker
   side), `search_indexing_files_awaiting_reindex` gauge (sampled from the DB at scrape).

## Tests (`backend/tests/unit/test_search_index_retry_1182.py`)

- timeout on every attempt: row never left `failed` while retries remain, retry published at the
  higher priority, delay capped and jittered; after the horizon it is `failed` and the sweep picks
  it up.
- `call_idempotent_write` applies the configured timeout and retries only timeouts; the bulk and
  index call sites use it.
- sweep: selects failed/missing, skips active/completed, idempotent across runs (cooldown), batch
  bound, disabled with `OPENSEARCH_ENABLED=false`.
- metrics gauge value, beat entry registered, env vars documented.

## Docs

`.env.example` and `docs-site/docs/configuration/environment-variables.md`.
