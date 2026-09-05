---
sidebar_position: 5
title: Performance Tuning
description: Optimize transcription speed, GPU utilization, and system performance
---

# Performance Tuning

This guide covers tuning OpenTranscribe for maximum throughput on your hardware. The transcription pipeline, GPU utilization, database, search, and task queue settings all contribute to overall performance.

## Transcription Performance

### Batch Size Tuning

Batch size controls how many audio chunks the Whisper encoder processes simultaneously. It is the single most impactful transcription speed parameter. OpenTranscribe auto-detects the optimal batch size based on GPU VRAM, but you can override it.

| GPU VRAM | Auto-Detected Batch Size | Notes |
|----------|--------------------------|-------|
| 48 GB+ (A6000, A100) | 32 | Optimal for large-v3-turbo |
| 24 GB (RTX 3090, A5000) | 24 | Optimal |
| 16 GB (RTX 4080) | 16 | Good headroom |
| 12 GB (RTX 3080) | 12 | Can push to 16 with turbo model |
| 8 GB (RTX 3070) | 8 | Test 12 with turbo model |
| 6 GB (entry-level) | 4 | Conservative |

Override via environment variable:

```bash
# In .env
BATCH_SIZE=24
```

**Important:** Transcription VRAM is constant per batch -- it does not scale with audio duration. Longer files simply process more batches sequentially at the same peak VRAM.

### Compute Type Selection

The compute type (quantization) affects speed and quality. OpenTranscribe auto-selects based on GPU compute capability.

| Compute Type | GPU Requirement | Speed | Quality Impact |
|-------------|-----------------|-------|----------------|
| `int8_float16` | Compute >= 7.5 (Turing+) | Fastest | -0.1 WER (negligible) |
| `float16` | Compute >= 7.0 (Volta+) | Fast | Baseline |
| `bfloat16` | Compute >= 8.0 (Ampere+) | Fast | Negligible loss |
| `float32` | Any | Slowest | Baseline |
| `int8` | Compute >= 6.1 | Fastest (CPU) | -0.1 WER |

Auto-detection logic (from `hardware_detection.py`):
- Compute 7.5+ (RTX 20xx, 30xx, 40xx, A-series): `int8_float16`
- Compute 7.0 (V100): `float16`
- Below 7.0: `float32`

Override:

```bash
WHISPER_COMPUTE_TYPE=float16
```

### Beam Size

| Beam Size | Speed | Quality Impact |
|-----------|-------|----------------|
| 1 (greedy) | 2-3x faster | -1-2% WER |
| 2 | 1.5x faster | -0.5% WER |
| 5 (default) | Baseline | Baseline |

For batch processing where speed matters more than perfection, `beam_size=1` gives a significant speedup with minimal quality loss.

```bash
WHISPER_BEAM_SIZE=1
```

### Model Selection Impact

| Model | Speed | VRAM | English | Multilingual | Translation |
|-------|-------|------|---------|--------------|-------------|
| `large-v3-turbo` (default) | 6x faster | ~6 GB | Excellent | Good | **No** |
| `large-v3` | Slow | ~10 GB | Excellent | Best | Yes |
| `large-v2` | Slow | ~10 GB | Excellent | Good | Yes |

Use `large-v3-turbo` unless you need translation to English or maximum non-English accuracy. The turbo model uses ~40% less VRAM and processes 6x faster.

```bash
WHISPER_MODEL=large-v3-turbo
```

### Hybrid Mode {/* #hybrid-mode */}

For systems where the GPU cannot fit the full transcription model, OpenTranscribe auto-activates **hybrid mode**: transcription runs on CPU while diarization stays on the NVIDIA GPU. This requires only ~1.3 GB VRAM.

**Auto-activation thresholds** (minimum batch=2 VRAM peak vs. 80% of GPU VRAM):

| Model | Min Peak VRAM | Auto-hybrid if GPU &lt; |
|-------|--------------|------------------------|
| large-v3-turbo / large-v3 | 3,893 MB | ~4.9 GB |
| medium | 3,829 MB | ~4.8 GB |
| small | 2,933 MB | ~3.7 GB |

macOS (Apple Silicon) is **not** a hybrid-mode platform: Docker Desktop has no Metal/GPU
passthrough into the Linux VM, so a container there has no MPS device to place diarization on.
Use the `--lite` (CPU-only) image; both stages run on CPU. The MPS branch in
`HardwareConfig._detect_optimal_device` gates on `platform.system() == "darwin"`, which is never
true inside the Linux container every install path runs — see
`backend/app/transcription/CLAUDE.md`.

```bash
# Hybrid mode environment variables
WHISPER_HYBRID_MODE=auto          # auto (default) | true | false
WHISPER_HYBRID_CPU_MODEL=small    # CPU transcription model: small | medium | base
```

**Accuracy**: The `small` model (default for hybrid) gives good accuracy at 15–30× real-time on modern CPUs. Use `WHISPER_HYBRID_CPU_MODEL=medium` for better accuracy at ~half the speed.

**Benchmark data**: See `docs/whisper-vram-profile/README.md` for full VRAM sweep results across models and batch sizes.

## GPU Optimization

### VRAM Usage by Pipeline Stage

The transcription pipeline runs in sequential mode -- the transcriber is loaded, used, and released before the diarizer loads. This keeps peak VRAM manageable for consumer GPUs.

| Stage | VRAM Usage | Scales With |
|-------|-----------|-------------|
| Model loading (both models) | ~5.5 GB | Fixed |
| Transcription inference | +300-400 MB | Batch size (not duration) |
| Diarization (PyAnnote) | +1-11 GB | Audio duration |
| Speaker embeddings | +4.5-6.6 GB | Post-pipeline, separate step |
| **Peak (sequential mode)** | **~9 GB typical** | Longest audio file |

### Diarization VRAM Scaling

Diarization is the primary VRAM consumer and scales with audio duration:

| Audio Duration | Diarization VRAM Overhead | Peak Device VRAM |
|---------------|--------------------------|------------------|
| 0.5 hours | +1 GB | ~3 GB |
| 1.0 hours | +11.5 GB | ~19.5 GB |
| 2.2 hours | +9.2 GB | ~11.3 GB |
| 3.2 hours | +0.9 GB | ~3 GB |
| 4.7 hours | +11.1 GB | ~25.8 GB |

VRAM variability is caused by PyTorch's caching allocator timing -- the allocator holds freed memory as "reserved" until `torch.cuda.empty_cache()` is called. Actual peak during processing is often higher than post-stage snapshots.

### Concurrent Processing

For multi-GPU or high-VRAM systems, concurrent processing can multiply throughput:

**Shared model weights** via thread pool mode (`GPU_WORKER_POOL=threads`):
- 5 concurrent tasks share one copy of model weights (~5.5 GB)
- vs. prefork mode: 5 separate copies = ~27.5 GB
- Savings: ~22 GB VRAM for 5 concurrent tasks

```bash
# In .env
GPU_WORKER_POOL=threads
GPU_CONCURRENT_REQUESTS=5  # Number of concurrent transcription tasks
```

**Multi-GPU scaling** for systems with multiple GPUs:

```bash
GPU_SCALE_ENABLED=true
GPU_SCALE_DEVICE_ID=2       # Which GPU to use
GPU_SCALE_WORKERS=4         # Parallel workers
```

See the [Multi-GPU Scaling](../configuration/multi-gpu-scaling) documentation for details.

### Auto-Detection Logic

The `HardwareConfig` class in `backend/app/utils/hardware_detection.py` handles all auto-detection:

1. **Device detection**: CUDA > MPS > CPU — but the MPS arm is unreachable in a container
   (it requires `platform.system() == "darwin"`), so on macOS this resolves to CPU
2. **Compute type**: Based on GPU compute capability (see table above)
3. **Batch size**: Based on total VRAM (see table above)
4. **Environment overrides**: `TORCH_DEVICE`, `COMPUTE_TYPE`, `BATCH_SIZE` override auto-detection

### Warm Model Caching

The `ModelManager` singleton keeps AI models loaded between Celery tasks. This eliminates the ~15 second model loading overhead per file.

| Scenario | Model Loading Overhead (2500 files) |
|----------|-------------------------------------|
| Without cache (load/unload per file) | 2500 x 15s = **10.4 hours** |
| With warm cache (first load only) | 15s + 2500 x 0s = **15 seconds** |

This is enabled by default. The GPU worker uses `--max-tasks-per-child=100000` to keep the process (and its cached models) alive across many tasks.

## PostgreSQL Tuning

OpenTranscribe configures PostgreSQL with tuning parameters optimized for transcription workloads (2500+ files, 1M+ segments on SSD storage). All values are configurable via `.env`.

| Parameter | Default | Purpose | When to Increase |
|-----------|---------|---------|------------------|
| `PG_SHARED_BUFFERS` | 256 MB | Shared memory for caching | Set to 25% of available RAM (e.g., 2 GB for 8 GB RAM) |
| `PG_EFFECTIVE_CACHE_SIZE` | 1 GB | Planner's estimate of OS cache | Set to 50-75% of available RAM |
| `PG_WORK_MEM` | 16 MB | Per-operation sort/hash memory | Increase for complex queries; careful -- multiplied by connections |
| `PG_MAINTENANCE_WORK_MEM` | 128 MB | VACUUM, CREATE INDEX operations | Increase to 256-512 MB for faster index rebuilds |
| `PG_RANDOM_PAGE_COST` | 1.1 | Planner cost estimate for random I/O | Already tuned for SSD; use 4.0 for spinning disks |
| `PG_EFFECTIVE_IO_CONCURRENCY` | 200 | Concurrent I/O operations | Already tuned for SSD; use 2 for spinning disks |
| `PG_MAX_CONNECTIONS` | 200 | Maximum client connections | Increase if connection errors appear |

Additional hardcoded tuning (in `docker-compose.yml`):

| Parameter | Value | Purpose |
|-----------|-------|---------|
| `wal_buffers` | 16 MB | Write-ahead log buffer size |
| `checkpoint_completion_target` | 0.9 | Spread checkpoint I/O over time |
| `default_statistics_target` | 100 | Statistics sampling for query planner |

### Recommended Configurations by System RAM

| System RAM | shared_buffers | effective_cache_size | work_mem | maintenance_work_mem |
|-----------|---------------|---------------------|----------|---------------------|
| 4 GB | 256 MB (default) | 1 GB (default) | 16 MB | 128 MB |
| 8 GB | 2 GB | 6 GB | 32 MB | 256 MB |
| 16 GB | 4 GB | 12 GB | 64 MB | 512 MB |
| 32 GB+ | 8 GB | 24 GB | 128 MB | 1 GB |

Monitor cache hit ratio -- it should be above 95%:

```bash
docker exec opentranscribe-postgres psql -U postgres -d opentranscribe -c \
  "SELECT round(100.0 * sum(blks_hit) / nullif(sum(blks_hit + blks_read), 0), 2) AS cache_hit_ratio
   FROM pg_stat_database WHERE datname = 'opentranscribe';"
```

## OpenSearch Heap: What It Is Actually For

**Heap is sized for the embedding model, not for your data.** This surprises people, and
the numbers below are measured on a real cluster (2026-08-18, `opensearch:3.4.0`) rather
than reasoned from documentation — an earlier version of this guidance was wrong on every
figure.

Measured on a working install: **25 indices, 52,053 documents, 188 MB on disk**, and
Lucene segment memory **0.0 MB** (modern Lucene keeps it off-heap). Your transcripts are
not what consumes heap.

### The default is a locked cost

```yaml
OPENSEARCH_JAVA_OPTS=-Xms4g -Xmx4g   # docker-compose.yml default
bootstrap.memory_lock=true
```

`Xms == Xmx` means the full 4 GB is claimed **at startup**, and `memory_lock` **pins it in
RAM** where it cannot be swapped or reclaimed. On a 16 GB laptop that is 4 GB gone before
Postgres, MinIO, Redis, the backend and the ASR models get anything.

### Verified smaller profiles

Each row was tested end to end — register the model, deploy it, and run a **real
prediction**:

| Heap | What it runs | Verified |
|---|---|---|
| **1 GB** | the default model (`all-MiniLM-L6-v2`, 87.5 MB) | ✅ returns 384-dim vectors |
| **2 GB** | every English model, incl. `all-mpnet-base-v2` (418.7 MB) | ✅ returns 768-dim vectors |
| **4 GB** | default; headroom for indexing bursts and multilingual models | ✅ |

```bash
# in .env — for a laptop or a small VM
OPENSEARCH_JAVA_OPTS=-Xms2g -Xmx2g
```

Keep `Xms` equal to `Xmx`. A differing pair makes the JVM resize the heap at runtime
(pause spikes), and `bootstrap.memory_lock` expects a fixed heap.

### ⚠️ "Deployed" does not mean "working"

`all-mpnet-base-v2` on a 1 GB heap reports `DEPLOY: COMPLETED` and then **fails to produce
an embedding**. Any health check that reads model *state* calls that cluster healthy. If
you shrink the heap, verify with a real prediction:

```bash
python3 scripts/verify-embedding-models.py --url http://localhost:9200
```

### ⚠️ A high heap percentage is usually NOT a problem

Java frees memory when it feels pressure, not when it finishes with it. A cluster sitting
at 83–89% heap with **0 full GCs in 25 hours** and young-generation GCs averaging 12.6 ms
is healthy — that number is mostly uncollected garbage, not live data. See
[Monitoring](./monitoring.md) for how to tell a real breaker trip from a false one before
adding heap that will not help.

## OpenSearch Tuning

### JVM Heap Sizing

The default is **4 GB** (`docker-compose.yml`), and heap is sized for the embedding model
rather than for the number of transcripts — see
[OpenSearch Heap: What It Is Actually For](#opensearch-heap-what-it-is-actually-for) above
for the measured floors.

**Rule of thumb:** Never set heap above 50% of available RAM, and never above ~30 GB (JVM compressed OOPs threshold).

### Refresh Interval

By default, OpenSearch refreshes indices every 1 second. During bulk operations (reindexing, batch imports), increase the refresh interval:

```bash
# Set refresh interval to 30 seconds during bulk operations
curl -X PUT http://localhost:5180/transcripts/_settings \
  -H 'Content-Type: application/json' \
  -d '{"index": {"refresh_interval": "30s"}}'

# Reset to default after bulk operation
curl -X PUT http://localhost:5180/transcripts/_settings \
  -H 'Content-Type: application/json' \
  -d '{"index": {"refresh_interval": "1s"}}'
```

### Neural Search Model

OpenSearch loads a sentence-transformer model for vector/semantic search. The default `all-MiniLM-L6-v2` (384-dim, ~80 MB) runs on CPU within the OpenSearch JVM.

Key ML Commons settings (already configured in `docker-compose.yml`):

| Setting | Value | Purpose |
|---------|-------|---------|
| `plugins.ml_commons.only_run_on_ml_node` | false | Run ML on data nodes (single-node) |
| `plugins.ml_commons.native_memory_threshold` | 99 | Allow high native memory for models |
| `plugins.ml_commons.model_access_control_enabled` | false | Simplified access for single-node |

## Redis Optimization

Redis serves as the Celery broker and result backend.

### Memory Configuration

```bash
# Check current memory usage
docker exec opentranscribe-redis redis-cli info memory

# Key metrics to watch:
# used_memory_human - current usage
# maxmemory_human - configured limit (0 = unlimited)
# evicted_keys - keys removed due to memory pressure
```

### Recommendations

| Setting | Default | Recommendation |
|---------|---------|---------------|
| `maxmemory` | Unlimited | Set to 256 MB - 1 GB depending on workload |
| `maxmemory-policy` | `noeviction` | Use `allkeys-lru` if memory-constrained |
| Persistence | RDB snapshots | Default is fine; disable AOF for performance |
| Password | None | Set `REDIS_PASSWORD` in `.env` for production |

For most deployments, Redis memory usage stays under 100 MB. It primarily stores Celery task metadata and results, not large data.

## Celery Worker Tuning

### Worker Configuration

Each worker type has different concurrency and lifecycle settings:

| Worker | Queue(s) | Concurrency | max-tasks-per-child | Rationale |
|--------|----------|-------------|---------------------|-----------|
| GPU | `gpu` | 1 | 100,000 | Sequential GPU processing; keep models warm |
| Download | `download` | 3 | 10 | I/O-bound; restart frequently to release file handles |
| CPU | `cpu,utility` | 8 | 20 | CPU-bound; restart to prevent memory leaks |
| NLP | `nlp,celery` | 4 | 50 | LLM API calls; moderate concurrency |
| Embedding | `embedding` | 1 | 500 | Single model loaded; keep warm |

### Tuning Concurrency

```bash
# In .env
GPU_CONCURRENT_REQUESTS=1   # GPU worker concurrency (default: 1)
DOWNLOAD_CONCURRENCY=3      # Download worker concurrency
NLP_CONCURRENCY=4           # NLP/LLM worker concurrency
```

**GPU worker concurrency** should stay at 1 for most setups. The sequential pipeline (transcribe then diarize) is designed for single-task processing to manage VRAM. Only increase for high-VRAM GPUs (48 GB+) with thread pool mode.

**NLP worker concurrency** can be increased if your LLM provider handles concurrent requests well. With self-hosted vLLM, match this to `--max-num-seqs` on the LLM server.

### Prefetch Multiplier

By default, Celery prefetches tasks. For GPU tasks (which are long-running), prefetching can cause uneven distribution:

```bash
# Disable prefetching for GPU worker (already recommended for long tasks)
CELERY_WORKER_PREFETCH_MULTIPLIER=1
```

## LLM/vLLM Optimization

If you use a self-hosted vLLM server for summarization and speaker identification, concurrent request handling is the key bottleneck.

### Concurrent Request Tuning

OpenTranscribe can send up to 6 concurrent LLM requests per file (4 summary chunks + topics + speaker ID). If your vLLM server limits concurrent sequences, requests queue and wait.

| vLLM Setting | Before | After (Recommended) |
|-------------|--------|---------------------|
| `--max-num-seqs` | 2 | 6 |
| `--enable-chunked-prefill` | No | Yes |

### Expected Performance Impact

| Metric | max-num-seqs=2 | max-num-seqs=6 |
|--------|---------------|---------------|
| Concurrent requests | 2 | 6 |
| Summary (4 chunks) time | ~40s sequential | ~10s parallel |
| Total LLM time per file | ~60s | ~15-20s |
| Throughput | ~1 file/min | ~3-4 files/min |

### VRAM Budget for vLLM

For a 20B parameter model at FP16:

| State | VRAM Usage |
|-------|-----------|
| Model weights (idle) | ~40 GB |
| 2 concurrent requests | ~42-44 GB |
| 6 concurrent requests | ~44-47 GB |

If OOM errors occur, reduce `--max-num-seqs` or lower `--gpu-memory-utilization`.

### Tuning Checklist

| Observation | Action |
|-------------|--------|
| Memory stays under 45 GB with 6 seqs | Try `--max-num-seqs 8` |
| OOM errors with 6 seqs | Reduce to `--max-num-seqs 4` |
| Slow first-token latency | Add `--enable-prefix-caching` |
| Requests time out | Increase `--swap-space` to 48-64 |

### Alternative: SGLang

If vLLM bottlenecks under load, [SGLang](https://github.com/sgl-project/sglang) offers superior continuous batching with the same OpenAI-compatible API:

```yaml
image: lmsysorg/sglang:latest
command:
  - python -m sglang.launch_server
  - --model-path openai/gpt-oss-20b
  - --host 0.0.0.0
  - --port 8000
  - --mem-fraction-static 0.90
  - --max-running-requests 8
```

## Network & I/O

### MinIO Performance

MinIO stores all uploaded media files. For large batch imports:

- Use SSD storage for the MinIO data volume
- Ensure the Docker volume is on a fast filesystem (ext4 or XFS on SSD)
- For NAS/NFS storage: mount with `noatime,async` for better write performance
- MinIO's default settings are sufficient for most deployments

### Model Cache Location

AI models (~2.5 GB total) are cached on disk and loaded to GPU/RAM on first use. Place the cache on fast storage:

```bash
# In .env
MODEL_CACHE_DIR=/path/to/fast/ssd/models
```

For NFS/NAS model storage: the initial model load will be slower (~30s vs ~5s on local SSD), but subsequent loads use warm caching in memory. This only affects cold starts and worker restarts.

## Benchmark Reference

All benchmarks use a 3.3-hour podcast (11,893s) on an NVIDIA RTX A6000 (49 GB).

### Pipeline Performance

| Configuration | Total Time | Transcription | Diarization | Speaker Assignment |
|--------------|-----------|---------------|-------------|-------------------|
| Legacy WhisperX + alignment | 706s | 75s | 194s | 10.2s |
| WhisperX batched, no alignment | 304s | 76s | 198s | 0.04s |
| Native pipeline (batch_size=32, beam=5) | **332s** | **105s** | **192s** | **0.5s** |

### Quality Comparison

| Metric | WhisperX (no alignment) | Native Pipeline |
|--------|------------------------|-----------------|
| Text word overlap | 92.5% | ~92% |
| Speaker consistency | 76.7% (segment-level) | **95.2%** (word-level) |
| Timestamp accuracy | 0.00s MAE | 0.00s MAE |

The native pipeline achieves 95% speaker consistency because word-level timestamps enable precise speaker-to-word matching via an interval tree algorithm (273x faster than WhisperX's linear scan).

### Scaling Projections (2500 Three-Hour Files)

| Configuration | Per File | 2500 Files | With 4x GPU Workers |
|--------------|---------|-----------|---------------------|
| Legacy WhisperX + alignment | 706s | 490 hours | 123 hours |
| **Native pipeline** | **332s** | **231 hours** | **58 hours** |
| + warm model caching | ~320s | 222 hours | 56 hours |
| + 4x GPU workers on A6000 | ~80s effective | **56 hours** | N/A |

### Solo Processing Times by Duration (A6000)

| Audio Duration | Transcription | Diarization | Total |
|---------------|---------------|-------------|-------|
| 0.5 hours | 18.7s | 31.4s | 54.6s |
| 1.0 hours | 38.8s | 62.5s | 103.2s |
| 2.2 hours | 72.8s | 131.4s | 217.2s |
| 3.2 hours | 112.9s | 202.5s | 326.4s |
| 4.7 hours | 183.7s | 441.8s | 707.4s |

## Scaling Decision Matrix

| Symptom | Root Cause | Solution | Cost |
|---------|-----------|----------|------|
| `gpu` queue backing up, GPU utilization 100% | GPU is the bottleneck | Add a second GPU worker on another GPU | Hardware cost |
| `gpu` queue backing up, GPU utilization under 50% | I/O or CPU bottleneck | Move model cache to SSD, increase CPU workers | Low |
| Long diarization times on large files | VRAM pressure from PyAnnote | Upgrade to higher-VRAM GPU | Hardware cost |
| LLM tasks slow | vLLM concurrent request limit | Increase `--max-num-seqs` | Free (config change) |
| LLM tasks slow, vLLM GPU at 100% | LLM GPU is the bottleneck | Add dedicated LLM GPU or use cloud API | Hardware/API cost |
| Database queries slow | PostgreSQL needs tuning | Increase `shared_buffers`, `work_mem` | Free (config change) |
| Search queries slow | OpenSearch heap too small | Increase `OPENSEARCH_JAVA_OPTS` heap | RAM |
| Many concurrent users, API slow | Backend needs scaling | Run multiple backend replicas behind load balancer | CPU/RAM |
| Download queue backing up | Network bandwidth | Increase `DOWNLOAD_CONCURRENCY`, check bandwidth | Free/network |

### When to Scale Vertically vs. Horizontally

**Scale vertically (upgrade hardware):**
- Moving from 8 GB to 24 GB GPU eliminates VRAM constraints for most files
- More system RAM improves PostgreSQL and OpenSearch caching
- NVMe SSD dramatically improves model loading and media I/O

**Scale horizontally (add workers):**
- Multiple GPU workers on separate GPUs for parallel transcription
- Additional CPU workers for utility tasks
- Separate machines for LLM inference (vLLM/Ollama on dedicated GPU)

**Use cloud APIs instead of self-hosting:**
- When GPU hardware cost exceeds API usage cost
- For occasional/bursty workloads that do not justify dedicated hardware
- Configure `LLM_PROVIDER=openai` or `LLM_PROVIDER=anthropic` in `.env`
