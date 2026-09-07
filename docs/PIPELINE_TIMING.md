# Pipeline Timing Reference

End-to-end wall-clock instrumentation for the OpenTranscribe transcription pipeline. This document is the canonical reference for every marker, how to enable them, how to read them, and how to interpret the numbers.

See also:
- `docs/BENCHMARK_GUIDE.md` — operational guide for running the scripts
- `docs/BENCHMARK_RESULTS.md` — headline numbers from production hardware
- `docs/performance-whitepaper/` — academic write-up of the end-to-end pipeline

## Why this exists

The pipeline runs across the API worker, Redis, Postgres, MinIO, three Celery worker pools, and a browser. No single log file tells you where wall-clock went. The Phase 1 instrumentation captures a timestamp at every stage transition and flushes them into one queryable row per task, so questions like "why did this upload take 40s longer than the median?" have a concrete answer.

The instrumentation is **opt-in** — nothing runs in production unless `ENABLE_BENCHMARK_TIMING=true` is set in `.env`. When off, every instrumentation call short-circuits in a cached env-flag check; the overhead is zero.

## Enabling

In `.env`:

```bash
ENABLE_BENCHMARK_TIMING=true
ENABLE_VRAM_PROFILING=true   # optional — adds per-GPU-stage VRAM deltas
```

Restart the services that touch the pipeline:

```bash
./opentr.sh restart-backend
```

No other config is needed. The helper in `backend/app/utils/benchmark_timing.py` caches the env-flag read per process, so there is no per-marker env-read cost.

## Architecture

Every instrumented site calls into the single module `app.utils.benchmark_timing`:

- `mark(task_id, name)` — single marker write to `benchmark:{task_id}` Redis hash.
- `stage(task_id, name)` — context manager that emits `<name>_start` and `<name>_end`.
- `mark_many(task_id, {...})` — batched write (one round-trip).
- `set_context(task_id, {...})` — attach metadata (file_size_bytes, whisper_model, etc.).
- `capture_queue_depth(task_id)` — snapshot Redis queue lengths + in-flight count.
- `mark_cold_start(task_id, worker_key)` — record whether this was the first task on the worker process.
- `fetch_all(task_id)` — read everything back (used by the admin endpoint and finalizer).

Values are UNIX epoch seconds as strings. Durations are computed by subtraction.

The hash has a 24h TTL. At `postprocess_end` the finalizer calls `record_pipeline_timing(...)` in `backend/app/services/pipeline_timing_service.py`, which upserts a row into `file_pipeline_timing` (epoch-ms columns) for durable analysis.

## Marker reference

Markers are grouped by stage. Each entry lists the timestamp key in the Redis hash and the corresponding column in `file_pipeline_timing` (Redis key + `_ms`).

### Stage 0 — Client-side (frontend instrumentation, optional)

| Marker | Description |
|---|---|
| `client_hash_start` / `client_hash_end` | SHA-256 hash compute in the browser. |
| `client_put_start` / `client_put_end` | Browser → MinIO or browser → API PUT. |

### Stage 1-5 — API ingress

The exact set of markers depends on which upload flow the client used.
Every row-level marker is a no-op on the reprocess flow.

**Legacy direct POST** (`backend/app/api/endpoints/files/upload.py::process_file_upload`)
— the client streams bytes to the API, which then PUTs them to MinIO.

| Marker | When |
|---|---|
| `http_request_received` | Just after the handler enters. |
| `http_validation_end` | After streaming magic-byte validation on the first chunk. |
| `http_read_complete` | After the remainder of the body has been buffered. |
| `imohash_start` / `imohash_end` | Around the backend imohash computation (3×128 KiB ranged sample). |
| `minio_put_start` / `minio_put_end` | Around the tuned `upload_file_tuned` call (64 MiB multipart parts). |
| `db_commit_start` / `db_commit_end` | Around the single `db.commit()` that now bundles INSERT + UPDATE. |
| `http_response_end` | Just before `return db_file`. The thumbnail generation has been deferred to a background task so this marker fires right after the commit. |

**Presigned direct-to-MinIO** (`backend/app/api/endpoints/files/prepare_upload.py`
and `complete_upload.py`) — the browser PUTs bytes directly to MinIO, the
API only sees the prepare/complete round-trips.

| Marker | When |
|---|---|
| `http_request_received` | Top of `complete_upload` when the browser reports the PUT is done. |
| `prepare_upload_end` | End of `prepare_upload` — captures the presign+create-row handoff. |
| `imohash_start` / `imohash_end` | Around the `compute_from_minio` call (three ranged reads against MinIO, no full download). |
| `db_commit_start` / `db_commit_end` | Around the status-flip commit. |
| `http_response_end` | Just before the pipeline is dispatched. |
| Client-side (optional) | `client_hash_start/end`, `client_put_start/end` supplied by the frontend Web Worker + direct PUT. |

**URL ingest** (`backend/app/tasks/youtube_processing.py::process_youtube_url_task`)
— yt-dlp downloads on the CPU download worker, uploads into MinIO, then
dispatches the same transcription pipeline.

| Marker | When |
|---|---|
| `http_request_received` | Top of the task — captures the submission-to-processing handoff. |
| `url_download_start` / `url_download_end` | Around the yt-dlp download in `process_media_url_sync`. |
| `imohash_start` / `imohash_end` | Around the post-download `compute_from_stream` call against the local temp file. |
| `minio_put_start` / `minio_put_end` | Around the tuned `upload_file_tuned` call (64 MiB multipart parts). |
| `thumbnail_start` / `thumbnail_end` | Around `_get_thumbnail_with_fallback`. Source-supplied thumbnail (YouTube, Vimeo, …) resolves inline as a single CDN GET; the FFmpeg frame-extraction fallback is deferred to `generate_thumbnail_task`. |
| `db_commit_start` / `db_commit_end` | Around the metadata commit. |
| `http_response_end` | Just before `dispatch_transcription_pipeline`. |

All three flows set the `http_flow` context field to `"legacy"`, `"presigned"`, or `"url"` so you can partition the data in SQL without parsing markers.

### Stage 6-10 — CPU preprocess

| Marker | Where |
|---|---|
| `dispatch_timestamp` | `dispatch_transcription_pipeline` right after `apply_async`. |
| `preprocess_task_prerun` | Top of the preprocess task body — captures queue pickup time. |
| `media_download_start` / `media_download_end` | Around `download_file_to_path` when FFmpeg falls back from the presigned URL. |
| `ffmpeg_start` / `ffmpeg_end` | Around `extract_audio_from_video` / `prepare_audio_for_transcription`. |
| `metadata_start` / `metadata_end` | Around `_extract_metadata_best_effort`. |
| `temp_upload_start` / `temp_upload_end` | Around `upload_temp_audio`. Disappears once the Phase 2 shared-memory handoff lands. |
| `preprocess_end` | End of the preprocess task body (original marker, preserved). |

### Stage 11-12 — GPU (or CPU-lightweight) transcription

| Marker | Notes |
|---|---|
| `gpu_received` | Top of `transcribe_gpu_task` / `transcribe_cpu_task` (original marker). |
| `gpu_task_prerun` | Same position — the two should be within a few ms. |
| `gpu_audio_load_start` / `gpu_audio_load_end` | Around `download_temp_audio`. |
| `gpu_end` | End of the GPU task (original marker). |

The `VRAMProfiler` writes a separate `gpu:profile:{task_id}` blob with per-GPU-phase sub-timings (`model_load_transcriber`, `transcription`, `diarization`, `speaker_assignment`) and VRAM deltas. Read it alongside the benchmark hash for a full GPU breakdown.

### Stage 13-15 — Postprocess + completion

| Marker | Where |
|---|---|
| `postprocess_received` | Top of `finalize_transcription` (original marker). |
| `postprocess_task_prerun` | Same position as above for consistency. |
| `postprocess_end` | In the `finally:` block at the bottom. Fires on both success and error paths. |
| `completion_notified` | Inside `send_completion_notification`, just before the WebSocket event fires. This is the **user-perceived "done"** moment. |

### Stage 17-22 — Async enrichment (fire-and-forget)

These run after `completion_notified` and are what determine "fully indexed" completeness.

| Marker | Where |
|---|---|
| `search_index_start` / `search_index_end` | Around the inline `index_transcript` call in `enrich_and_dispatch`. |
| `search_index_chunks_start` / `search_index_chunks_end` | Around the chunk-level `TranscriptIndexingService` call in `index_transcript_search_task`. |
| `speaker_upsert_start` / `speaker_upsert_end` | Around `extract_speaker_embeddings_task`. Cloud-ASR path only. |
| `waveform_start` / `waveform_end` | In `generate_waveform_task` (runs in parallel with the pipeline). |
| `clustering_start` / `clustering_end` | Around `cluster_speakers_for_file` (not yet wired — reserved). |
| `summary_start` / `summary_end` | Around `generate_summary_task` (not yet wired — reserved, user-triggered). |

### Context fields (not timestamps)

Set via `benchmark_timing.set_context(task_id, {...})`:

- `audio_duration_s` — file duration in seconds.
- `file_size_bytes` — bytes on the wire.
- `content_type` — MIME type.
- `whisper_model`, `asr_provider`, `asr_model` — transcription provider.
- `gpu_device` — which GPU ran the task.
- `http_flow` — `legacy` (direct API POST), `presigned` (browser PUT), or `url` (yt-dlp ingest).
- `queue_depth_at_dispatch` — JSON `{cpu: N, gpu: N, ...}` snapshot.
- `concurrent_files_at_dispatch` — count of in-flight MediaFile rows.
- `cpu_worker_cold`, `gpu_worker_cold`, `cpu_transcribe_worker_cold` — `"true"` when this task was the first on its worker process.
- `retry_count`, `per_retry_timings` — reserved for the Phase 2 resilience PR.

## Derived durations

Computed by `app.services.pipeline_timing_service.build_row_payload`:

- `user_perceived_duration_ms` = `completion_notified` − (`http_request_received` ?? `dispatch_timestamp`).
- `fully_indexed_duration_ms` = `max(search_index_chunks_end, search_index_end, speaker_upsert_end, waveform_end, clustering_end, summary_end, postprocess_end)` − (`http_request_received` ?? `dispatch_timestamp`).

The headline number in reports is `user_perceived_duration_ms`; `fully_indexed_duration_ms` is the secondary number that tells you when the file is fully searchable.

## Reading the data

### Live / ad-hoc

```bash
# Dump the current Redis hash for a specific task
redis-cli -h localhost -p 6379 HGETALL benchmark:<task_id>

# Dump the GPU VRAM profile blob
redis-cli -h localhost -p 6379 GET gpu:profile:<task_id>
```

### Via the admin API

```bash
# Full timing for one task (merges Redis + Postgres views)
curl -H "Authorization: Bearer <admin token>" \
     http://localhost:5174/api/admin/timing/<task_id>

# Recent completed tasks (compact summary)
curl -H "Authorization: Bearer <admin token>" \
     "http://localhost:5174/api/admin/timing-summary/recent?limit=50"
```

### Via SQL (Postgres)

```sql
-- p50/p95 user-perceived wall-clock over the last 24 hours
SELECT
    percentile_cont(0.5)  WITHIN GROUP (ORDER BY user_perceived_duration_ms) / 1000.0 AS p50_s,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY user_perceived_duration_ms) / 1000.0 AS p95_s,
    count(*) AS n
FROM file_pipeline_timing
WHERE created_at > now() - interval '24 hours'
  AND user_perceived_duration_ms IS NOT NULL;

-- Preprocess vs GPU vs postprocess breakdown
SELECT
    count(*) AS n,
    avg((preprocess_end_ms       - dispatch_timestamp_ms) / 1000.0) AS preprocess_s,
    avg((gpu_received_ms         - preprocess_end_ms)     / 1000.0) AS queue_cpu_to_gpu_s,
    avg((gpu_end_ms              - gpu_received_ms)       / 1000.0) AS gpu_s,
    avg((postprocess_received_ms - gpu_end_ms)            / 1000.0) AS queue_gpu_to_post_s,
    avg((postprocess_end_ms      - postprocess_received_ms) / 1000.0) AS postprocess_s
FROM file_pipeline_timing
WHERE completion_notified_ms IS NOT NULL
  AND created_at > now() - interval '7 days';

-- Find tasks where the HTTP ingress took > 10 s
SELECT task_id, file_id,
       (http_response_end_ms - http_request_received_ms) / 1000.0 AS http_total_s,
       (minio_put_end_ms     - minio_put_start_ms)       / 1000.0 AS minio_s,
       (thumbnail_end_ms     - thumbnail_start_ms)       / 1000.0 AS thumb_s,
       file_size_bytes
FROM file_pipeline_timing
WHERE (http_response_end_ms - http_request_received_ms) > 10000
ORDER BY http_total_s DESC
LIMIT 20;

-- Cold-start filter (drop outliers from distribution analysis)
SELECT avg(user_perceived_duration_ms / 1000.0) AS avg_s, count(*) AS n
FROM file_pipeline_timing
WHERE gpu_worker_cold = 'false'
  AND created_at > now() - interval '24 hours';
```

## Benchmarking recipes

### Audit a single reprocess (the legacy baseline)

```bash
python scripts/benchmark_e2e.py --file-uuid <uuid> --detailed
```

Produces the same CSV + report as before, plus the extended stage columns.

### Audit a fresh upload (full ingress → indexed path)

```bash
python scripts/benchmark_e2e.py \
    --mode upload --fixture-file /path/to/sample.mp4 \
    --iterations 3 --detailed
```

Each iteration creates a new MediaFile. After the run you can inspect the `file_pipeline_timing` rows by their `file_uuid` (written to the CSV).

### Contention test under load

```bash
python scripts/benchmark_concurrent_uploads.py \
    --fixture-file /path/to/sample.mp4 --n 8
```

Launches 8 uploads in parallel, waits for all to finish, then reads the DB for p50/p95 of every stage. The plan's CPU-queue head-of-line-blocking hypothesis is visible in the `queue_cpu_to_gpu` and `queue_gpu_to_post` rows.

### Regression guard

Record a baseline run with the pre-change code, then after each PR rerun the same fixture and diff the two reports. Changes should move the targeted stage and leave every other stage within 10%.

### A/B one change without building two stacks — `scripts/diar-native-handoff-ab.py`

The recipes above compare two *builds*, which is the honest way to measure a whole release but
a poor way to attribute one change: every unrelated difference between the builds lands in the
delta. Where a change can be selected at runtime, prefer a controlled A/B in **one process, on
one GPU, over one decoded waveform**.

`scripts/diar-native-handoff-ab.py` does this for issue #661's E2 path handoff, because
`NativeSpeakerDiarizer.diarize` picks the path from its argument:

```bash
W=opentranscribe-celery-worker
docker cp scripts/diar-native-handoff-ab.py $W:/tmp/
docker cp benchmark/diarization-boundary/karpathy/karpathy_kwSVtQ7dziU/karpathy_10m.wav $W:/tmp/
docker exec $W python3 /tmp/diar-native-handoff-ab.py --audio /tmp/karpathy_10m.wav --reps 5
```

- `wav_path=<WAV on the shared volume>` → path handoff (post-E2)
- `wav_path=None` → `_post_own_copy` (pre-E2 behaviour)

`None` cannot satisfy `reused`'s first clause, so the arms are *structurally* different — they
cannot silently converge the way a misread feature flag can.

Three things it does that a naive timing loop does not, each because the naive version is
wrong:

- **Arms ALTERNATE (ABAB…), never AAA-then-BBB.** The GPU clocks down as it heats and the
  sidecar's ORT arena warms, so a blocked design charges one arm's drift to the other's
  difference.
- **A warm-up per arm is discarded** — the first call pays ORT session init and CUDA context
  setup, which would otherwise land entirely on whichever arm ran first.
- **It refuses to run rather than measure nothing.** If the WAV fails the engine's own
  `_path_is_on_shared_volume` predicate, arm A takes the own-copy path *too*, both arms become
  the same code, and the delta is ~0 — which reads exactly like "the optimisation does
  nothing". That is checked and made fatal *before* any timing, not inferred from the numbers
  afterwards. It also compares segment counts across arms and warns if they differ, since a
  different result means something other than the handoff changed.

Measured 2026-09-04 (RTX 3080 Ti, diar-server 0.3.1, 600 s / 88 segments, n=5 per arm):

| arm | median | p95 | min | max |
|---|---|---|---|---|
| reused (post-E2) | 3.743 s | 3.761 s | 3.582 s | 3.949 s |
| own_copy (pre-E2) | 3.845 s | 3.848 s | 3.747 s | 4.038 s |

**E2 saves ~0.10 s per diarization (2.7%)**, with the fast path ahead in 5 of 5 paired reps and
identical segment counts in both arms.

⚠️ **Read that number for what it is.** It is E2's contribution *to the diarization call*, not
the E0–E5 chain's effect on `user_perceived_duration_ms`: E3 (decode once) and E4 (the lazy
scaled reader) act in preprocessing and transcription, which this harness does not exercise. And
#661's larger claim was never primarily about wall clock — it was about not writing and reading
the whole signal again. That I/O saving is one full write plus one full read of the 16 kHz mono
WAV per job (19.2 MB for this 600 s clip) and scales linearly with duration; 2.7% is what that
is worth in *time* on a fast NVMe host, and would be worth more on slower or contended storage.
Quote the command, not the table — a figure transcribed into prose rots.

## Interpretation notes

- **`user_perceived_duration_ms` is the headline number.** It's what the browser user experiences.
- **`fully_indexed_duration_ms` is the secondary number.** Search and AI enrichment may lag `completion_notified` by 1-5 s. Under heavy load the gap can be much larger.
- **Cold starts are fine to report separately.** The first task after a worker restart carries a 3-10 s model-load penalty. Filter with `cpu_worker_cold = 'false' AND gpu_worker_cold = 'false'` for steady-state numbers.
- **Clock skew is not a concern.** All Docker containers share the host kernel clock, so cross-process timestamps are directly subtractable.
- **Reprocess mode is missing the HTTP stages.** `http_request_received` and its siblings are null for reprocess runs — the headline number falls back to `completion_notified - dispatch_timestamp`.
- **`temp_upload_*` and `gpu_audio_load_*` are transient.** They disappear once the Phase 2 shared-memory handoff lands; plan queries that don't rely on those two being present indefinitely.

## Debugging

- If no Redis hash exists, check that `ENABLE_BENCHMARK_TIMING=true` is set in `.env` *and* that the container you're testing has been restarted since the change.
- If a row is missing from `file_pipeline_timing` but the Redis hash exists, check the backend logs for `Failed to persist pipeline timing row`. The usual culprit is a mismatched `file_id` (task ran before the file record was created — never happens in practice, but worth eliminating).
- If markers are present in Redis but not in the CSV, double-check you're running the latest `scripts/benchmark_e2e.py` — `calculate_stages` was extended alongside the new markers.

## Phase 2 landings (reference)

All Phase 2 PRs are now merged. Behaviour changes to be aware of:

- **Presigned upload flow** (PR #2): `minio_put_*` and the `http_read_*` ingress markers drop out of the API-side hash for the presigned path. `client_hash_*`, `client_put_*`, and `prepare_upload_end` take their place. `http_flow` flips from `legacy` to `presigned`.
- **URL ingest flow** (PR #9): adds `url_download_*`, and reuses `imohash_*`, `minio_put_*`, `thumbnail_*`, `db_commit_*`. `http_flow` is `url`. `thumbnail_*` wraps the source-thumbnail-or-defer helper — a cheap inline path or an async dispatch, not a long inline FFmpeg run.
- **Shared-memory handoff** (PR #4): `temp_upload_*` and `gpu_audio_load_*` become sub-millisecond on same-host deployments (atomic rename + hard-link into the scratch volume). They stay larger in multi-host deployments where the MinIO fallback path is taken.
- **Parallelization / deferral** (PR #5): `thumbnail_*` on the legacy flow is now dispatched asynchronously to `generate_thumbnail_task` — the HTTP handler returns before FFmpeg runs. `search_index_chunks_*` is the primary chunk-indexing marker; `search_index_*` wraps the full-doc index which also runs on the embedding worker now.
- **Resilience** (PR #8): `pipeline_error_end` fires when `on_pipeline_error` runs; `retry_count` and `per_retry_timings` accumulate per Celery retry so analysis can separate first-try wall-clock from retry-inflated wall-clock.
- **Parallel preprocess + diarizer overlap** (PR #9): `ffmpeg_start/end` and `metadata_start/end` now overlap on videos (the two subprocesses run on a `ThreadPoolExecutor`). The diarizer model load absorbs into the transcription window when diarization is enabled — this is NOT a separately-marked stage; it shows up as reduced wall-clock between `model_load_transcriber` and the next profiler snapshot.

This document stays authoritative — future changes should update the tables above rather than appending version-specific notes.
