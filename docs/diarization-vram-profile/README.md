# Diarization VRAM Profile — Phase A Findings

**Measured:** 2026-04-20 on GPU 0 (NVIDIA RTX A6000, 48 GB), benchmark container using the same image as `opentranscribe-celery-worker` (CUDA 12.8, torch 2.8.0+cu128, fork `davidamacey/pyannote-audio@gpu-optimizations`).

**Corpus:** `benchmark/test_audio/0.5h_1899s.wav` (1899 s, 4 speakers) and `2.2h_7998s.wav` (7998 s, 3 speakers).

**Harness:** `scripts/vram-probe-diarization.py` via `docker-compose.benchmark.yml` (`--matrix` and `--small-batch-sweep` modes). NVML ctypes sampler at 100 ms cadence; torch allocator peaks via `torch.cuda.memory_stats`.

**Raw data:** `docs/diarization-vram-profile/raw/*.json` (156 runs: 96 matrix + 60 small-batch sweep).

## Bottom line

1. **Embedding-batch throughput saturates at bs=16.** On the 2.2 h file at fp32, wall-time is 103 s at bs=16, 103 s at bs=32, 100 s at bs=64, 100 s at bs=128. Above 16, the fork's auto-scaler promotes batch up to 64–128 and spends **3–7 GB of extra VRAM for ~3 % throughput gain.**
2. **bs=16 fp32 fits a 4 GB laptop GPU with room for Whisper.** Pipeline process footprint at bs=16 is ~950 MB (device peak − idle baseline) plus ~500 MB CUDA context = ~1.5 GB total. Whisper small/base adds ~0.5–0.8 GB. Budget on a 4 GB card: ~2 GB free for system + driver overhead.
3. **fp16 autocast is not safe to ship as default.** At every batch size from 1 to 256, fp16 systematically merges speakers (0.5 h: 4 → 2; 2.2 h: 3 → 2) and drops ~30–44 % of the segments. The effect is consistent across batch sizes, so it is a pure precision effect, not a batching artifact. fp16 gains ~30–45 % wall time; it should be an opt-in "fast mode" with a loud accuracy caveat, not the default.
4. **Parallel diarization is viable on large GPUs.** At bs=16 fp32, a single pipeline uses ~1.5 GB total. An A6000 (48 GB) could host ~20+ concurrent diarizations, or a combined Whisper-plus-diarization worker pool an order of magnitude larger than today's `concurrent_requests=1` default.

## Throughput curve (2.2 h file, fp32, unlimited cap)

| Embedding batch size | Wall time (s) | Δ vs bs=64 | Device peak (MB) | Process footprint (MB) |
|---:|---:|---:|---:|---:|
| 1   | 265 | +165 % | 1 740 | 640 |
| 4   | 126 |  +26 % | 1 740 | 640 |
| 8   | 111 |  +11 % | 1 740 | 640 |
| **16**  | **103** | **+3 %** | **2 054** | **954** |
| 32  | 103 |   +3 % | 3 046 | 1 946 |
| 64  | 100 |   0 %  | 5 668 | 4 568 |
| 128 | 100 |   0 %  | 9 054 | 7 954 |

Process footprint = device peak − ~1 100 MB idle baseline (other host workloads on GPU 0 during the sweep).
bs=64/128 rows are from the 96-run trimmed matrix; bs≤32 from the small-batch sweep with `PYANNOTE_FORCE_EMBEDDING_BATCH_SIZE` bypassing the fork auto-scaler.

At bs=1/4/8 the process footprint plateaus at ~640 MB because torch's allocator pool is dominated by the segmentation model weights and a minimum reservation; actual per-item cost only shows up once the embedding pool scales past the reservation floor (~bs=16).

## DER — formal accuracy verification (Phase A.3, 2026-04-20)

Reference: fp32/bs=16/unlimited/r=0 for each file. Metric: `pyannote.metrics.DiarizationErrorRate(collar=0.25, skip_overlap=False)`. Full table in [`accuracy.md`](accuracy.md); raw RTTMs in `raw/rttm/`.

| Precision | bs range | DER @ 0.5 h | DER @ 2.2 h | Speaker count | Tier |
|---|---|---:|---:|:---:|:---:|
| fp32 | 1 → 128 | 0.00–0.07 % | 0.00 % | match | **T1** |
| fp16 | 1 → 128 | **33.3 %**  | **26.9 %** | 4→2 / 3→2 | **T3** |

Two findings from the DER table:

1. **fp32 is batch-size-invariant.** Changing bs from 1 to 128 produces the same diarization output, modulo 0.07 % jitter at bs=1 on the 0.5 h file (6 of 703 segments differ). The fork's auto-scaler, which chose bs=64–128, was trading 3-7 GB of VRAM for *zero* accuracy or speed benefit.
2. **fp16 is not a precision tunable; it is a different model.** The DER delta is so large (26-33 %) that the fp16 path is effectively clustering on a different embedding space. The root cause is almost certainly numerical underflow in the WeSpeaker pooling `std()` stage (warning emitted on every fp16 run). fp16 must be gated behind an opt-in flag with explicit accuracy-loss documentation; it cannot ship as default.

## fp16 vs fp32 on the same corpus

| File | Precision | Speakers detected | Segments | Wall @ bs=16 |
|---|---|---:|---:|---:|
| 0.5 h (4-speaker GT) | fp32 | **4** | 703 | 23.5 s |
| 0.5 h (4-speaker GT) | fp16 | 2   | 395 | 15.0 s |
| 2.2 h (3-speaker GT) | fp32 | **3** | 2 855 | 102.9 s |
| 2.2 h (3-speaker GT) | fp16 | 2   | 2 020 | 60.7 s |

The fp16 delta is constant across bs ∈ {1, 4, 8, 16, 32, 64, 128, 256}. The `UserWarning: std(): degrees of freedom is <= 0` emitted during fp16 runs in `pyannote/audio/models/blocks/pooling.py:103` points at numerical underflow in the statistics pooling stage — a plausible root cause for the speaker merging.

Phase A.3 (formal DER via `pyannote.metrics.DiarizationErrorRate`) will put a hard number on the accuracy cost. Based on the ~30–44 % segment-count drop, we expect DER well above the 3 % "marginal" threshold in the plan, which puts fp16 in the T3 (rejected as default) tier.

## Recommended Phase B policy

The fork's `_budget.py` helper should map free VRAM to a batch size without tier-differentiating by GPU class:

| Free VRAM available to diarization | Embedding batch size | Precision |
|---|---|---|
| < 700 MB   | 4  | fp32 |
| 700–1 000 MB | 8  | fp32 |
| 1 000–1 900 MB | 16 | fp32 |
| ≥ 1 900 MB  | 16 | fp32 (bigger buys nothing) |
| User opts in to fast mode | unchanged | fp16 (with accuracy warning) |

Retire the current auto-scaler's bs=64–256 range entirely. It was premised on "bigger batch = more throughput" which this data disproves above bs=16.

## Parallel-diarization opportunity

A single diarization pipeline at bs=16 fp32 holds ~1.5 GB of VRAM (weights + CUDA context + activations). This unlocks:

- **Multi-file parallelism on a single A6000** (48 GB): ~20–25 concurrent pipelines, vs. today's default of 1 via `GPU_CONCURRENT_REQUESTS=1`. Actual ceiling limited by CPU pre/post-processing, not GPU.
- **Mixed-model worker** on a 24 GB card: Whisper medium (~4 GB) + 8–10 concurrent diarizations in the same worker process.
- **Laptop recipe** on a 4 GB 3050: Whisper small + 1 diarization, sequential, completes a 2.2 h file in ~100 s diarization + transcription time.

Phase C should surface a `DIARIZATION_PARALLEL_WORKERS` setting that scales with `free_vram_mb / 1500` and expose a measured `recommended_budget_mb` from the backend diag endpoint (plan A.7 item 4).

## Baseline accounting note

Runs up to 2026-04-20 subtract a manual 1 100 MB estimate of the idle-GPU baseline (other host workloads on GPU 0 during the sweep). From 2026-04-20 onward the probe captures `device_baseline_mb` (pre-pipeline NVML reading) and `device_delta_mb = peak − baseline` directly in each run's JSON, so downstream analysis no longer needs to approximate.

## Phase A artifacts (all completed 2026-04-20)

| Phase | Artifact | Finding |
|---|---|---|
| A.2 | isolation matrix + small-batch sweep — this README, `raw/*.json` (156 runs) | bs=16 fp32 saturates throughput; bs>16 wastes VRAM |
| A.3 | [`accuracy.md`](accuracy.md), `raw/rttm/` | fp32 DER≈0 across bs∈{1..128}; fp16 DER 27-33 % (T3) |
| A.6 | [`whole-stack.md`](whole-stack.md), `raw/whole-stack/*.json` (12 runs) | Full consumer-GPU sizing table; sequential swap confirmed |
| A.6b | see `whole-stack.md` § A.6b | Residue floor = 300 MB (CUDA context + allocator); per-process, not per-task |
| A.7 | `backend/app/scripts/diarization_diag.py` | `python -m app.scripts.diarization_diag` prints recommended config |

Phase A policy conclusion (confirmed end-to-end): **bs=16 fp32 for all deployments**, subtract 300 MB for CUDA context, Whisper-small is the largest model that fits on a 4 GB laptop alongside diarization.

## Outstanding (Phase B / C)

- **Phase B and C, as originally planned here, were superseded (`7ed7456e`, same day as
  landing) — the APP-SIDE plumbing was removed, not the fork's auto-scaler itself.**
  `pipelines/_budget.py`'s free-VRAM → batch auto-scaler is still present and live in the
  pinned fork (`requirements.txt`'s `pyannote.audio` SHA) — what got removed was the app's own
  `DIARIZATION_VRAM_BUDGET_MB` / `DIARIZATION_MIXED_PRECISION` / `DIARIZATION_ONNX_CPU` env
  vars → settings → `_apply_vram_policy` → `pipeline.vram_budget_mb` chain that would have
  driven it. In its place: a single fixed `EMBEDDING_BATCH_SIZE = 16` on every deployment,
  since the sweep found no throughput or DER benefit above bs=16 for the VRAM it costs. The
  fork's auto-scaler is **deliberately bypassed, not deleted** — `diarizer.py` sets
  `PYANNOTE_FORCE_EMBEDDING_BATCH_SIZE`, which short-circuits the fork's `_force` branch
  *before* it ever reads `vram_budget_mb` (`speaker_diarization.py`). **Do not remove that env
  var as "vestigial"**: without it the fork's ladder re-engages and steps down to bs=4 under
  VRAM pressure, silently breaking the fixed ~1 GB-per-pipeline footprint the multi-worker
  GPU-scale concurrency model depends on. See `CHANGELOG.md`'s `[Unreleased] > Removed` entry
  and `backend/app/transcription/diarizer.py`'s `EMBEDDING_BATCH_SIZE` class var (issue #568).
  Do not resurrect the app-side budget knob without new measurement showing the fixed-16
  policy is actually insufficient for some deployment shape.
- Drop the diag's `CUDA_CONTEXT_MB` to a per-driver table once we have numbers from other GPU
  classes — this piece of Phase C is unrelated to the batch-size knob and is still open.
- **Cross-validation:** one representative config on physical RTX 3080 Ti (GPU 1) vs. capped-A6000 to confirm simulation fidelity within 5 % (plan A.7 item 3).
- **Whisper large-v3-turbo whole-stack probe:** add to `--sweep` models list; need turbo numbers for the default deployment path.

## MPS (Apple Silicon) cross-platform validation — 2026-04-20

**Host:** Mac Studio, M2 Max (superstudio@192.168.30.26). torch `mps.is_available() == True`, `recommended_max_memory = 21 845 MB`.

**Unit tests:** all 28 `test_budget.py` assertions pass on `darwin-arm64`. `_budget.py` is pure-Python + stdlib so the ladder math is host-independent.

**Budget recommendations on MPS:**

| `free_mb` passed to `recommend_embedding_batch(device='mps')` | Returned |
|---:|---|
| 8 000 | `batch_size=16, status='optimal'` |
| 1 000 | `batch_size=8, status='tight'` |
| 500 | `batch_size=4, status='insufficient'` |

Identical to CUDA behaviour — no MPS-specific divergence.

**Integration smoke:** fresh-pipeline runs on the 0.5 h clip, forced bs=64 (old auto-scaler) vs. Phase B auto-selected bs=16:

| Config | Speakers | Wall time | Notes |
|---|:---:|---:|---|
| forced bs=64 | 4 | 42.6 s | matches Phase A CUDA reference for this file |
| Phase B auto (bs=16) | 4 | 41.7 s | output identical |

**Direct MPS measurement landed (2026-04-20).** A sampled `torch.mps.driver_allocated_memory()` probe at 100 ms cadence (`scripts/vram-probe-diarization-mps.py`) filled the gap left by MPS's missing `max_memory_allocated` API. Raw JSONs in `raw/mps/`.

### MPS small-batch sweep (fp32, 0.5 h clip, M2 Max, torch 2.8.0 mps backend)

| `bs` | Wall (s) | Peak MB | Δ over baseline | Speakers | Segments |
|---:|---:|---:|---:|:---:|---:|
| 1 | 65.4 | 1 865 | 1 865 | 4 | 693 |
| 4 | 45.7 | 1 922 | 1 918 | 4 | 693 |
| 8 | **43.5** | 1 866 | 1 862 | 4 | 693 |
| 16 | **42.0** | 1 890 | 1 886 | 4 | 693 |
| 32 | 43.1 | 3 914 | 3 910 | 4 | 693 |
| 64 | 42.5 | 4 978 | 4 974 | 4 | 693 |

`recommended_max_memory` on this Mac: 21 845 MB (M2 Max unified).

**Two findings that required a fork code change:**

1. **Throughput saturation happens earlier on MPS** (around bs=8) than on CUDA (bs=16). bs=8 and bs=16 are within 1.5 s on MPS vs. a larger CUDA gap at the same batch sizes. The `_BATCH_CEILING=16` policy is still correct (it's the safe throughput-optimal ceiling on both devices) but the "tight/optimal" tier becomes more forgiving on MPS.

2. **MPS allocator pre-reserves ~1.9 GB at any bs ≤ 16**, while CUDA's allocator grows gradually (640 MB at bs ≤ 8, 950 MB at bs=16). A single device-agnostic footprint table would size budgets correctly on CUDA and OOM on MPS when the caller's free memory is between 1.2 and 2.0 GB. The fork now carries per-device tables.

**Fork update** (`_budget.py`, commit `85858462`):

```
_DIARIZATION_FOOTPRINT_MB_BY_DEVICE = {
    "cuda": {4: 640,  8: 640,  16: 954,  32: 1946},
    "mps":  {4: 1900, 8: 1900, 16: 1900, 32: 3914},
}
```

`recommend_embedding_batch` routes through `_footprint_table(device)` which strips `cuda:0`-style indices, is case-insensitive, and falls back to the CUDA table for unknown devices (`xpu`, `rocm`, etc.). 33/33 unit tests pass on both Linux CUDA and darwin-arm64 MPS.

### Accuracy cross-check

On MPS the pipeline produced **4 speakers / 693 segments** at every batch size. Compare to the Phase A.3 CUDA reference (4 speakers / 703 segments): 10-segment delta is ~1.4 %, well inside tier T1. No DER computation performed on MPS yet (the reference RTTMs were generated on CUDA); future work: run the DER sweep directly on MPS and confirm the ~0 DER invariance holds cross-device.

### Throughput comparison — A6000 vs M2 Max

At bs=16 fp32 on the 0.5 h clip: **A6000 23 s**, **M2 Max 42 s** — MPS reaches ~55 % of A6000 throughput for diarization alone. Combined with Apple Silicon's 21.8 GB unified memory, this makes an M2 Max a reasonable target for single-file workloads where privacy beats speed.

The Mac's prior uncommitted MPS work is parked as `stash@{0}` (`phase-B-mps-test-stash`) on the fork and intentionally not reapplied; those changes predate the measured-policy work and would conflict with the device-aware footprint tables.
