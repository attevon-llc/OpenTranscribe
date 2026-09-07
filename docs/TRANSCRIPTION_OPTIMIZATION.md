# Transcription Pipeline Optimization

## Architecture Overview

OpenTranscribe uses a custom transcription pipeline (`backend/app/transcription/`) that bypasses WhisperX for the default path, using faster-whisper's `BatchedInferencePipeline` with native word timestamps + PyAnnote v4 diarization directly.

**Two engines available** (controlled by `TRANSCRIPTION_ENGINE` env var):

| Engine | Default | Description |
|--------|---------|-------------|
| `native` | Yes | faster-whisper BatchedInferencePipeline + PyAnnote v4 direct |
| `whisperx` | Fallback | Legacy WhisperX pipeline (uses same native word timestamps) |

### Native Pipeline Flow

```
Audio Load (decode_audio)
  -> Transcribe (BatchedInferencePipeline, word_timestamps=True, vad_filter=True)
  -> Release transcriber VRAM
  -> Diarize (PyAnnote v4 direct)
  -> Speaker Assignment (interval tree + NumPy, 273x faster than WhisperX)
  -> Segment Dedup (vectorized numpy, <0.2s)
  -> Result
```

### Key Design Decisions

1. **BatchedInferencePipeline + word_timestamps=True**: faster-whisper's batched pipeline provides native word-level timestamps via cross-attention DTW for all 100+ languages. No separate alignment model needed.

2. **Sequential VRAM mode**: Transcriber is loaded, used, and released before the diarizer loads. This keeps peak VRAM low (~5-9GB) for compatibility with 8-12GB GPUs.

3. **Warm model caching (ModelManager)**: Models persist between Celery tasks via a singleton. For batch imports of 2500 files, this saves ~10 hours of model loading overhead.

4. **PyAnnote v4 direct**: No monkey-patching via `pyannote_compat.py`. The native engine calls PyAnnote v4's API directly, handling the v4 output format (exclusive_speaker_diarization, overlap detection) natively.

5. **Silero VAD built-in**: faster-whisper's `vad_filter=True` uses Silero VAD internally. No separate VAD step needed.

### Package Structure

```
backend/app/transcription/
  __init__.py           # Public API: TranscriptionPipeline, TranscriptionConfig
  config.py             # Dataclass with env var + hardware detection
  audio.py              # load_audio via faster_whisper.decode_audio
  transcriber.py        # BatchedInferencePipeline wrapper (THE KEY FILE)
  diarizer.py           # PyAnnote v4 direct (no WhisperX wrapper)
  speaker_assigner.py   # Thin wrapper around fast_speaker_assignment.py
  model_manager.py      # Warm model caching singleton
  pipeline.py           # Orchestrates full pipeline
```

---

## Benchmark Results

All benchmarks use NVIDIA RTX A6000 (49GB, Ampere). Full end-to-end pipeline benchmarks (transcription + diarization) are documented in detail in `docs/BENCHMARK_RESULTS.md`.

**Key headline figures (v0.4.0, large-v3-turbo)**:
- Single-file realtime factor: **40.3x** (2.78hr file, full pipeline)
- Peak throughput: **54.6x** at concurrency=8
- Perfect linear scaling confirmed 1x through 12x workers
- VRAM ceiling: ~48.5GB at concurrency=10+

### Native Pipeline Comparison (JRE #2404, 3.3 hours, 11,893s)

| Configuration | Total | Transcription | Alignment | Diarization | Speaker Assign | Dedup |
|---------------|-------|---------------|-----------|-------------|----------------|-------|
| Old baseline (WhisperX + alignment ON) | **706s** | 75s | 389s | 194s | 10.2s | N/A |
| WhisperX batched, alignment OFF + dedup | **304s** | 76s | SKIPPED | 198s | 0.04s | <0.1s |
| Native word_ts, beam=1, int8, sequential | **437s** | 173s | N/A | 192s | 0.5s | 0.1s |
| **Native pipeline (batch_size=32, beam=5)** | **332s** | 105s | N/A | 192s | 0.5s | 0.1s |

### Quality Comparison

| Metric | WhisperX no-align + dedup | Native pipeline | Threshold |
|--------|--------------------------|-----------------|-----------|
| Text word overlap | 92.5% | ~92% | >85% |
| Speaker consistency | 76.7% (segment-level) | 95.2% (word-level) | >75% |
| Timestamp MAE | 0.00s start | 0.00s start | <5.0s |

**Key insight**: Native word timestamps give 95% speaker consistency (vs 77% without alignment) because word-level timestamps enable precise speaker-to-word matching via the interval tree algorithm.

### VRAM Usage

| Step | VRAM (observed) | Notes |
|------|-----------------|-------|
| Transcription (large-v3-turbo) | ~3-4 GB | BatchedInferencePipeline |
| Diarization (PyAnnote v4) | ~5-9 GB | Peak depends on audio length |
| Speaker embeddings | ~4.5-6.6 GB | Post-pipeline, separate step |
| **Peak (sequential mode)** | **~9 GB** | Transcriber released before diarizer |

### Scaling Projections (2500 Three-Hour Files)

| Configuration | Per File | 2500 Files | With 4x GPU Workers |
|---------------|---------|-----------|-------------------|
| Old baseline (WhisperX + alignment) | 706s | 490 hours | 123 hours |
| **Native pipeline** | **332s** | **231 hours** | **58 hours** |
| + warm model caching (batch import) | ~320s | 222 hours | 56 hours |
| + 4x GPU workers on A6000 | ~80s eff | 56 hours | N/A |

---

## Configuration Guide

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TRANSCRIPTION_ENGINE` | `native` | `native` or `whisperx` (fallback) |
| `WHISPER_MODEL` | `large-v3-turbo` | Whisper model name |
| `WHISPER_BEAM_SIZE` | `5` | Beam search width (1=greedy, faster; 5=default, better quality) |
| `WHISPER_COMPUTE_TYPE` | auto-detected | `float16`, `int8_float16`, `int8`, `float32` |
| `SOURCE_LANGUAGE` | `auto` | ISO language code or `auto` for detection |
| `MIN_SPEAKERS` / `MAX_SPEAKERS` | `1` / `20` | Speaker count range for diarization |
| `HUGGINGFACE_TOKEN` | none | Required for PyAnnote model access |
| `ENABLE_VRAM_PROFILING` | `false` | Log per-step GPU memory and timing |

#### WhisperX-only Variables (when TRANSCRIPTION_ENGINE=whisperx)

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_ALIGNMENT` | `false` | Legacy wav2vec2 alignment (deprecated, native timestamps used instead) |
| `PIPELINE_MODE` | `sequential` | `sequential` or `parallel` (parallel needs 12GB+ VRAM) |

### Recommended Configurations

**Maximum Speed (batch processing)**:
```env
TRANSCRIPTION_ENGINE=native
WHISPER_MODEL=large-v3-turbo
# beam_size and compute_type auto-optimized by hardware detection
```

**Maximum Quality**:
```env
TRANSCRIPTION_ENGINE=native
WHISPER_MODEL=large-v3
# Native word timestamps via cross-attention DTW for all languages
```

**Low VRAM (8GB GPU)**:
```env
TRANSCRIPTION_ENGINE=native
WHISPER_MODEL=large-v3-turbo
# batch_size auto-detected to 8 for 8GB GPUs
# Sequential mode keeps peak VRAM under 9GB
```

**No-GPU / Cloud ASR (DEPLOYMENT_MODE=lite)**:
```env
DEPLOYMENT_MODE=lite
# Disables local GPU worker. All transcription via cloud ASR provider.
# Configure LLM_PROVIDER + cloud ASR keys in .env
```

### Admin-Pinned Model

The active Whisper model is **admin-controlled** and shared across all users. An admin sets the model via **Settings → ASR → Local Model** in the UI. The model is loaded once into GPU VRAM at worker startup and remains warm between tasks (GPU pool type: `threads`, single warm model shared by all concurrent tasks).

To switch models:
1. Admin selects the new model in Settings → ASR → Local Model
2. Click **Restart Worker** — the worker drains in-flight tasks, then restarts
3. New model loads on the next task (or immediately if pre-loading is enabled)

Per-user model override is removed. All users share the admin-set model. Users may still select base/tiny models per-upload (these route to the CPU worker).

### Dual-Model Architecture

| Worker | Models | Pool |
|--------|--------|------|
| `celery-worker` (GPU) | medium, large-v2, large-v3, large-v3-turbo | threads |
| `celery-worker-cpu` | tiny, base | prefork |

Small models (tiny, base) are fast enough on CPU and avoid wasting GPU VRAM for quick transcriptions. The routing is automatic based on the requested model.

### Diarization Provider

The diarization source is independently configurable via `diarization_source`:

| Value | Description |
|-------|-------------|
| `local` | PyAnnote v4 (default, runs on GPU) |
| `pyannote.ai` | Cloud diarization via pyannote.ai API |
| `off` | No diarization (transcription-only mode) |

### Cloud ASR Providers

8 cloud ASR providers are available as alternatives to local GPU processing:

| Provider | Notes |
|----------|-------|
| Deepgram | Free tier available, no CC required |
| AssemblyAI | Free tier available |
| OpenAI Whisper API | Pay-per-use |
| Google Cloud Speech | Pay-per-use |
| Azure Cognitive | Pay-per-use |
| AWS Transcribe | Pay-per-use |
| Speechmatics | Pay-per-use |
| Gladia | Free tier available |

Cloud ASR is configured per-user (each user manages their own API key). Cloud providers bypass local GPU processing entirely.

### Whisper Model Selection

| Model | Speed | VRAM | English | Multilingual | Translation |
|-------|-------|------|---------|--------------|-------------|
| `large-v3-turbo` | 6x faster | ~6GB | Excellent | Good | **NO** |
| `large-v3` | Slow | ~10GB | Excellent | Best | Yes |
| `large-v2` | Slow | ~10GB | Excellent | Good | Yes |

**Warning**: `large-v3-turbo` cannot translate. If "Translate to English" is needed, use `large-v3`.

---

## Optimization History & Key Findings

### Why We Built the Native Pipeline

WhisperX was the original transcription backend but had architectural constraints:

1. **Alignment bottleneck**: The old wav2vec2 alignment step took 389s (55% of total) for a 3hr file, processing segments sequentially with 300+ separate CUDA kernel launches. This is now eliminated by native word timestamps.

2. **No batched word timestamps**: WhisperX deliberately hardcodes `word_timestamps=False` in its batched pipeline (`asr.py` lines 370-372). You had to choose: batched speed OR word timestamps.

3. **Monkey-patching required**: PyAnnote v4 API changes required a 465-line compatibility layer (`pyannote_compat.py`) that monkey-patched WhisperX's diarization internals.

### What We Tried (Chronological)

| Approach | Result | Outcome |
|----------|--------|---------|
| Disable alignment + segment dedup | 316s, 99.5% quality match | Good speed, but only 77% speaker consistency (segment-level timestamps) |
| Parallel alignment + diarization | 579s, GPU contention 16-24% | Not worth it on same GPU |
| Batched wav2vec2 alignment | ~420s estimated | Implemented but still slow (now removed) |
| Native word_timestamps (sequential) | 437s, 95% speaker consistency | Good quality but slower than batched |
| **BatchedInferencePipeline + word_timestamps** | **332s, 95% speaker consistency** | **Best of both worlds** |

The breakthrough was discovering that faster-whisper 1.2.1's `BatchedInferencePipeline.transcribe()` supports `word_timestamps=True` — batched speed with word-level timestamps. WhisperX didn't expose this.

### Segment Dedup: Why It's Needed

The transcription pipeline produces both coarse VAD-chunked segments AND fine-grained subsegments for the same time ranges due to batched inference chunking.

The dedup module (`backend/app/utils/segment_dedup.py`) handles this:
1. NLTK punkt sentence splitting (replicates what alignment implicitly provided)
2. Containment detection — removes coarse "parent" segments covered by finer children
3. Exact text duplicate removal
4. Time+text overlap merging

Performance: <0.2s for 3000+ segments (vectorized numpy). Quality: 99.5% match to aligned baseline.

### Fast Speaker Assignment

The original WhisperX `assign_word_speakers()` used O(n) linear scan per word. Our replacement
(`backend/app/transcription/speaker_assigner.py`, formerly `backend/app/utils/fast_speaker_assignment.py`
— an interval-tree version measured below; since replaced with a fully vectorized numpy
implementation) computes speaker assignment via numpy broadcasting instead of a per-word loop.

- WhisperX: 10.2s for 150 segments, 1349 words
- Interval-tree version (superseded): 0.037s (273x faster)
- Current vectorized-numpy version: ~13x speedup on long-form content (80s → 6s for a 4.7hr
  file, 54K words) — see the module docstring for current numbers.

### Warm Model Caching

The `ModelManager` singleton (`backend/app/transcription/model_manager.py`) keeps models loaded between Celery tasks. Since the GPU worker has concurrency=1, only one task runs at a time — safe for singleton model state.

| Scenario | Model Loading Overhead |
|----------|----------------------|
| Without cache (per-file load/unload) | 2500 files x 15s = 10.4 hours |
| With warm cache (first file only) | 15s + 2500 x ~0s = 15 seconds |

---

## CTranslate2 / faster-whisper Tuning Reference

These parameters affect transcription speed. Relevant for both native and whisperx engines.

### compute_type

| Type | GPU Support | Speed | Quality |
|------|-------------|-------|---------|
| `float16` (default on CUDA) | Compute >=7.0 | Fast | Baseline |
| `int8_float16` | Compute >=7.0 | ~20-30% faster | -0.1 WER |
| `int8` | Compute >=6.1 | Fastest | -0.1 WER |
| `bfloat16` | Compute >=8.0 | Fast | Negligible loss |

### beam_size

| Value | Speed | Quality |
|-------|-------|---------|
| 1 (greedy) | ~2-3x faster | -1-2% WER |
| 2 | ~1.5x faster | -0.5% WER |
| 5 (default) | Baseline | Baseline |

### batch_size (auto-detected by hardware_detection.py)

| GPU VRAM | batch_size | Notes |
|----------|------------|-------|
| 48GB+ (A6000) | 32 | Optimal |
| 24GB (RTX 3090) | 24 | Optimal |
| 12GB (RTX 3080) | 12 | Could push to 16 with turbo model |
| 8GB (RTX 3070) | 8 | Test 12 with turbo model |

---

## PyAnnote Diarization Notes

- **Non-deterministic**: PyAnnote uses neural embeddings + agglomerative clustering. Small floating-point variations between runs can shift speaker boundaries, especially at edges.
- **No hard speaker limit**: `AgglomerativeClustering` has no maximum — `MAX_SPEAKERS=50+` works for large events.
- **VRAM scales with audio length**: ~5GB for short files, up to ~9GB for 3+ hour files.
- **Model variants**: v4 community model (`pyannote/speaker-diarization-community-1`) preferred, with v3.1 fallback.

---

## Files Reference

### Native Pipeline (default)

| File | Purpose |
|------|---------|
| `backend/app/transcription/pipeline.py` | Main orchestrator |
| `backend/app/transcription/transcriber.py` | BatchedInferencePipeline + word_timestamps |
| `backend/app/transcription/diarizer.py` | PyAnnote v4 direct |
| `backend/app/transcription/model_manager.py` | Warm model caching singleton |
| `backend/app/transcription/config.py` | Configuration from env + hardware |
| `backend/app/transcription/audio.py` | Audio loading (decode_audio) |
| `backend/app/transcription/speaker_assigner.py` | Vectorized numpy speaker assignment |

### Shared Utilities

| File | Purpose |
|------|---------|
| `backend/app/transcription/speaker_assigner.py` | Vectorized numpy speaker assignment (~13x faster) |
| `backend/app/utils/segment_dedup.py` | Vectorized segment dedup + sentence splitting |
| `backend/app/utils/hardware_detection.py` | GPU detection, batch_size, compute_type |
| `backend/app/utils/vram_profiler.py` | Per-step VRAM and timing profiler |

### WhisperX Fallback (Legacy, Removed)

The following files were part of the legacy WhisperX fallback pipeline and have been removed:

| File | Purpose | Status |
|------|---------|--------|
| `backend/app/tasks/transcription/whisperx_service.py` | Legacy WhisperX pipeline | Removed |
| `backend/app/utils/pyannote_compat.py` | PyAnnote v4 monkey-patching for WhisperX | Removed |
| `backend/app/utils/batched_alignment.py` | Batched wav2vec2 alignment | Removed |
| `backend/app/tasks/transcription/parallel_pipeline.py` | Parallel alignment+diarization | Removed |

### Benchmarking Tools

| File | Purpose |
|------|---------|
| `backend/app/tasks/baseline_export.py` | Export/compare transcript snapshots |
| `backend/app/utils/transcript_comparison.py` | Vectorized transcript comparison |
