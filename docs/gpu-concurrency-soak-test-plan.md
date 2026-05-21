# GPU Concurrency Soak Test Plan

**Status:** PENDING — scheduled for later today (2026-04-29)
**Author:** David Macey
**Branch:** `feat/engine-opimization`

## Purpose

Empirically determine the true safe concurrency ceiling for both production GPUs.
The current `_auto_concurrent()` formula uses whitepaper numbers measured at high
concurrency on the A6000. We need to confirm:

1. **RTX 3080 Ti (12 GB, slot 1):** formula gives `1`. Can it actually run `2` or `3`
   concurrent tasks with the post-Phase-A stable VRAM profile?
2. **RTX A6000 (49 GB, slot 2):** formula gives `10`. Can we push `12`–`16`? The
   whitepaper VRAM ceiling *plateaued* at 48.5 GB for conc=10+, suggesting CTranslate2
   pre-allocates a fixed activation pool that is reused across tasks rather than growing
   linearly. If the plateau holds, headroom may exist.

Results will be used to recalibrate `_auto_concurrent()` in
`backend/app/transcription/config.py`.

---

## Hardware Inventory

| Slot | GPU | VRAM | Role |
|------|-----|------|------|
| 0 | RTX A6000 | 49 GB | Primary Celery GPU worker (production) |
| 1 | RTX 3080 Ti | 12 GB | Secondary / hybrid-mode baseline |
| 2 | RTX A6000 | 49 GB | GPU-scale worker (used for whitepaper benchmarks) |

---

## Benchmark Corpus

**File:** `docs/benchmark-corpus/corpus.json`

A fixed 16-file set sourced from existing MinIO deployment (no file copies — files are
reprocessed in-place via the API). Designed for repeatable, whitepaper-quality results.

### Corpus Design

Files are ordered duration-ascending so that `--batches N` always selects the N shortest
files. This means small batch tests use cheaper short files (fast iteration), and the full
16-file run covers the complete duration spectrum.

| Tier | Label | Range | Files | Audio |
|------|-------|-------|-------|-------|
| 1 | Short | 5–25 min | 4 | 1.14 h |
| 2 | Medium | 31–60 min | 4 | 3.17 h |
| 3 | Long | 1.5–3 h | 4 | 8.29 h |
| 4 | Extra-long | 3 h+ | 4 | 16.06 h |
| **Total** | | | **16** | **28.67 h** |

### Corpus Files

| # | Tier | UUID | Duration | File |
|---|------|------|----------|------|
| 1 | 1 | `5bbfb6cf-3e55-4cd6-8a5e-4f3312ad0019` | 11 min | PyTorch at Tesla — Andrej Karpathy |
| 2 | 1 | `eb191ff0-4ad7-4686-8093-6e2eedb15834` | 15 min | Secret Airships Aviation Conspiracies |
| 3 | 1 | `a196fce1-5393-4532-8270-1d2d6474ffc3` | 20 min | JRE #211 — Ari Shaffir (Part 2) |
| 4 | 1 | `a6639bee-dad8-4910-83b0-5f88907772b7` | 23 min | JRE #124 — Michael Schiavello (Part 2) |
| 5 | 2 | `ce471b5a-b4ae-45e5-8905-af7420d50f79` | 31 min | 0.5h_1899s.wav (synthetic baseline) |
| 6 | 2 | `541b4bda-241a-4e2c-8831-20e8f3c31e53` | 43 min | JRE #13 — Eddie Bravo |
| 7 | 2 | `126a9e15-e1b5-4630-b432-4a64b07812a9` | 56 min | JRE #140 — Brendon Walsh (Part 2) |
| 8 | 2 | `bf5858cf-cf43-4f95-bd1d-a45cfb72ef62` | 60 min | JRE #94 — Joey Diaz (Part 4) |
| 9 | 3 | `6089a8f2-0ef1-4e45-a3eb-7bf5fd031d32` | 1h 46m | JRE #1509 — Abigail Shrier |
| 10 | 3 | `5d351168-af6f-4bb7-a3b6-3b77b8c1776e` | 1h 49m | JRE MMA Show #66 — Michelle Waterson |
| 11 | 3 | `ce3965b9-4372-4643-a206-35cb6c6c9c9a` | 1h 51m | JRE #1362 — Lenny Clarke |
| 12 | 3 | `ccee9e47-e879-4ba1-9b59-648c79a444a0` | 2h 51m | JRE #1461 — Owen Smith |
| 13 | 4 | `4824a0ab-1a3c-41f4-98e9-9a4547f479f0` | 3h 24m | JRE #1609 — Elon Musk |
| 14 | 4 | `7dc3ffbe-bdb3-4bc9-8d13-f78cd5d251ad` | 3h 42m | JRE #1393 — Game Changers Debate (3 spk) |
| 15 | 4 | `28506325-0e30-40b1-a300-47f4ab6482e2` | 4h 13m | JRE #1769 — Jordan Peterson |
| 16 | 4 | `3e313bbd-924f-4a4b-9584-fa24532b9a01` | 4h 44m | JRE #1907 — Protect Our Parks 6 (4+ spk) |

### Corpus Profiles

Select a profile with `--profile <name>`. Combine with `--shuffle` to randomise
arrival order within the selected set (recommended for all real-world throughput tests).

| Profile | `--batches 4` selects | Best for |
|---------|----------------------|----------|
| `by_duration` *(default)* | 4 shortest files (all Tier 1, 5–23 min) | VRAM ceiling tests — OOM fails fast |
| `mixed` | 1 file from each tier (T1+T2+T3+T4) | Throughput / scheduler stress tests |
| `mixed_hard` | Longest file from each tier (8.97 h total) | Quick 4-file real-world stress test |

**Rule of thumb:**
- VRAM / OOM tests → `--profile by_duration` (no shuffle needed — homogeneous files)
- Throughput / scheduler tests → `--profile mixed --shuffle`
- Quick stress check → `--profile mixed_hard --shuffle`

### Expected Throughput at Full Corpus (batch=16)

At the whitepaper single-file RTF of 44.6×, the 4.73 h anchor file (longest) takes ~382 s.
All 16 files submit simultaneously; wall time ≈ the longest file's completion time.

```
Projected aggregate RTF = 28.67 h audio / (382 s / 3600) = ~270×
```

This headline metric will be printed automatically at the end of the benchmark run.

---

## Known Baselines (existing benchmark data)

From `docs/engine-benchmark-results/engine_single_20260429_040907.csv` (GPU 0, conc=1,
warm, `large-v3-turbo`, 0.5 h audio):

| Run | Preprocess | GPU | Finalize | Total | RTF |
|-----|-----------|-----|----------|-------|-----|
| 1 (cold) | 2.09 s | 47.71 s | 0.29 s | 50.08 s | 37.9× |
| 2 (warm) | 0.86 s | 41.92 s | 0.15 s | 42.93 s | 44.2× |
| 3 (warm) | 0.82 s | 41.59 s | 0.15 s | 42.56 s | **44.6×** |

From `docs/engine-benchmark-results/engine_queue_20260429_040907.csv` (GPU 0, direct
mode, 3 concurrent threads, 5 files):

| File | Duration | Wall | RTF |
|------|----------|------|-----|
| 0.5 h | 1 899 s | 117.9 s | 16.1× |
| 1.0 h | 3 758 s | 279.2 s | 13.5× |
| 2.2 h | 7 998 s | 585.5 s | 13.7× |
| 3.2 h | 11 495 s | 712.1 s | 16.2× |
| 4.7 h | 17 044 s | 838.8 s | 20.3× |
| **Aggregate** | **42 239 s** | — | **15.95× avg** |

Whitepaper peak: **54.6× aggregate throughput at conc=8**, 48.5 GB VRAM ceiling at
conc=10+ (VRAM plateau observed, not linear beyond that point).

---

## Shared Weight Architecture

The `ModelManager` singleton (`backend/app/transcription/model_manager.py`) loads Whisper
and PyAnnote **once per worker process** and shares those weights across all concurrent
Celery threads:

```
Worker process (1 Python process, N Celery threads via --pool=threads):
  └─ ModelManager._instance
       ├─ Transcriber   ← CTranslate2 CUDA weights, loaded once (~5-6 GB)
       └─ SpeakerDiarizer ← PyAnnote weights, loaded once (~1-2 GB)
            ↑ all N threads share the same tensors in VRAM
     Each thread allocates only activation / KV-cache buffers per inference.
```

This means VRAM does **not** scale linearly with concurrency after the model baseline.
Per-task overhead is activation buffers only — measured at roughly 3–5 GB per task at
low concurrency, then VRAM plateaus once CTranslate2's activation pool saturates.

---

## Current Auto Formula (in code — may need updating after this test)

```python
# backend/app/transcription/config.py — _auto_concurrent()  (current code)
# Calibrated from whitepaper: ~7 GB shared model baseline, ~4 GB/task overhead
concurrent = int((total_mb - 7000) // 4000)
return max(1, min(concurrent, 12))
```

| GPU | total_vram | Formula result | Capped to |
|-----|-----------|----------------|-----------|
| 3080 Ti 12 GB | 12 288 MB | 1 | **1** |
| A6000 49 GB | 50 176 MB | 10 | **10** |

The 4 GB/task coefficient was measured at conc=10 on A6000 (48.5 GB total / 10 tasks ≈
4.15 GB/task after subtracting the 7 GB baseline). However, the VRAM plateau means this
number is an average — actual marginal cost per additional task above conc=10 may be near
zero (shared activation pool). The cap of 12 was set conservatively.

This test checks whether cap=16–20 is safe and whether the 4 GB/task coefficient should
be revised down now that the plateau behaviour is confirmed.

---

## Measured VRAM Data — A6000, existing (`docs/BENCHMARK_RESULTS.md`)

Pre-existing data from GPU 0 (A6000), ~2.7 h matched files, `large-v3-turbo`:

| Concurrency | Peak VRAM | ∆ from prev | Aggregate RTF | Notes |
|-------------|-----------|-------------|--------------|-------|
| 1 | 8 909 MB | — | 39.3× | Baseline |
| 2 | 19 157 MB | +10 248 MB | 47.4× | |
| 4 | 31 301 MB | +12 144 MB | 51.3× | |
| 6 | 34 263 MB | +2 962 MB | 52.4× | Plateau starting |
| 8 | 44 199 MB | +9 936 MB | **54.6×** | **Throughput peak** |
| 10 | 48 535 MB | +4 336 MB | 51.6× | VRAM plateau |
| 12 | 48 519 MB | −16 MB | 52.5× | **Confirmed stable** |

Key observations:
- VRAM plateaus at ~48.5 GB from conc=10 onward — shared weights confirmed
- Throughput peaks at conc=8, slight regression beyond (SM contention, not OOM)
- conc=12 holds the same VRAM as conc=10 → conc=14, 16, 20+ may also fit

**Unknown:** Does the plateau hold at conc=14, 16, 20, 24? Does SM contention cause a
hard throughput floor, or a soft regression? That is what this soak test answers.

---

## Hypothesis

**A6000:** With the VRAM plateau proven stable at conc=10–12, conc=14–24 should remain
within 49 GB (model weights already loaded, no new allocation at steady-state). The risk
is SM execution serialisation, not OOM. Throughput may regress modestly but the GPU
should not crash. New formula target: `cap = 16` or `cap = 20`, formula coefficient
based on measured per-task overhead at the new stable ceiling.

**3080 Ti:** With shared weights and ~9 GB model baseline (scaled from A6000 measurements),
12 GB leaves ~3 GB for activation buffers — enough for conc=2 or 3. Current cap of 4
is probably too high for 12 GB (would require only 0.75 GB/task). Test conc=2, 3, 4.
Expected result: conc=2 stable, conc=3 marginal, conc=4 OOM.

---

## Test Matrix

### GPU 1 — RTX 3080 Ti (12 GB, `CUDA_VISIBLE_DEVICES=1`)

Run each level. Stop on OOM. Use `--profile by_duration` (Tier 1 files, 11–23 min each).

| # | Conc | Expected VRAM | Status | Peak VRAM | Avg RTF | Stable? | Notes |
|---|------|--------------|--------|-----------|---------|---------|-------|
| A | 1 | ~9 GB (model baseline) | TODO | | | | Baseline |
| B | 2 | ~11 GB | TODO | | | | Target — likely fits |
| C | 3 | ~12 GB | TODO | | | | Marginal |
| D | 4 | ~13+ GB | TODO | | | | Likely OOM — current cap |

**Stop criteria:** OOM error or peak VRAM > 11.5 GB (95% of 12 GB).

### GPU 2 — RTX A6000 (49 GB, `CUDA_VISIBLE_DEVICES=2`)

Levels 1–12 have prior data (shown for reference); levels 14–24 are new.
Use `--profile mixed --shuffle` for levels 8+ to test realistic scheduler behaviour.

| # | Conc | Expected VRAM | Status | Peak VRAM | Avg RTF | GPU Util% | Stable? | Notes |
|---|------|--------------|--------|-----------|---------|-----------|---------|-------|
| A | 1 | 8 909 MB ✓ | KNOWN | 8 909 | 39.3× | | Yes | From BENCHMARK_RESULTS.md |
| B | 4 | 31 301 MB ✓ | KNOWN | 31 301 | 51.3× | | Yes | From BENCHMARK_RESULTS.md |
| C | 8 | 44 199 MB ✓ | KNOWN | 44 199 | **54.6×** | | Yes | **Peak throughput** |
| D | 10 | 48 535 MB ✓ | KNOWN | 48 535 | 51.6× | | Yes | VRAM plateau begins |
| E | 12 | 48 519 MB ✓ | KNOWN | 48 519 | 52.5× | | Yes | Plateau confirmed |
| F | 14 | ~48.5 GB | TODO | | | | | **First new level** |
| G | 16 | ~48.5 GB | TODO | | | | | Full corpus size |
| H | 20 | ~48.5 GB? | TODO | | | | | Stretch — SM contention |
| I | 24 | ~48.5 GB? | TODO | | | | | Hard limit or regression |

**Stop criteria:** OOM error, peak VRAM > 48.5 GB (new allocation = plateau broken),
or aggregate RTF drops below 40× (severe SM contention — no throughput benefit).

---

## Test Procedure

### Prerequisites

```bash
# 1. Activate venv on host
source backend/venv/bin/activate

# 2. Confirm ENABLE_BENCHMARK_TIMING=true in .env (needed for e2e timing data)
grep ENABLE_BENCHMARK_TIMING .env

# 3. Check GPUs are visible and idle
nvidia-smi --query-gpu=index,name,memory.used,memory.total --format=csv

# 4. Verify corpus files exist in the deployment (dry run)
BENCHMARK_EMAIL=admin@example.com BENCHMARK_PASSWORD=password \
python scripts/benchmark_parallel.py \
    --corpus-file docs/benchmark-corpus/corpus.json \
    --batches 16 --dry-run
```

### Phase 1 — Single-File Latency Baseline (both GPUs)

Run inside the Celery worker container:

```bash
# A6000 (GPU 0 — primary worker)
docker exec opentranscribe-celery-worker \
    python /app/scripts/benchmark_engine_single.py \
    --audio /app/benchmark/test_audio/0.5h_1899s.wav \
    --runs 5 --cuda-device 0 \
    --output /app/docs/engine-benchmark-results/single_a6000_conc1_$(date +%Y%m%d).csv

# 3080 Ti (GPU 1)
docker exec -e CUDA_VISIBLE_DEVICES=1 opentranscribe-celery-worker \
    python /app/scripts/benchmark_engine_single.py \
    --audio /app/benchmark/test_audio/0.5h_1899s.wav \
    --runs 5 --cuda-device 0 \
    --output /app/docs/engine-benchmark-results/single_3080ti_conc1_$(date +%Y%m%d).csv
```

### Phase 2 — Concurrency Sweep via Queue Mode

For each concurrency level N in the matrix above:

```bash
# Step 1: Set concurrency in .env
#   GPU_CONCURRENT_REQUESTS=N   (replace N with the level under test)

# Step 2: Restart GPU worker with the new setting
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.local.yml \
    up -d --no-deps --force-recreate celery-worker

# Step 3: Confirm worker started with right concurrency
docker compose logs --tail=20 celery-worker | grep "concurrent_requests\|concurrency"

# Step 4a: VRAM ceiling test — by_duration profile, no shuffle (homogeneous files, OOM fails fast)
BENCHMARK_EMAIL=admin@example.com BENCHMARK_PASSWORD=password \
python scripts/benchmark_parallel.py \
    --corpus-file docs/benchmark-corpus/corpus.json \
    --profile by_duration \
    --batches N \
    --gpu-id 2 \
    --output docs/engine-benchmark-results/parallel_vram_conc${N}_$(date +%Y%m%d)/

# Step 4b: Throughput / scheduler test — mixed profile, shuffled (real-world representative)
BENCHMARK_EMAIL=admin@example.com BENCHMARK_PASSWORD=password \
python scripts/benchmark_parallel.py \
    --corpus-file docs/benchmark-corpus/corpus.json \
    --profile mixed --shuffle \
    --batches N \
    --gpu-id 2 \
    --output docs/engine-benchmark-results/parallel_mixed_conc${N}_$(date +%Y%m%d)/

# Step 5: Watch VRAM in another terminal during the run
watch -n 1 "nvidia-smi --query-gpu=index,memory.used,memory.total,utilization.gpu \
    --format=csv,noheader | grep -E '^1,|^2,'"
```

**Full-corpus run (all 16 files, mixed profile, shuffled — whitepaper headline metric):**
```bash
BENCHMARK_EMAIL=admin@example.com BENCHMARK_PASSWORD=password \
python scripts/benchmark_parallel.py \
    --corpus-file docs/benchmark-corpus/corpus.json \
    --profile mixed --shuffle \
    --batches 16 \
    --gpu-id 2 \
    --output docs/engine-benchmark-results/parallel_fullcorpus_$(date +%Y%m%d)/
```
The script prints `*** Full-corpus aggregate RTF ***` automatically when all 16 files complete.

**Quick 4-file stress test (mixed_hard — 8.97 h total, one file per tier):**
```bash
BENCHMARK_EMAIL=admin@example.com BENCHMARK_PASSWORD=password \
python scripts/benchmark_parallel.py \
    --corpus-file docs/benchmark-corpus/corpus.json \
    --profile mixed_hard --shuffle \
    --batches 4 \
    --gpu-id 2 \
    --output docs/engine-benchmark-results/parallel_stress4_$(date +%Y%m%d)/
```

### Phase 3 — Duration Curve (sequential, one file at a time)

Produces RTF-vs-duration data for the whitepaper figure. Uses the corpus to ensure the
same file set is measured sequentially and concurrently (allows fair comparison).

```bash
# Run each corpus file individually, in order, collecting RTF per duration tier
BENCHMARK_EMAIL=admin@example.com BENCHMARK_PASSWORD=password \
python scripts/benchmark_parallel.py \
    --corpus-file docs/benchmark-corpus/corpus.json \
    --sequential \
    --cooldown 15 \
    --output docs/engine-benchmark-results/duration_curve_$(date +%Y%m%d)/
```

### Phase 4 — E2E Stage Timing (single file, each concurrency level)

Per-stage breakdown (preprocess / GPU / postprocess / queue gaps):

```bash
BENCHMARK_EMAIL=admin@example.com BENCHMARK_PASSWORD=password \
python scripts/benchmark_e2e.py \
    --file-uuid ce471b5a-b4ae-45e5-8905-af7420d50f79 \
    --iterations 3 --detailed \
    --output docs/engine-benchmark-results/e2e_conc${N}_$(date +%Y%m%d).csv
```

---

## Metrics to Record

For each concurrency level on each GPU:

| Metric | Source | Why It Matters |
|--------|--------|---------------|
| Peak VRAM (MB) | nvidia-smi during run | OOM risk, headroom |
| VRAM at model load (idle) | nvidia-smi before first task | Baseline drift check |
| GPU utilisation % | nvidia-smi avg over batch | GPU saturation |
| Aggregate RTF | benchmark_parallel.py summary | Throughput |
| Full-corpus RTF (batch=16) | `*** Full-corpus aggregate RTF ***` line | Whitepaper headline metric |
| Per-file wall time (avg/min/max) | parallel CSV | Latency variance |
| GPU stage duration (avg) | e2e CSV | Inference bottleneck |
| CPU→GPU queue gap (avg) | e2e CSV | Queue contention |
| OOM occurred | manual observation | Hard stop |
| VRAM stable across runs (drift < 200 MB) | soak check | Memory leak regression |

---

## Soak Stability Check (after finding max safe concurrency)

Run 10 consecutive batches at the identified max concurrency level. VRAM must not
drift > 200 MB between batch 1 and batch 10 (confirms no memory leak).

```bash
# Example: 10-batch soak at conc=12 on A6000, mixed profile shuffled (real-world queue)
for i in $(seq 1 10); do
    echo "=== Soak batch $i ==="
    BENCHMARK_EMAIL=admin@example.com BENCHMARK_PASSWORD=password \
    python scripts/benchmark_parallel.py \
        --corpus-file docs/benchmark-corpus/corpus.json \
        --profile mixed --shuffle \
        --batches 12 \
        --gpu-id 2 \
        --cooldown 0 \
        --output docs/engine-benchmark-results/soak_conc12_batch${i}_$(date +%Y%m%d_%H%M%S)/
    sleep 30  # allow GPU cooldown between batches
done
```

---

## Result Tables (fill in during testing)

### RTX 3080 Ti — Concurrency Sweep Results

| Conc | Peak VRAM | GPU Util% | Aggregate RTF | Stable | OOM |
|------|-----------|-----------|--------------|--------|-----|
| 1 | | | | | |
| 2 | | | | | |
| 3 | | | | | |

**Safe maximum:** ___
**Recommended default for 12 GB GPUs:** ___

### RTX A6000 — Concurrency Sweep Results

| Conc | Peak VRAM | GPU Util% | Aggregate RTF | Stable | OOM |
|------|-----------|-----------|--------------|--------|-----|
| 1 | | | | | |
| 4 | | | | | |
| 8 | | | | | |
| 10 | | | | | |
| 12 | | | | | |
| 14 | | | | | |
| 16 | | | | | |

**Safe maximum:** ___
**Throughput peak (RTF):** ___ at conc=___
**Full-corpus aggregate RTF (batch=16):** ___ (projected ~270×)
**VRAM plateau observed:** YES / NO, at conc=___

---

## Automated Sweep Script

`scripts/benchmark_concurrency_sweep.sh` already automates the full A6000 sweep
(set concurrency in `.env` → restart worker → run parallel benchmark → record VRAM).

**Note:** The script uses old hardcoded file UUIDs from before the fixed corpus was
created. Before running, update the `FILES_*` variables at the top of the script to use
corpus UUIDs, or use `benchmark_parallel.py --corpus-file` directly (preferred).

The sweep script's concurrency levels (`1 2 4 6 8 10 12`) only go to 12 — extend
`CONCURRENCY_LEVELS` to `"1 2 4 6 8 10 12 14 16 20 24"` before running.

---

## Formula Update Criteria

After testing, update `_auto_concurrent()` in `backend/app/transcription/config.py`:

```python
# New formula (fill in after testing):
# Baseline: ___ MB (model idle footprint from Phase 1 baseline VRAM)
# Per-task overhead: ___ MB (∆ VRAM per additional task at low concurrency)
# Cap: ___ (last conc level with stable VRAM plateau and RTF ≥ 40×)
concurrent = int((total_vram - ___) // ___)
return max(1, min(concurrent, ___))
```

Expected new cap: **16–20** for A6000 (if plateau holds), **2** for 3080 Ti (12 GB).

Also update:
- `.env.example` — `GPU_CONCURRENT_REQUESTS` comment with confirmed values
- `docs/BENCHMARK_RESULTS.md` — append new rows to the concurrency table
- `docs/gpu-concurrency-soak-test-plan.md` — fill in the TODO cells in the test matrix above

---

## Files Changed by This Work

| File | Change |
|------|--------|
| `backend/app/transcription/config.py` | `_auto_concurrent()` formula and cap |
| `.env.example` | `GPU_CONCURRENT_REQUESTS` comment with measured values |
| `scripts/benchmark_e2e.py` | Redis URL auto-loads from `.env` (fixed wrong port/auth) |
| `scripts/benchmark_parallel.py` | Redis URL, `--corpus-file`, `--profile`, `--shuffle`, GPU util% in summary |
| `docs/benchmark-corpus/corpus.json` | Fixed 16-file corpus (28.67 h total audio) with `profiles` section |
| `docs/gpu-concurrency-soak-test-plan.md` | This file |

Committed on branch `feat/engine-opimization` as of 2026-04-29.
