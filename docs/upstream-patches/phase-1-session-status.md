# Phase 1 Baseline Session — Status

**Date**: 2026-04-21
**Session**: Phase 1 bring-up of pyannote fork optimization round 2

## What landed this session

### Tooling (`benchmark-pyannote-direct.py`)

Extended the benchmark harness with:
- `--runs N` with 5-number statistical aggregation (mean / stdev / min / max / cv)
- `--tag LABEL` for per-session output filenames
- `--rttm-out DIR` for per-run RTTM export (feeds Phase 1.4 DER scoring)
- `--profiler` for single-run torch.profiler Chrome-trace export
- `--device mps` support with a background VRAM poller (100 ms) — MPS has no `max_memory_allocated`
- `--optimized-src` override so the harness can load from either fork checkout
- `--model` override (default now `pyannote/speaker-diarization-community-1` to match production; was `3.1`)
- `torch.cuda.synchronize()` / `torch.mps.synchronize()` before every timestamp and per-stage VRAM capture
- scipy WAV fallback when torchcodec is broken (container is missing `libnppicc.so.12`)
- Fork git SHA + torch version + CUDA version recorded in the result JSON
- Graceful `get_gpu_info()` fallback to `torch.cuda.get_device_properties` when pynvml is unavailable

### Fork edits (`pyannote-audio-fork`, gpu-optimizations branch)

14 `torch.profiler.record_function` labels added across three files:
- `src/pyannote/audio/pipelines/speaker_diarization.py` (4): `pyannote::segmentation`, `pyannote::embedding_fbank`, `pyannote::embedding_resnet`, `pyannote::embedding_batch`
- `src/pyannote/audio/pipelines/clustering.py` (10): `pyannote::clustering_cdist` (×2), `pyannote::clustering_cdist_merge`, `pyannote::clustering_normalize` (×3), `pyannote::clustering_linkage` (×3), `pyannote::clustering_kmeans`
- `src/pyannote/audio/core/inference.py` (1): `pyannote::aggregation` (wraps the `aggregate` static method via a `_aggregate_impl` helper)

Zero runtime cost when profiler is inactive. **Not yet committed** — awaiting review.

### Scripts

- `scripts/diarization-der-compare.py` (new) — reads our `benchmark/results/rttm/<tag>/` layout and scores DER between two run dirs with `DiarizationErrorRate(collar=0.25, skip_overlap=False)`. Returns non-zero exit if DER > 0.3% or speaker mismatch. Companion to the existing Phase-A-specific `diarization-der.py`.

### Docs

- `docs/upstream-patches/vectorization-audit.md` — 8 findings, ranked by impact. Feeds Phase 3.5.
- `docs/upstream-patches/vram-budget-table.md` — per-stage VRAM decomposition on the 0.5h CUDA baseline. Feeds Phase 3 (GPU clustering VRAM guard).

### Infrastructure

- `docker-compose.benchmark.yml` mount fix: `./benchmark/results` now writable; test_audio still read-only.
- One-time Mac Studio setup: rsynced fork edits, benchmark script, and short test audio to `mac-studio.local:~/repos/pyannote-audio/`.

## Baseline numbers

### CUDA A6000 (RTX A6000, 48 GB, torch 2.8+cu128)

5-run statistical means:

| file | wall (s) | cv | peak VRAM | steady | spkrs | segs |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 0.5h_1899s | **22.62** ±0.48 | 2.1% | 844 MB | 39 MB | 4 | 703 |
| 2.2h_7998s | **101.47** ±0.29 | 0.29% | 844 MB | 39 MB | 3 | 2855 |

Per-stage means (2.2h file):

| stage | duration (s) | ±stdev | cum peak | delta |
| --- | ---: | ---: | ---: | ---: |
| segmentation | 5.52 | 0.14 | 437 MB | +398 |
| binarization | 0.18 | 0.005 | 437 MB | +0 |
| embedding_chunk_extraction | 0.85 | 0.04 | 437 MB | +0 |
| **embeddings** | **75.27** | 0.32 | 844 MB | **+407** |
| **clustering_start** (scipy CPU) | **12.29** | 0.11 | 844 MB | **+0** |
| discrete_diarization | 4.32 | 0.08 | 844 MB | +0 |
| reconstruction_start | 2.44 | 0.05 | 844 MB | +0 |

3-run long-file (3.2h) — complete, 4.7h in progress:

| file | wall (s) | cv | peak VRAM | spkrs | segs |
| --- | ---: | ---: | ---: | ---: | ---: |
| 3.2h_11495s | **152.7** ±1.0 | 0.65% | 844 MB | 3 | 3040 |
| 4.7h_17044s (run 0) | 338.8 | — | 844 MB | 8 | 12105 |

**Peak VRAM 844 MB is identical across all 13 completed runs on all 3 files.** Allocator is deterministic for fixed input shapes.

### MPS M2 Max (Mac Studio, torch 2.10, 32 GB unified)

1-run smoke on 0.5h:

| file | wall (s) | peak VRAM | spkrs | segs | vs CUDA |
| --- | ---: | ---: | ---: | ---: | --- |
| 0.5h_1899s | 42.3 | 1890 MB | 4 | 693 | 1.87× slower, 2.24× VRAM |

Per-stage smoke (0.5h MPS):

| stage | duration | cum peak | delta |
| --- | ---: | ---: | ---: |
| segmentation | 4.79s | 1864 MB | **+1808** |
| embeddings | 36.18s | 1890 MB | +26 |
| clustering_start | **0.35s** | 1890 MB | +0 |
| reconstruction_start | 0.17s | 1890 MB | +0 |

**5-run MPS baseline still in flight** — will update this doc when complete.

**Caveat**: Mac Studio is an active workstation during these runs (browsers, SSH sessions). MPS wall time may show higher CV than CUDA. Data is still useful for relative comparisons under similar load.

## Key findings

### 1. Clustering share scales with file length

| file | duration | clustering share | projected if 3× faster |
| --- | ---: | ---: | ---: |
| 0.5h | 22.6s | **5.9%** | −4% E2E |
| 2.2h | 101.5s | **12.1%** | **−8% E2E** |
| 3.2h | 152.7s | (likely 15-17%) | −10% to −12% E2E |
| 4.7h | 338.8s | (will measure) | **target ≥10%** |

Phase 3 (GPU clustering via `torch.cdist`) hits the plan's ≥10% end-to-end target on 4.7h — needs the 4.7h stage breakdown to confirm, which lands when the long baseline completes.

### 2. MPS has a fundamentally different optimization profile

Clustering on MPS is **0.35s (0.83% of wall)** — already fast because M2 Max CPU beats Linux-container scipy. Phase 3's GPU clustering will barely move MPS wall time. MPS optimization must target the **embedding stage (85% of MPS wall)** and whichever ops profile out as CPU fallbacks (Phase 1.5).

### 3. VRAM envelope confirmed

CUDA per-pipeline peak is 844 MB on every file every run. Invariant for Phase 3 is 1.05 GB → **~200 MB headroom headline, 50 MB budget after safety margin**. Matches the Phase A measurement. MPS's 1890 MB peak reflects the unified-memory allocator pre-reserve (Phase A's observation) — separate ceiling will be needed for MPS-specific invariants.

### 4. Deterministic across runs

CUDA allocator is deterministic: peak is identical byte-for-byte across 13 runs. Segment count is identical (703 / 2855 / 3040) across runs of the same file. Speaker count is identical. This means Phase 3's numerical-parity tests have a very tight target.

CV on 2.2h wall time: **0.29%** — sub-1% speedups are detectable with confidence.

## Open infrastructure issues

1. ~~Two divergent fork copies~~ **Resolved 2026-04-21**: `reference_repos/pyannote-audio-optimized/` (stale March-11 WIP snapshot of upstream main with uncommitted Phase-A patches) was deleted. All code, scripts, and docs now point at `/mnt/nvm/repos/pyannote-audio-fork/` (gpu-optimizations branch, canonical). Harness default `DEFAULT_OPTIMIZED_SRC` updated accordingly.

2. **Container missing `libnppicc.so.12`** breaks torchcodec inside `opentranscribe-backend:latest`. Worked around with scipy.io.wavfile fallback. Proper fix is a Dockerfile tweak to include `libnvjpeg-12-0` / `libnpp-12-0`.

3. **pynvml not installed** in the container. Worked around with torch fallback. Low priority.

4. **Fork edits uncommitted** — Phase 1.2 record_function labels sit in the working tree of `/mnt/nvm/repos/pyannote-audio-fork/`. Safe to commit (zero-cost labels + refactor to `_aggregate_impl`) but awaiting review.

## Next session

1. Wait for CUDA 4.7h × 3 and MPS 5-run to complete (~15-20 min after this doc).
2. Run `diarization-der-compare.py` on the baseline RTTM vs itself (sanity check: should be DER=0).
3. Phase 1.5 MPS profiler trace: rerun 2.2h MPS with `--profiler`, parse Chrome trace for `aten::` ops falling back to CPU.
4. Commit fork record_function labels once baselines confirm no accidental regressions.
5. Start Phase 2 (trivial wins: compile gate fix, Hamming cache, cudnn.benchmark, mixed-precision plumbing).
6. Start Phase 3 (GPU clustering via torch.cdist) — the highest-impact remaining optimization.

## Reproducing any measurement

All commands in this session:

```bash
# CUDA short baseline
docker compose -f docker-compose.benchmark.yml run --rm --remove-orphans diarization-probe \
  python scripts/benchmark-pyannote-direct.py --variant optimized --device cuda \
    --files 0.5h_1899s 2.2h_7998s --runs 5 \
    --tag baseline_a6000_$(date +%Y%m%d_%H%M%S)_short \
    --rttm-out benchmark/results/rttm/baseline_a6000_short

# CUDA long baseline
docker compose -f docker-compose.benchmark.yml run --rm --remove-orphans diarization-probe \
  python scripts/benchmark-pyannote-direct.py --variant optimized --device cuda \
    --files 3.2h_11495s 4.7h_17044s --runs 3 \
    --tag baseline_a6000_$(date +%Y%m%d_%H%M%S)_long \
    --rttm-out benchmark/results/rttm/baseline_a6000_long

# MPS baseline (Mac Studio via ssh)
ssh user@mac-studio.local 'cd ~/repos/pyannote-audio && source venv/bin/activate && \
  HUGGINGFACE_TOKEN="$HUGGINGFACE_TOKEN" python scripts/benchmark-pyannote-direct.py \
    --variant optimized --device mps --files 0.5h_1899s 2.2h_7998s --runs 5 \
    --tag baseline_m2max_$(date +%Y%m%d_%H%M%S)_short \
    --rttm-out benchmark/results/rttm/baseline_m2max_short'

# DER comparison (once Phase 2/3 RTTMs exist)
python scripts/diarization-der-compare.py \
  --reference benchmark/results/rttm/baseline_a6000_short \
  --hypothesis benchmark/results/rttm/phase3_gpu_clustering_a6000_short
```

Pre-req: the `opentranscribe-celery-worker` docker container must be **stopped**, not just paused, so GPU 0 VRAM is clean before starting the benchmark.
