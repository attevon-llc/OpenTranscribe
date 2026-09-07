# GPU Processing Performance Benchmark Guide

## Overview

Benchmark end-to-end transcription pipeline performance, test GPU concurrency limits, and project total reprocessing time for all files in the database.

> **v0.4.0 pipeline**: Benchmarks measure the full native pipeline (faster-whisper `BatchedInferencePipeline` + PyAnnote v4 direct). The default model is `large-v3-turbo`. Baseline numbers from completed benchmarks: **40.3x realtime** single-file, **54.6x peak** at concurrency=8, perfect linear scaling 1x–12x. See `docs/BENCHMARK_RESULTS.md` for detailed results.

### Scripts

| Script | Purpose |
|--------|---------|
| `scripts/benchmark_projection.py` | DB analysis: total audio hours, file distribution, processing projections |
| `scripts/benchmark_e2e.py` | Single-file benchmark: per-stage timing, VRAM profile, realtime factor |
| `scripts/benchmark_parallel.py` | Parallel benchmark: batch scaling, throughput, VRAM monitoring |

### Hardware

| GPU | Device | VRAM | Role |
|-----|--------|------|------|
| RTX A6000 | 0 | 49GB | Available for transcription |
| RTX 3080 Ti | 1 | 12GB | Available for transcription |
| RTX A6000 | 2 | 49GB | Running LLM (or available) |

---

## Phase A: Environment Setup

### A1. Stop all services

```bash
./opentr.sh stop
```

- [ ] All services stopped

### A2. Free the target GPU

```bash
nvidia-smi
```

Check that nothing is using the GPU you want to benchmark on. If vLLM or other processes are running on it, stop them.

```bash
# Example: stop a vLLM container if on device 0
# docker stop <vllm-container-name>
```

- [ ] Target GPU shows ~0 MB used (idle)

### A3. Configure .env for benchmarking

Edit `.env` and set these values:

```bash
# ── Pick ONE GPU for isolated testing ──
# A6000 (49GB) recommended for concurrency testing
GPU_DEVICE_ID=0

# ── Disable GPU scaling (single worker for baseline) ──
GPU_SCALE_ENABLED=false

# ── Single worker, sequential processing ──
GPU_CONCURRENT_REQUESTS=1

# ── Enable profiling ──
ENABLE_BENCHMARK_TIMING=true
ENABLE_VRAM_PROFILING=true
```

- [ ] GPU_DEVICE_ID set to target GPU
- [ ] GPU_SCALE_ENABLED=false
- [ ] GPU_CONCURRENT_REQUESTS=1
- [ ] ENABLE_BENCHMARK_TIMING=true
- [ ] ENABLE_VRAM_PROFILING=true

### A4. Start in dev mode (single GPU, no scale)

```bash
./opentr.sh start dev
```

Wait for healthy:

```bash
./opentr.sh status
```

Verify the GPU worker started on the right device:

```bash
docker compose logs celery-worker 2>&1 | grep -i "preload\|cuda\|model\|device" | tail -20
```

- [ ] All services healthy
- [ ] GPU worker loaded models on correct device

---

## Phase B: Database Analysis

### B1. Activate venv

```bash
source backend/venv/bin/activate
```

### B2. Run projection script

```bash
python scripts/benchmark_projection.py
```

This shows:
- Total completed files and audio hours
- Duration distribution (how many 3hr+ files exist)
- Historical processing stats
- Preliminary projections (will be refined after benchmarking)

- [ ] Projection script ran successfully
- [ ] Record: **Total files**: ___
- [ ] Record: **Total audio hours**: ___
- [ ] Record: **Files >= 3hr**: ___

### B3. Pick a benchmark file

Find a 3hr+ file to use as the reference:

```bash
docker exec opentranscribe-postgres psql -U postgres -d opentranscribe -c \
  "SELECT uuid, filename, round(duration/3600.0, 1) AS hours
   FROM media_file
   WHERE status='completed' AND duration >= 10800
   ORDER BY duration DESC LIMIT 5;"
```

- [ ] Record: **Benchmark file UUID**: ___
- [ ] Record: **Benchmark file duration**: ___ hours

---

## Phase C: Single-File Benchmark

### C1. Find your Redis password

```bash
grep REDIS_PASSWORD .env | head -1
```

### C2. Run 3-iteration benchmark

Replace `<UUID>` and `<REDIS_PASSWORD>` below:

```bash
python scripts/benchmark_e2e.py \
  --file-uuid <UUID> \
  --iterations 3 \
  --detailed \
  --redis-url "redis://:<REDIS_PASSWORD>@localhost:5177/0"
```

Expected duration: ~15-30 min total (3 passes of a 3hr file).

- [ ] All 3 iterations completed successfully
- [ ] VRAM profile data was captured (not "None")
- [ ] CSV written to `benchmark_results.csv`

### C3. Record results

From the detailed report output:

| Metric | Value |
|--------|-------|
| Preprocess (mean) | ___ s |
| Queue: CPU -> GPU (mean) | ___ s |
| GPU Transcribe+Diarize (mean) | ___ s |
| Queue: GPU -> Post (mean) | ___ s |
| Total (mean) | ___ s |
| Whisper transcription | ___ s (___ % of GPU) |
| PyAnnote diarization | ___ s (___ % of GPU) |
| Realtime factor (GPU) | ___ x |
| Realtime factor (total) | ___ x |
| Peak device VRAM | ___ MB |
| Segments | ___ |
| Speakers | ___ |

---

## Phase D: Concurrency Testing

### D1. Test parallel file dispatch (sequential GPU)

With `GPU_CONCURRENT_REQUESTS=1`, dispatching multiple files means they queue on the GPU and process one at a time. This tests queue overhead and CPU parallelism.

```bash
python scripts/benchmark_parallel.py \
  --batches 1,2,3,4 \
  --gpu-id 0 \
  --min-duration 10800 \
  --output benchmarks/
```

- [ ] Batch 1 completed
- [ ] Batch 2 completed
- [ ] Batch 3 completed
- [ ] Batch 4 completed
- [ ] CSV reports written to `benchmarks/`

### D2. Test actual GPU concurrency (shared VRAM)

Stop services, increase concurrency, restart:

```bash
./opentr.sh stop
```

Edit `.env`:

```bash
GPU_CONCURRENT_REQUESTS=2
```

```bash
./opentr.sh start dev
```

Wait for healthy, then:

```bash
python scripts/benchmark_parallel.py \
  --batches 2 \
  --gpu-id 0 \
  --min-duration 10800 \
  --output benchmarks/conc2/
```

- [ ] Concurrency=2 completed without OOM
- [ ] Record: **Throughput at conc=2**: ___ x

If successful, try concurrency=3:

```bash
./opentr.sh stop
# Edit .env: GPU_CONCURRENT_REQUESTS=3
./opentr.sh start dev

python scripts/benchmark_parallel.py \
  --batches 3 \
  --gpu-id 0 \
  --min-duration 10800 \
  --output benchmarks/conc3/
```

- [ ] Concurrency=3 completed without OOM
- [ ] Record: **Throughput at conc=3**: ___ x

Try concurrency=4 (and beyond — A6000 has been tested stable up to concurrency=12 at ~48.5GB VRAM):

```bash
./opentr.sh stop
# Edit .env: GPU_CONCURRENT_REQUESTS=4
./opentr.sh start dev

python scripts/benchmark_parallel.py \
  --batches 4 \
  --gpu-id 0 \
  --min-duration 10800 \
  --output benchmarks/conc4/
```

- [ ] Concurrency=4 completed without OOM
- [ ] Record: **Throughput at conc=4**: ___ x
- [ ] Record: **Peak VRAM at conc=4**: ___ MB / 49152 MB

### D3. Record scaling results

| Workers | Batch Wall Time | Avg/File | Throughput (audio hrs/wall hr) | VRAM Peak |
|---------|-----------------|----------|-------------------------------|-----------|
| 1 | | | | |
| 2 | | | | |
| 3 | | | | |
| 4 | | | | |

---

## Phase E: Multi-GPU Testing (Optional)

### E1. Dual-GPU scaled deployment

```bash
./opentr.sh stop
```

Edit `.env`:

```bash
GPU_DEVICE_ID=1                  # 3080 Ti
GPU_SCALE_ENABLED=true
GPU_SCALE_DEVICE_ID=0            # A6000
GPU_SCALE_WORKERS=4              # 4 concurrent on A6000
GPU_SCALE_DEFAULT_WORKER=1       # Keep 3080 Ti worker too (dual-GPU)
GPU_CONCURRENT_REQUESTS=1        # 3080 Ti runs 1 at a time
```

```bash
./opentr.sh start dev --gpu-scale
```

```bash
python scripts/benchmark_parallel.py \
  --batches 1,3,5,8 \
  --gpu-id 0 \
  --min-duration 10800 \
  --output benchmarks/dual-gpu/
```

- [ ] Dual-GPU benchmark completed
- [ ] Record: **5-worker throughput**: ___ x

### E2. GPU Split deployment (transcribe + diarize on separate GPUs)

```bash
./opentr.sh stop
```

Edit `.env`:

```bash
GPU_TRANSCRIBE_DEVICE_ID=0       # A6000 — WhisperX only
GPU_DIARIZE_DEVICE_ID=1          # 3080 Ti — PyAnnote only
ENGINE_SHARED_VOLUME_PATH=/scratch/opentranscribe/engine
```

```bash
./opentr.sh start dev --with-gpu-split
```

```bash
python scripts/benchmark_engine_single.py \
  --audio benchmark/test_audio/0.5h_1899s.wav \
  --runs 3 \
  --output /tmp/engine_single_gpu_split.csv
```

- [ ] GPU split benchmark completed
- [ ] Record: **Stage 2 GPU total (solo warm)**: ___ s
- [ ] Record: **Realtime factor (warm)**: ___ x
- [ ] Compare to standard (single-GPU) solo result from Phase 2

---

## Phase F: Final Projection

### F1. Generate final projection with measured data

Use the realtime factor from Phase C:

```bash
python scripts/benchmark_projection.py \
  --realtime-factor <MEASURED_VALUE> \
  --gpu-name "RTX A6000 (49GB)"
```

- [ ] Projection generated
- [ ] Record: **Estimated reprocess time (1 worker)**: ___ hours
- [ ] Record: **Estimated reprocess time (4 workers)**: ___ hours
- [ ] Record: **Estimated reprocess time (5 workers, dual-GPU)**: ___ hours

---

## Results Summary

Fill in after all phases complete (v0.4.0 reference values from completed benchmarks shown in parentheses):

| Question | Answer |
|----------|--------|
| Total files to reprocess | |
| Total audio hours | |
| Single-file realtime factor | x (ref: 40.3x for 2.78hr file) |
| Bottleneck stage (% of GPU time) | (ref: diarization 50.2%, whisper 47.6%) |
| Max concurrency on A6000 before OOM | (ref: 12, VRAM ceiling ~48.5GB) |
| Best single-GPU throughput | audio hrs/wall hr (ref: 54.6x at conc=8) |
| Best multi-GPU throughput | audio hrs/wall hr |
| Estimated time to reprocess all files | hours |
| Optimal configuration | (ref: concurrent=6-8 for shared-GPU production) |

---

## Cleanup After Benchmarking

```bash
# Restore .env to normal operation
# Edit .env:
#   ENABLE_BENCHMARK_TIMING=false   (or remove)
#   ENABLE_VRAM_PROFILING=false     (or remove)
#   GPU_CONCURRENT_REQUESTS=<best value from testing>
#   GPU_SCALE_ENABLED=<true if using dual-GPU>

# Restart with your preferred configuration
./opentr.sh stop
./opentr.sh start dev                    # single GPU
# or
./opentr.sh start dev --gpu-scale        # parallel workers on one GPU
# or
./opentr.sh start dev --with-gpu-split   # split transcribe/diarize across two GPUs
```

- [ ] Profiling disabled
- [ ] Services restarted with optimal config
