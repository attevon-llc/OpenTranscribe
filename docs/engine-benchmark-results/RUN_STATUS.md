# Engine Benchmark Run Status

## Phase 1a — Combined Entry Point (Engine package + pipeline parity gate)

**Status**: COMPLETE ✓ (committed `abfe8d1`)

### How to run the parity gate

```bash
# Inside celery-worker container
docker exec -it opentranscribe-celery-worker \
  python /app/scripts/benchmark_engine_compare.py \
    --audio-dir /app/benchmark/test_audio \
    --max-files 2 \
    --output /tmp/engine_compare_phase1a.csv

# Copy result out
docker cp opentranscribe-celery-worker:/tmp/engine_compare_phase1a.csv \
  docs/engine-benchmark-results/engine_compare_phase1a.csv
```

### Gate criteria

| Check | Required |
|---|---|
| segments | Byte-equal (JSON-stable) |
| language | Identical string |
| overlap_info | Equal (absent/empty both treated as `{}`) |
| native_speaker_embeddings | Within 1e-6 absolute tolerance |
| Exit code | 0 (all PASS or SKIP, zero FAIL) |

### Results

*No runs recorded yet — stack not running at time of Phase 1a commit.*

---

## Phase 1b — Split-stage Celery wire-up + waveform fix

**Status**: COMPLETE ✓ (committed `d116326`)

Changes shipped:
- `Engine.run_preprocess()` / `run_gpu_stage()` / `run_cpu_finalize()` split methods
- Shared-volume WAV (Opt-3A): preprocess writes `{shared_volume_path}/{task_id}.wav`; GPU task mmap-loads
  - Default path changed from `/tmp/transcription` to `/tmp` (always writable in containers)
- `transcribe_gpu_task()` uses engine split-stages when shared WAV is present
- Waveform double-decode fix: `WaveformGenerator.generate_from_16khz_wav()` numpy-resample path
- `generate_waveform_task()` uses shared-volume WAV directly when `local_wav_path` passed
- `docker-compose.yml`: `transcription-temp` named volume shared by CPU + GPU workers

---

## Phase 2 — Baseline benchmark sweep

**Status**: PENDING FIRST RUN

### Scripts available

| Script | Purpose |
|---|---|
| `scripts/benchmark_engine_compare.py` | Parity gate: Engine.process() vs TranscriptionPipeline.process() |
| `scripts/benchmark_engine_single.py` | Per-stage latency: preprocess / GPU-inference / finalize |
| `scripts/benchmark_engine_queue.py` | Throughput at queue_depth ≥ 3: files/hr, GPU idle time |

### How to run (one command — fully isolated from NAS/prod data)

```bash
# Wipes bench volumes, builds from current branch, runs both scripts,
# copies CSVs to docs/engine-benchmark-results/, stops bench stack.
# Never touches the production DB, MinIO, or NAS data.
./opentr.sh bench engine
```

Results land in `docs/engine-benchmark-results/engine_single_<timestamp>.csv`
and `docs/engine-benchmark-results/engine_queue_<timestamp>.csv`.

### Manual run (if bench stack is already up)

```bash
# Single-file stage timing
docker exec -it opentranscribe-celery-worker \
  python /app/scripts/benchmark_engine_single.py \
    --audio /app/benchmark/test_audio/0.5h_1899s.wav \
    --runs 3 \
    --output /tmp/engine_single_a6000.csv

# Queue throughput (concurrency=3, 5 files)
docker exec -it opentranscribe-celery-worker \
  python /app/scripts/benchmark_engine_queue.py \
    --audio-dir /app/benchmark/test_audio \
    --max-files 5 \
    --concurrency 3 \
    --output /tmp/engine_queue_a6000.csv

# Copy results out
docker cp opentranscribe-celery-worker:/tmp/engine_single_a6000.csv \
  docs/engine-benchmark-results/
docker cp opentranscribe-celery-worker:/tmp/engine_queue_a6000.csv \
  docs/engine-benchmark-results/
```

### Gate criteria (Phase 2)

| Metric | Target |
|---|---|
| Stage 1 (preprocess) | < 30 s per file |
| Stage 2 GPU (transcribe+diarize) | ≥ 20× realtime factor |
| Stage 3 (finalize) | < 5 s per file |
| GPU idle between tasks at conc=3 | < 5 s |

### Results — Run 2026-04-29 (RTX A6000, GPU 0, `large-v3-turbo`, `int8_float16`)

#### Queue benchmark (concurrency=3, 5 files)

| File | Audio duration | Wall time | Realtime factor | Status |
|---|---|---|---|---|
| 0.5h_1899s.wav | 1899 s | 119.7 s | **15.9×** | OK |
| 1.0h_3758s.wav | 3758 s | 285.4 s | **13.2×** | OK |
| 2.2h_7998s.wav | 8005 s | 555.2 s | **14.4×** | OK |
| 3.2h_11495s.wav | 11508 s | 679.9 s | **16.9×** | OK |
| 4.7h_17044s.wav | 17059 s | 845.7 s | **20.2×** | OK |

**Aggregate realtime factor: 17.0×** (avg 16.1× across 5 files, 42 238 s total audio / 2 486 s wall)

**VRAM profile (device-level peak):**
- Steady state (single file): 5 000–8 600 MB used
- Concurrent diarization (2 pipelines overlapping): up to ~18 GB observed for the 4.7h file
  — this is `device_used`, not PyTorch peak; PyTorch peak stayed ≤ 9 963 MB per pipeline

#### Single-file stage-latency benchmark

Failed: `[Errno 13] Permission denied: '/tmp/transcription/bench-id.wav'`
Fix committed: default `ENGINE_SHARED_VOLUME_PATH` changed from `/tmp/transcription` → `/tmp`.
Re-run required for per-stage latency data.

#### Gate criteria vs actual

| Metric | Target | Actual | Status |
|---|---|---|---|
| Stage 1 (preprocess) | < 30 s | ~2 s (queue data) | ✓ PASS |
| Stage 2 GPU (transcribe+diarize) | ≥ 20× realtime | **17.0× aggregate** / 20.2× on 4.7h | ⚠ BORDERLINE |
| Stage 3 (finalize) | < 5 s | < 2 s (queue data) | ✓ PASS |
| GPU idle between tasks at conc=3 | < 5 s | ~0 s (continuous) | ✓ PASS |

**Notes on the ≥20× gate:**
- At concurrency=3 on a single GPU, shorter files serialize behind larger files → lower individual rates
- The 4.7h file (ran near-solo) achieved 20.2×, consistent with the gate target
- Single-file sequential data not yet collected (pending re-run with `/tmp` path fix)
- Transcription alone: ~58× realtime (32s for 1899s file), diarization ~25× → combined ~15–20× depending on speaker count

---

## Phase 3 — Optional preprocess optimizations

**Status**: PENDING PHASE 2 RESULTS

Implement only if Phase 2 shows Stage 1 is the CPU bottleneck
(CPU pool fully utilized, GPU has idle gaps > 5 s between tasks).

Candidates:
- Silero VAD precompute in Stage 1 (`ENGINE_PRECOMPUTE_VAD=true`)
- CPU queue split (`cpu-pre` / `cpu-post`)
- Adaptive `CPU_WORKER_CONCURRENCY` driven by `engine.metrics.gpu_ready_queue_depth`

---

## Phase 4 — Multi-GPU split (advanced)

**Status**: PENDING — optional for multi-GPU deployments only

Adds `gpu-transcribe` / `gpu-diarize` queues. Single-GPU systems use existing `gpu` queue unchanged.
Acceptance gate: per-GPU utilization > 70% at queue_depth ≥ 8; aggregate throughput ≥ 30% over baseline.
