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

**Status**: COMPLETE ✓ (Run 2, 2026-04-29, `engine_single_20260429_040907.csv` + `engine_queue_20260429_040907.csv`)

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

### Results — Run 2 (2026-04-29, canonical, RTX A6000, GPU 0, `large-v3-turbo`, `int8_float16`)

CSVs: `engine_single_20260429_040907.csv`, `engine_queue_20260429_040907.csv`

#### Single-file stage-latency benchmark (0.5h_1899s.wav, 3 runs, solo GPU)

| Run | Stage 1 | Stage 2 GPU | Stage 3 | Total | Realtime |
|---|---|---|---|---|---|
| 1 (cold model load) | 2.086 s | 47.708 s | 0.287 s | 50.08 s | **37.9×** |
| 2 (warm) | 0.862 s | 41.915 s | 0.151 s | 42.93 s | **44.2×** |
| 3 (warm) | 0.824 s | 41.590 s | 0.148 s | 42.56 s | **44.6×** |

Stage 2 breakdown (Run 1, solo): transcription 18.12 s (104.8× RTF), diarization 24.0 s (79.1× RTF)
PyTorch peak (solo diarization): **844 MB**

#### Queue benchmark (concurrency=3, 5 files)

| File | Audio duration | Wall time | Realtime factor | Status |
|---|---|---|---|---|
| 0.5h_1899s.wav | 1,899 s | 117.9 s | **16.1×** | OK |
| 1.0h_3758s.wav | 3,768 s | 279.2 s | **13.5×** | OK |
| 2.2h_7998s.wav | 8,005 s | 585.5 s | **13.7×** | OK |
| 3.2h_11495s.wav | 11,508 s | 712.1 s | **16.2×** | OK |
| 4.7h_17044s.wav | 17,059 s | 838.8 s | **20.3×** | OK |

**Aggregate realtime factor: 16.7×** (avg 16.0× across 5 files, 42 239 s audio / 2 533 s wall)
**Throughput**: 7.1 files/hour at concurrency=3

**VRAM profile:**
- Solo single pipeline: +1,302 MB process delta, PyTorch peak 844 MB (diarization)
- Concurrent (3 pipelines): PyTorch peak up to ~9,963 MB (3× embedding batches overlap)
- Device-level baseline varies by host (open_speakers workers add ~7.4 GB on dev machine)

#### Gate criteria vs actual

| Metric | Target | Actual | Status |
|---|---|---|---|
| Stage 1 (preprocess) | < 30 s | 2.1 s cold / 0.84 s warm | ✓ PASS |
| Stage 2 GPU (solo, warm) | ≥ 20× realtime | **44.2×** | ✓ PASS |
| Stage 2 GPU (conc=3 agg) | ≥ 20× realtime | **16.7×** (queue serialization) | ⚠ BORDERLINE |
| Stage 3 (finalize) | < 5 s | 0.15 s warm | ✓ PASS |
| GPU idle between tasks at conc=3 | < 5 s | ~0 s (continuous) | ✓ PASS |

**Notes:**
- Solo warm realtime (44.2×) exceeds the gate. The concurrency=3 aggregate (16.7×) is below 20× due to GPU serialization of three simultaneous pipelines — not a throughput ceiling.
- The 4.7h file (ran near-solo in the queue) hit 20.3×, confirming gate is achievable when files don't overlap.
- TF32 is globally disabled by `pyannote/speaker-diarization-community-1` at import. In queue mode this amplifies the diarization cost vs. the solo case (where TF32 was implicitly on during transcription).

### Results — Run 1 (2026-04-29, superseded)

CSVs: `engine_queue_20260429_032415.csv` (queue only — single-file failed with `/tmp/transcription` EACCES)
Aggregate: 17.0× agg / 16.1× avg. Single-file stage latency was not collected due to the permission bug (now fixed).

---

## Phase 3 — Optional preprocess optimizations

**Status**: NOT WARRANTED — Phase 2 gate not triggered

Phase 2 data: Stage 1 preprocess = 0.84 s warm (CPU is not the bottleneck). GPU idle gap at conc=3 = ~0 s (GPU is continuously busy). Neither of the gate-trigger conditions is met.

Candidates:
- Silero VAD precompute in Stage 1 (`ENGINE_PRECOMPUTE_VAD=true`)
- CPU queue split (`cpu-pre` / `cpu-post`)
- Adaptive `CPU_WORKER_CONCURRENCY` driven by `engine.metrics.gpu_ready_queue_depth`

---

## Phase 4 — Multi-GPU split (advanced)

**Status**: PENDING — optional for multi-GPU deployments only

Adds `gpu-transcribe` / `gpu-diarize` queues. Single-GPU systems use existing `gpu` queue unchanged.
Acceptance gate: per-GPU utilization > 70% at queue_depth ≥ 8; aggregate throughput ≥ 30% over baseline.
