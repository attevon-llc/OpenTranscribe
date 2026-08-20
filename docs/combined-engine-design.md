# OpenTranscribe Combined Engine — Design Reference

This document describes the `backend/app/transcription/engine/` package introduced in Phase 1a/1b
of the GPU pipeline optimization work. It is intended for contributors maintaining or extending the
engine after the initial implementation.

---

## Why the Engine Exists

The pre-engine architecture ran the entire transcription pipeline on the GPU Celery worker:

1. Download original media from MinIO (I/O, 5–60 s)
2. Extract audio via FFmpeg (CPU, 5–30 s)
3. Upload 16 kHz WAV back to MinIO (I/O, 30–60 s)
4. Download the WAV again (I/O, 2–5 s)
5. Transcribe (GPU, 30–600 s)
6. Diarize (GPU/CPU, 10–120 s)
7. Assign speakers + dedup (CPU, 1–5 s)
8. Save to DB + OpenSearch (I/O, 1–10 s)

Steps 1–4 and 7–8 are pure I/O or CPU work, yet they were running on the GPU queue, blocking the
GPU worker from accepting the next task. On cloud deployments where GPU instances cost 5–10× more
than CPU instances, this idle GPU time is direct dollar waste.

The engine splits the pipeline into three stages, each running on the appropriate queue:

```
[CPU worker]  Stage 1: preprocess  →  [GPU worker]  Stage 2: GPU inference  →  [CPU worker]  Stage 3: finalize
preprocess_for_transcription()           transcribe_gpu_task()                    finalize_transcription()
```

---

## Package Layout

```
backend/app/transcription/engine/
├── __init__.py          # Re-exports Engine, EngineConfig, JobSpec, JobResult
├── engine.py            # Engine class — orchestrator, public API
├── config.py            # EngineConfig — wraps TranscriptionConfig, adds engine-level settings
├── job.py               # JobSpec / PreprocessResult / RawInferenceResult / JobResult dataclasses
├── stages.py            # _GpuStage / _PreprocessStage / _GpuRawStage / _FinalizeStage
├── audio_loader.py      # Shared-volume WAV read/write helpers
├── progress.py          # ProgressCallback type + adapt_legacy() shim
├── metrics.py           # MetricsCollector — Redis-backed counters
└── backends/
    ├── __init__.py
    ├── protocols.py     # TranscriberBackend / DiarizerBackend Protocol interfaces
    ├── transcribers/
    │   ├── faster_whisper_backend.py
    │   ├── whisperx_backend.py
    │   └── cloud_backend.py
    └── diarizers/
        └── pyannote_backend.py
```

---

## Key Abstractions

### `Engine`

The main entry point. One instance per worker process; stateless after construction (all per-task
state lives in the dataclasses).

```python
# Phase 1a — combined path (byte-identical to TranscriptionPipeline)
engine = Engine(EngineConfig.from_environment())
result = engine.process(JobSpec(audio_path=..., task_id=...))

# Phase 1b — split-stage path
pre    = engine.run_preprocess(spec)
raw    = engine.run_gpu_stage(pre)
result = engine.run_cpu_finalize(raw)
```

### `EngineConfig`

Wraps `TranscriptionConfig` and adds engine-level settings:

| Field | Default | Purpose |
|-------|---------|---------|
| `shared_volume_path` | `/tmp/transcription` | Directory for the shared-volume WAV |
| `transcriber_backend` | `faster_whisper` | Backend registry key |
| `diarizer_backend` | `native` | Backend registry key (`native` primary, `pyannote` failover) |
| `gpu_split` | `False` | Multi-GPU split (Phase 4) |
| `precompute_vad` | `False` | Pre-run Silero VAD in Stage 1 (Phase 3) |

`EngineConfig.to_snapshot()` serializes to a plain `dict` for cross-stage Celery handoff.
`EngineConfig.from_snapshot(d)` reconstructs from that dict. This ensures Stage 2 and Stage 3
see exactly the same config without re-reading the database or environment.

### Stage Dataclasses (`job.py`)

All three handoff types are JSON-serializable:

| Class | Produced by | Consumed by | Key fields |
|-------|-------------|-------------|-----------|
| `JobSpec` | Caller | Stage 1 | `audio_path`, `task_id`, `file_id`, `user_id` |
| `PreprocessResult` | Stage 1 | Stage 2 | `local_wav_path`, `minio_temp_object`, `audio_duration_s`, `config_snapshot` |
| `RawInferenceResult` | Stage 2 | Stage 3 | `raw_segments`, `diarize_records`, `overlap_info`, `native_speaker_embeddings`, `config_snapshot` |
| `JobResult` | Stage 3 | Caller | `segments`, `language`, `overlap_info`, `native_speaker_embeddings` |

`PreprocessResult.serialize()` / `deserialize()` and `RawInferenceResult.serialize()` /
`deserialize()` handle numpy array ↔ list conversion and nested type restoration automatically.

---

## Shared-Volume WAV (Opt-3A)

Stage 1 writes the decoded 16 kHz WAV to a Docker-named volume (`transcription-temp`) mounted at
`ENGINE_SHARED_VOLUME_PATH` (default `/tmp/transcription`) on both CPU and GPU workers. Stage 2
`mmap`-loads the file via `load_from_shared_volume()`, avoiding a MinIO round-trip (saves 2–5 s).

Fallback: if the write fails (permissions, volume not mounted), `local_wav_path` is set to `""`
and Stage 2 falls back to downloading from MinIO. No task failure occurs.

The volume definition in `docker-compose.yml`:

```yaml
volumes:
  transcription-temp:
    driver: local
```

Mounted in both `celery-worker` (GPU) and `celery-cpu-worker` (CPU) services.

---

## Waveform Double-Decode Fix

Before Phase 1b, the waveform task re-downloaded the original media file and ran FFmpeg to decode
audio — even though the CPU preprocess stage had already decoded the same audio to a 16 kHz WAV.

The fix (Option B, numpy resample):

1. `preprocess_for_transcription()` passes `local_wav_path` to `_dispatch_waveform_if_missing()`
2. `generate_waveform_task()` calls `WaveformGenerator.generate_from_16khz_wav(local_wav_path)` —
   a numpy resample path using `scipy.signal.resample_poly(441:320)` that converts 16 kHz → 22050 Hz
   without running FFmpeg
3. Falls back to the MinIO download + FFmpeg path if the shared-volume WAV is missing or resample
   fails (e.g. scipy unavailable, corrupted WAV)

---

## Stages (`stages.py`)

### `_GpuStage` (Phase 1a)

Combined stage that replicates `TranscriptionPipeline.process()` exactly. Used by `Engine.process()`
for the parity gate and backward compatibility.

### `_PreprocessStage` (Phase 1b)

Runs on the CPU queue. Decodes audio via `faster_whisper.audio.decode_audio()` to float32 at
16 kHz, writes the WAV to the shared volume, and optionally runs Silero VAD (Phase 3).

### `_GpuRawStage` (Phase 1b)

Runs on the GPU queue. Loads audio from the shared volume via `mmap`, runs transcription and
diarization, and returns raw results without speaker assignment. Serializes `DiarizeResult` to
`diarize_records` (list of dicts) at the boundary so Stage 3 doesn't need numpy arrays.

### `_FinalizeStage` (Phase 1b)

Runs on the CPU queue. Reconstructs `DiarizeResult` from `diarize_records`, runs optional dedup
via `clean_segments()`, and calls `assign_speakers()` for the final segment-speaker mapping.

---

## Metrics

`MetricsCollector` writes Redis counters under `engine:metrics:<hostname>`:

| Key | Type | Description |
|-----|------|-------------|
| `tasks_submitted` | INCR | Total jobs submitted |
| `tasks_completed` | INCR | Total jobs completed (success) |
| `tasks_failed` | INCR | Total jobs that errored |
| `stage_timings:<stage>` | LPUSH (list) | Per-stage wall times (seconds, last 100) |
| `gpu_ready_queue_depth` | GET/SET | Current GPU-queue depth (set by CPU worker after Stage 1) |

---

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `ENGINE_TRANSCRIBER_BACKEND` | `faster_whisper` | Transcriber backend key |
| `ENGINE_DIARIZER_BACKEND` | `native` | Diarizer backend key (`native` primary, `pyannote` failover) |
| `ENGINE_GPU_SPLIT` | `false` | Enable Phase 4 multi-GPU split |
| `ENGINE_SHARED_VOLUME_PATH` | `/tmp/transcription` | Shared-volume mount point |
| `ENGINE_PRECOMPUTE_VAD` | `false` | Pre-run Silero VAD in Stage 1 (Phase 3) |

---

## Parity Gate

`scripts/benchmark_engine_compare.py` runs both `TranscriptionPipeline` and `Engine.process()` on
the same audio files and asserts:

- `segments`: byte-equal (JSON-stable)
- `language`: identical string
- `overlap_info`: equal (absent/empty both treated as `{}`)
- `native_speaker_embeddings`: within 1e-6 absolute tolerance

Exit code 0 if all checks PASS or SKIP, non-zero if any FAIL.

---

## Extending the Engine

### Adding a new transcriber backend

1. Create `engine/backends/transcribers/<name>_backend.py` implementing `TranscriberBackend` protocol
2. Register it in `engine/backends/__init__.py`
3. Set `ENGINE_TRANSCRIBER_BACKEND=<name>` in `.env`

### Adding a new diarizer backend

Same pattern under `engine/backends/diarizers/`.

### Adding a new stage optimization

Phase 3 optimizations (VAD precompute, adaptive CPU concurrency) are feature-flagged via
`EngineConfig.precompute_vad` and env vars. The pattern is:

1. Add a field to `EngineConfig`
2. Check it in the appropriate stage's `run()` method
3. Add it to `to_snapshot()` / `from_snapshot()` so the config survives cross-stage serialization
4. Gate behind an env var (`ENGINE_PRECOMPUTE_VAD=true`)

---

## Phase Roadmap

| Phase | Status | Description |
|-------|--------|-------------|
| 1a | Shipped | Combined-entry engine, byte-equal parity gate |
| 1b | Shipped | Split-stage methods, shared-volume WAV, waveform double-decode fix |
| 2 | Pending | Baseline benchmark sweep (CSVs to `docs/engine-benchmark-results/`) |
| 3 | Pending | Optional: VAD precompute, queue split, adaptive CPU concurrency |
| 4 | Pending | Multi-GPU split (`gpu-transcribe` / `gpu-diarize` queues) |
| 5 | Deferred | Evaluate after 4–8 weeks production data: extract to standalone repo? |
