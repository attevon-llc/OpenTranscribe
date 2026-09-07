# GPU Pipeline Optimization Plan

**Discovered:** 2026-04-26 during upload-speed A/B benchmark
**Updated:** 2026-04-26 — expanded after pipeline architecture review
**Status:** Documented — not yet implemented
**Priority:** Ordered by effort/impact ratio (tackle top-down)

---

## Context: Why Upload Speed Improvements Were Small (v362 Benchmark)

The upload-speed branch (`feat/upload-speed-improvement`, v362) improved HTTP upload
latency by ~29% average (streaming validation, MinIO part-size tuning, presigned direct
uploads, single DB commit on intake, parallel preprocessing). However, end-to-end wall
clock improved only 1-5%:

- 0.5h file: 0% improvement (upload is 4% of total time)
- 1.0h file: 5% improvement (upload is 4% of total time)
- 4.7h file: 1% improvement (upload is 3% of total time)

GPU transcription + diarization accounts for 95-97% of total wall clock. Any optimization
that touches only the upload or CPU path faces a hard ceiling imposed by Amdahl's Law:
improving 5% of the pipeline by 100% yields at most a 5% total improvement. The
optimizations below target the GPU-blocking CPU work and VRAM bottlenecks that directly
affect GPU utilization — the 95% that matters for throughput.

Raw benchmark data: `docs/benchmark-results/`
Hardware: RTX A6000 49 GB, CUDA 13.0, large-v3-turbo int8_float16, batch_size=32

---

## Current Architecture (as of v362)

The pipeline already runs as a 3-stage Celery chain:

```
Stage 1 — CPU Preprocess   (queue: cpu,  ~15s)
Stage 2 — GPU Transcribe   (queue: gpu,  ~50-170s)
Stage 3 — CPU Postprocess  (queue: cpu,  ~5-80s)
```

### Stage 2 Sub-Steps (GPU Worker — measured on RTX A6000)

```
GPU Worker holds the slot for the entire duration below:

[I/O]  Download preprocessed WAV from MinIO temp          (~2-5s)
[GPU]  Whisper transcription — BatchedInferencePipeline   (~30-120s)  batch_size=32
[GPU]  PyAnnote diarization — segmentation + embedding    (~10-40s)   seg. batch VRAM-aware
[CPU]  assign_speakers() — map words to speaker labels    (~1-5s)
       ─── GPU IS NOW IDLE — but worker slot still blocked ───
[CPU]  resegment_by_speaker + merge_consecutive_segments  (~1-3s)
[DB]   create_speaker_mapping (1 DB round-trip)           (~1-2s)
[CPU]  process_segments_with_speakers                     (~1-3s)
[CPU]  mark_overlapping_segments                          (<1s)
[CPU]  clean_garbage_words                                (<1s)
[DB]   save_transcript_segments (bulk insert segs+words)  (~2-5s)
[DB]   update_media_file_transcription_status             (<1s)
[GPU]  optimize_memory_usage() — GPU VRAM finally freed   (~1s)
       ─── GPU worker slot released, next file can start ───

Total CPU/DB work after GPU is idle: core.py:_process_and_save_critical() = 6-16s
```

### VRAM Profile (observed on RTX A6000 49 GB, large-v3-turbo int8_float16)

| Phase | Stable VRAM | Peak VRAM | Root Cause |
|-------|-------------|-----------|------------|
| Idle (models loaded) | ~1.5 GB | — | turbo + pyannote preloaded |
| WhisperX transcription | 4-9 GB | ~9 GB | batch=32: encoder activations + attention maps + KV cache (beam_size=5) simultaneously for 32 segments |
| PyAnnote diarization | ~1 GB | ~2 GB | segmentation batch is VRAM-aware; embedding pinned at 16 |
| _process_and_save_critical | 0 GB GPU active | — | GPU idle but worker slot blocked |
| CPU postprocess | 0 GB | — | GPU worker already released |

---

## Core Challenge: Pre/Post GPU Work Is Baked Into Third-Party Pipelines

The WhisperX and PyAnnote libraries each bundle their own CPU pre-processing and
post-processing steps directly inside their inference calls. You cannot simply call
"GPU-only transcription" or "GPU-only diarization" — the pipelines are designed as
monolithic objects that accept audio in, return results out, handling all internal
CPU work themselves.

**WhisperX / faster-whisper internal stages:**
1. [CPU] Audio resampling to 16 kHz (if not already done)
2. [CPU] Log-mel spectrogram computation per chunk
3. [GPU] Encoder forward pass (all chunks in batch)
4. [GPU] Beam-search decode (KV cache, beam_size=5)
5. [CPU] Tokenizer decode, word alignment, timestamp assignment
6. [CPU] VAD filtering, silence suppression

**PyAnnote diarization internal stages:**
1. [CPU] Audio chunking into overlapping windows
2. [GPU] Segmentation model — frame-level speaker activity scores
3. [CPU] Binarize segmentation into speaker segments
4. [GPU] Embedding model — per-segment speaker embeddings (batch=16 pinned)
5. [CPU] AgglomerativeClustering to assign SPEAKER_00, SPEAKER_01, ...
6. [CPU] Overlap detection, annotation to timeline

The CPU work in steps 1-2 and 5-6 of each library IS interleaved with GPU steps and
cannot be extracted without modifying the upstream library code (which we do for PyAnnote
via `davidamacey/pyannote-audio@gpu-optimizations` fork).

**What we CAN decouple (without upstream library changes):**

Everything in `_process_and_save_critical()` in `core.py:1989`. This function runs on the
GPU worker AFTER BOTH library pipelines have fully returned their results. It is pure
application-layer code — no dependency on GPU or library internals — and it blocks the GPU
worker slot for 6-16s while doing CPU processing and DB writes.

---

## Optimization 1 — Move `_process_and_save_critical()` Off the GPU Worker

**Effort:** Medium | **GPU Idle Saved:** 6-16s per file | **Priority:** 1st to implement

### Problem

`_process_and_save_critical()` (`core.py:1989`) runs ~6-16s of CPU and DB work on the GPU
worker after BOTH WhisperX and PyAnnote have fully returned. The GPU worker slot stays
blocked until this completes, preventing the next file's GPU transcription from starting.

```
CURRENT (v362):
GPU slot: [download] [whisper] [pyannote] [assign] [IDLE: resegment+save_db+mem_free]
CPU slot:                                                                             [postprocess N]
File N+1:                                                        blocked waiting for GPU slot ──────→

PROPOSED:
GPU slot: [download] [whisper] [pyannote] [assign] [mem_free]
CPU slot:                                  chain starts → [resegment+save_db] [postprocess N]
File N+1:                                          GPU slot free ~10s sooner ↑
```

For a batch of 10 files: saves 60-160s total (6-16s x 10 files).
For a single 4.7h file (568s total): saves ~2-3% end-to-end.
For small files (0.5h, 51s): saves ~10-15% end-to-end.

### Why It Is Safe to Move

`_process_and_save_critical()` only needs:
- The raw pipeline result dict (segments list, native embeddings, language, overlap_info)
- The preprocessing context (file_id, user_id, task_id)
- A database connection (already available on CPU workers)

None of these require the GPU worker. The raw pipeline result is already JSON-serializable
(segments are dicts; native embeddings are serialized via `.tolist()` at the end of
`_process_and_save_critical` today — move that serialization to before the function call
in the GPU task so the chain can carry the data over Redis).

The only thing that MUST stay on the GPU worker is `optimize_memory_usage()` — VRAM must
be freed before the next file's GPU task can load its audio. This call (~1s) stays in
Stage 2 and is called immediately after the pipeline returns, before the chain return.

### Implementation: What Moves Where

**Stage 2 GPU task after refactor:**
```python
# Run GPU pipeline (WhisperX + PyAnnote — unchanged)
result = pipeline.process(audio)

# Assign speakers to segments (CPU, 1-5s, stays here because
# it reads directly from pipeline result before memory is freed)
result = assign_speakers(result)

# Release GPU VRAM immediately — next file can start
hardware_config.optimize_memory_usage()

# Serialize native embeddings (numpy arrays → lists) for Redis transfer
if result.get("native_speaker_embeddings"):
    result["native_speaker_embeddings"] = {
        label: emb.tolist()
        for label, emb in result["native_speaker_embeddings"].items()
    }

# Return raw result — _process_and_save_critical() happens in Stage 3
return _build_gpu_chain_context(result, preprocess_context)
```

**Stage 3 CPU postprocess task (new first step):**
```python
def finalize_transcription(self, gpu_context: dict) -> None:
    # NEW: run the CPU/DB processing that used to block the GPU worker
    _process_and_save_critical(ctx, raw_result, preprocess_context)

    # ... rest of existing finalize_transcription() logic (embeddings, notify, enrichment)
```

**Chain context size:** A 4.7h file has ~2,000-5,000 segments. At ~300-500 bytes per
segment dict that is 0.6-2.5 MB per Redis message. Well within Redis's 512 MB default
message limit. Native embeddings are ~8 KB per speaker (256 float64 values).

### Files to Change

| File | Change |
|------|--------|
| `backend/app/tasks/transcription/core.py:1989` | `_process_and_save_critical()` — move function body to `postprocess.py`. GPU task calls `optimize_memory_usage()` and returns raw result directly. |
| `backend/app/tasks/transcription/postprocess.py:45` | `finalize_transcription()` — call `_process_and_save_critical()` as first step before existing embedding/notify logic. |
| `backend/app/tasks/transcription/notifications.py` | Progress 68-78% markers move from GPU task to postprocess task. |
| `backend/app/tasks/transcription/dispatch.py` | Chain structure unchanged (still 3 stages). |

### Progress Notification Remapping

| Progress | Message | Currently In | Move To |
|----------|---------|--------------|---------|
| 68% | "Processing speaker segments" | GPU task | Stage 3 postprocess |
| 72% | (speaker mapping) | GPU task | Stage 3 postprocess |
| 75% | "Saving transcript to database" | GPU task | Stage 3 postprocess |
| 78% | (update task status) | GPU task | Stage 3 postprocess |

---

## Optimization 2 — WhisperX Batch Size Empirical Study (Phase B)

**Effort:** Small (script) + Small (config) | **VRAM Impact:** High | **Priority:** Run first (diagnostic only, no code change)

### Background

PyAnnote diarization underwent a rigorous Phase A study (April 2026) that pinned
embedding batch size at 16 — throughput saturates at batch=16 with <3% wall-time
difference vs batch=128, but VRAM drops from 7 GB to 1 GB. That study covered batch
sizes 1-128 on multiple GPUs with 5-run statistical confidence intervals.

WhisperX has **no equivalent study**. The current VRAM-aware auto-selector
(`hardware_detection.py:119`) was written by intuition with thresholds NOT validated
by measurement:

```python
if memory_gb >= 40:   return 32   # A6000/A100
elif memory_gb >= 24: return 24
elif memory_gb >= 16: return 16
elif memory_gb >= 12: return 12   # problem: turbo may still spike >4GB here
elif memory_gb >= 8:  return 8    # problem: turbo at batch=8 VRAM not measured
elif memory_gb >= 6:  return 4
else:                 return 2
```

Benchmarks show 4-9 GB VRAM spikes at batch=32 on the A6000. That spike pattern
leaves only 3 GB headroom on a 12 GB GPU — dangerously close to OOM when PyAnnote
segmentation (1-3 GB) runs immediately after. Without measured data we cannot safely
set batch sizes for 4-8 GB GPU deployments.

### The Internal CPU Work Inside WhisperX Cannot Be Separated

As noted in the Core Challenge section, WhisperX's CPU pre/post steps (mel spectrogram
computation, tokenizer decode, word alignment) are interleaved inside the library's
inference call. We cannot hoist them out. What we CAN control is batch size — it
directly governs how many encoder activations, attention maps, and KV cache entries
are allocated simultaneously on GPU.

Smaller batch = less peak VRAM = slower throughput (more kernel launches, less GPU
parallelism). The study determines where the throughput cliff is relative to the VRAM
savings, model by model — the same question Phase A answered for diarization.

### Agent-Runnable Diagnostic Script

**Create:** `backend/app/scripts/whisper_batch_diag.py`

Modeled after `backend/app/scripts/diarization_diag.py`.

**Run inside the celery-worker container:**
```bash
docker exec -it opentranscribe-celery-worker \
  python -m app.scripts.whisper_batch_diag \
    --audio /app/benchmark/test_audio/2.2h_7998s.wav \
    --model large-v3-turbo \
    --batch-sizes 4,8,12,16,24,32 \
    --compute-type int8_float16 \
    --output /tmp/whisper_batch_results.csv
```

**Script logic:**

```python
"""
Whisper batch size vs VRAM diagnostic.
Usage: python -m app.scripts.whisper_batch_diag --help
"""
import argparse, csv, time, gc
import numpy as np
import torch
from app.transcription.audio import load_audio_from_file
from app.transcription.config import TranscriptionConfig
from app.transcription.transcriber import Transcriber

FIELDS = [
    "model", "compute_type", "batch_size", "audio_duration_s",
    "vram_stable_before_mb", "vram_peak_mb", "vram_stable_after_mb",
    "wall_time_s", "rtf", "status",
]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", required=True)
    parser.add_argument("--model", default="large-v3-turbo")
    parser.add_argument("--batch-sizes", default="4,8,12,16,24,32")
    parser.add_argument("--compute-type", default="int8_float16")
    parser.add_argument("--output", default="/tmp/whisper_batch_results.csv")
    args = parser.parse_args()

    audio = load_audio_from_file(args.audio)
    duration = len(audio) / 16000.0
    batch_sizes = [int(b) for b in args.batch_sizes.split(",")]
    rows = []

    print(f"Model: {args.model}  compute_type: {args.compute_type}")
    print(f"Audio: {args.audio}  duration: {duration:.1f}s")
    print(f"GPU: {torch.cuda.get_device_name(0)}")
    print(f"{'batch':>6}  {'peak MB':>9}  {'stable MB':>10}  {'wall s':>8}  {'RTF':>7}  status")

    for bs in batch_sizes:
        torch.cuda.empty_cache(); gc.collect()
        torch.cuda.reset_peak_memory_stats()

        cfg = TranscriptionConfig(
            model_name=args.model,
            batch_size=bs,
            compute_type=args.compute_type,
        )
        t = Transcriber(cfg)
        t.load()
        stable_before = torch.cuda.memory_allocated() / 1024**2

        t0 = time.perf_counter()
        try:
            t.transcribe(audio)
            elapsed = time.perf_counter() - t0
            peak = torch.cuda.max_memory_allocated() / 1024**2
            stable_after = torch.cuda.memory_allocated() / 1024**2
            rtf = elapsed / duration
            status = "ok"
        except torch.cuda.OutOfMemoryError:
            elapsed = peak = stable_after = rtf = None
            status = "OOM"

        print(f"{bs:>6}  {peak or 0:>9.0f}  {stable_after or 0:>10.0f}  "
              f"{elapsed or 0:>8.1f}  {rtf or 0:>7.3f}  {status}")
        rows.append({
            "model": args.model, "compute_type": args.compute_type,
            "batch_size": bs, "audio_duration_s": round(duration, 1),
            "vram_stable_before_mb": round(stable_before, 0),
            "vram_peak_mb": round(peak, 0) if peak else None,
            "vram_stable_after_mb": round(stable_after, 0) if stable_after else None,
            "wall_time_s": round(elapsed, 2) if elapsed else None,
            "rtf": round(rtf, 4) if rtf else None,
            "status": status,
        })
        del t; torch.cuda.empty_cache(); gc.collect()

    with open(args.output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader(); writer.writerows(rows)
    print(f"\nSaved: {args.output}")

if __name__ == "__main__":
    main()
```

**Run on both GPUs:**
```bash
# A6000 (full curve)
CUDA_VISIBLE_DEVICES=0 docker exec opentranscribe-celery-worker \
  python -m app.scripts.whisper_batch_diag \
    --model large-v3-turbo --batch-sizes 2,4,8,12,16,24,32 \
    --output /tmp/whisper_batch_a6000.csv

# 3080 Ti (12 GB — critical for deployment targets)
CUDA_VISIBLE_DEVICES=1 docker exec opentranscribe-celery-worker \
  python -m app.scripts.whisper_batch_diag \
    --model large-v3-turbo --batch-sizes 2,4,8,12,16 \
    --output /tmp/whisper_batch_3080ti.csv
```

**Expected columns:**
`model, compute_type, batch_size, audio_duration_s, vram_stable_before_mb,
 vram_peak_mb, vram_stable_after_mb, wall_time_s, rtf, status`

### What to Do With Results

1. Save CSVs to `docs/whisper-vram-profile/` (create directory).
2. Write `docs/whisper-vram-profile/README.md` following the same format as
   `docs/diarization-vram-profile/README.md`.
3. Update `hardware_detection.py:_get_optimal_batch_size()` with model-name-aware
   VRAM budget thresholds — the same pattern used for diarization:

```python
WHISPER_VRAM_BUDGET_MB = {
    # Filled in from whisper_batch_diag results
    # Rule: peak_vram <= 0.80 x total_vram (leave 20% for diarization headroom)
    "large-v3-turbo": {32: None, 16: None, 8: None, 4: None},  # TBD
    "large-v3":       {32: None, 16: None, 8: None, 4: None},
    "medium":         {32: None, 16: None, 8: None, 4: None},
    "small":          {32: None, 16: None, 8: None, 4: None},
}

def _resolve_batch_size_for_model(model_name: str, available_vram_mb: int) -> int:
    budgets = WHISPER_VRAM_BUDGET_MB.get(model_name, {})
    for batch in [32, 16, 8, 4, 2]:
        required = budgets.get(batch)
        if required is not None and required <= available_vram_mb * 0.80:
            return batch
    return 2  # safe fallback for very small GPUs
```

### Files to Change

| File | Change |
|------|--------|
| `backend/app/scripts/whisper_batch_diag.py` | NEW — diagnostic script (see above) |
| `backend/app/utils/hardware_detection.py:119` | Update `_get_optimal_batch_size()` with validated VRAM budgets |
| `backend/app/transcription/config.py:80` | Rename env var from `BATCH_SIZE` to `WHISPER_BATCH_SIZE` (keep `BATCH_SIZE` alias for back-compat) |
| `backend/app/core/config.py:414` | Add `WHISPER_BATCH_SIZE` field alongside existing `BATCH_SIZE` |
| `.env.example` | Add `WHISPER_BATCH_SIZE=auto` with GPU-target comments |
| `docs/whisper-vram-profile/README.md` | NEW — study results (create after running script) |

---

## Optimization 3 — Further CPU/GPU Decoupling Within Library Calls

**Effort:** Large | **Impact:** Variable | **Priority:** Low

### What Is Baked Into the Libraries

The CPU work inside WhisperX and PyAnnote cannot be hoisted out without modifying the
upstream library code. For reference, here is what each library does internally:

**WhisperX/faster-whisper (sequential, interleaved CPU+GPU):**
```
[CPU] Audio chunking + mel spectrogram computation   (depends on batch_size windows)
[GPU] Encoder forward pass for all chunks in batch
[GPU] Beam-search KV cache decode (beam_size=5 paths)
[CPU] CTC prefix beam search / greedy decode
[CPU] Word-level timestamp alignment via cross-attention DTW
[CPU] Post-processing: token decode, confidence scores
```

**PyAnnote diarization (sequential, interleaved CPU+GPU):**
```
[CPU] Audio resampling and window chunking
[GPU] SpeakerSegmentation model — frame-level scores
[CPU] Binarize + threshold segmentation frames
[GPU] WeSpeaker embedding model — one embedding per segment (batch=16)
[CPU] AgglomerativeClustering — map segments to speakers
[CPU] Overlap detection pass
```

Our PyAnnote fork (`davidamacey/pyannote-audio@gpu-optimizations`) already extracts
native 256-dim centroids alongside the standard output, vectorizes the clustering
operations, and pins the embedding batch size. Further optimization within PyAnnote
would require streaming audio to the segmentation model incrementally (overlapping
Whisper decoding with PyAnnote segmentation for the same file).

### Concrete Opportunity: Audio Prefetch Off the GPU Worker

The GPU worker currently downloads the preprocessed WAV from MinIO temp as the FIRST
step (~2-5s before GPU work starts). This download could overlap with the time the GPU
worker is picking up the task from the queue.

**Option A — Shared temp volume:**
Stage 1 (CPU preprocess) writes the WAV to a shared Docker volume AND uploads to MinIO.
Passes the local path in the chain context. GPU worker reads from the shared path
(zero network I/O). Saves 2-5s per file.

Prerequisite: GPU worker and CPU worker containers must share a `/tmp/transcription/`
volume mount. Verify in `docker-compose.yml` before implementing.

> **2026-09-04 correction:** this "Option A" prerequisite shipped, in consolidated form, as
> issue #661's `pipeline_scratch` volume (mounted at `/scratch/opentranscribe` on every
> pipeline-touching worker, replacing the separate `transcription-temp` and `diar-native-tmp`
> volumes this plan predates). The path named above is stale; see `docs/combined-engine-design.md`
> and `docs/ARCHITECTURE.md` for the current layout. Left as-is otherwise — this document
> records a point-in-time plan, not live behavior.

**Option B — Async download on task receipt:**
GPU worker starts a background `ThreadPoolExecutor` thread to download the WAV the
moment the task is received (during Celery task setup, before the function body runs).
Meanwhile the main thread checks model readiness and resolves settings. The audio is
ready by the time GPU processing starts.

This is a smaller change than Option A and does not require compose changes.

---

## Optimization 4 — Whisper/PyAnnote GPU Overlap (Research)

**Effort:** Very Large | **Impact:** +10-20% single-file | **Priority:** Research only

Within Stage 2, Whisper and PyAnnote run strictly sequentially. During PyAnnote's
segmentation phase (~8-12s), GPU utilization is low. The two models could partially
overlap if Whisper output is chunked and streamed to PyAnnote incrementally rather
than waiting for full transcription to complete.

This requires a significant refactor of the pipeline abstraction and has not been
attempted. Listed here as a future research direction.

---

## Full Before/After Pipeline Diagram

```
LEGEND: [GPU] = GPU compute  [CPU] = CPU only  [I/O] = network/disk

CURRENT v362 — GPU worker slot blocked for full duration:
        GPU slot ←────────────────────────────────────────────────────────────────→
Stage2: [I/O:download] [GPU:whisper] [GPU:pyannote] [CPU:assign] [CPU+I/O:save+db] [GPU:mem_free]
Stage3:                                                                              [CPU:embed+notify+enrichment→]
Next file GPU start:                                                                              ↑ unblocked here

AFTER OPT-1 — GPU worker slot shortened by 6-16s per file:
        GPU slot ←──────────────────────────────────────────────→
Stage2: [I/O:download] [GPU:whisper] [GPU:pyannote] [CPU:assign] [GPU:mem_free]
Stage3:                              chain triggers here → [CPU:save+db] [CPU:embed+notify+enrichment→]
Next file GPU start:                                                  ↑ unblocked ~10s sooner

AFTER OPT-1 + OPT-3B (async download) — GPU slot shortened further:
        GPU slot ←─────────────────────────────────────────→
Stage2: [I/O:async download + GPU warmup overlap] [GPU:whisper] [GPU:pyannote] [GPU:mem_free]
Stage3:                                                          chain → [CPU:save+db] [CPU:embed+notify→]

AFTER ALL OPTS + validated batch_size (8GB GPU with large-v3-turbo):
        GPU slot ←──────────────────────────────────────────────────────────→
Stage2: [I/O:async] [GPU:whisper bs=8, ~3GB VRAM] [GPU:pyannote] [GPU:mem_free]
Stage3:                                           chain → [CPU:save+db] [CPU:embed+notify→]
        ↑ OOM-safe on 8GB GPU
```

---

## Summary Table

| Optimization | Effort | GPU Idle Saved | VRAM Impact | Enables 4-8 GB GPU | Order |
|-------------|--------|----------------|-------------|---------------------|-------|
| Opt-2: Run `whisper_batch_diag.py` | Tiny (script only) | None | Documents reality | Prerequisite | Run first |
| Opt-2: Apply validated batch thresholds | Small | None | Prevents OOM on small GPUs | Yes | 2nd |
| Opt-1: Move `_process_and_save_critical` to CPU | Medium | 6-16s/file | None | No | 3rd |
| Opt-3B: Async audio prefetch on GPU worker | Small | 2-5s/file | None | No | 4th |
| Opt-3A: Shared volume audio prefetch | Medium | 2-5s/file | None | No | Alt to 3B |
| Opt-4: Whisper/PyAnnote GPU overlap | Very Large | 8-12s/file | None | No | Research |

**Recommended sequence:**

1. **Write and run `whisper_batch_diag.py`** — no production code change. Produces the
   measured VRAM table needed to safely configure all GPU classes.
2. **Update `_get_optimal_batch_size()`** with validated thresholds. Add `WHISPER_BATCH_SIZE`
   env var. This is the single change that enables 4-8 GB GPU deployments without OOM.
3. **Move `_process_and_save_critical()` to Stage 3** — frees GPU 6-16s sooner per file.
   Most impactful for workloads with many small files queued.
4. **Async audio prefetch** (Opt-3B) — small refactor, 2-5s saved per file.
5. **Whisper/PyAnnote overlap** — only after the above ship and only if GPU utilization
   remains a bottleneck at scale.
