---
sidebar_position: 2
---

# Multi-GPU Worker Scaling

For systems with multiple GPUs, OpenTranscribe supports parallel GPU workers to dramatically increase transcription throughput.

## Overview

**Standard Setup**: 1 GPU = 1 worker = 70x realtime (1-hour file in ~50 seconds)

**Scaled Setup**: 1 GPU = 4 workers = 280x realtime (4 files simultaneously)

## When to Use

Multi-GPU scaling is ideal for:
- Batch processing large numbers of files
- High-throughput production systems
- Systems with dedicated transcription GPU
- Workflows with concurrent uploads

## `--gpu-scale` vs. `--with-gpu-split`

These are two distinct multi-GPU features with different goals, enabled independently:

| | `--gpu-scale` (this page) | `--with-gpu-split` |
|---|---|---|
| What it does | Runs **N parallel Celery workers in one container** against a dedicated GPU, for higher **throughput** (more files transcribed concurrently) | Runs transcription and diarization on **separate** GPUs for higher **per-file** performance |
| Enabled by | The `--gpu-scale` CLI flag on `./opentr.sh start dev` / `start prod` -- **not** `GPU_SCALE_ENABLED` (see Step 1 below) | The `--with-gpu-split` CLI flag **and** `ENGINE_GPU_SPLIT=true` |
| Compose overlay | `docker-compose.gpu-scale.yml` (`COMPOSE_PROFILES=gpu-scale`) | `docker-compose.gpu-split.yml` (`gpu-split` profile) |
| Tuning | `GPU_SCALE_WORKERS`, `GPU_SCALE_DEVICE_ID`, `GPU_SCALE_DEFAULT_WORKER` | `GPU_TRANSCRIBE_DEVICE_ID`, `GPU_DIARIZE_DEVICE_ID` |

This page covers `--gpu-scale` only. For `--with-gpu-split`, see
[Deployment Configuration](../operations/deployment-configuration.md#split-gpu-transcription-on-one-card-diarization-on-another).
The two flags combine on a 3+ GPU host if you want both behaviors at once.

## Hardware Example

```
GPU 0: NVIDIA RTX A6000 (49GB) - Local LLM (vLLM/Ollama)
GPU 1: RTX 3080 Ti (12GB) - Default worker (disabled when scaling)
GPU 2: NVIDIA RTX A6000 (49GB) - 4 parallel workers (scaled)
```

## Configuration

### Step 1: Configure Environment

:::warning[`GPU_SCALE_ENABLED` does not enable scaling]
Scaling is turned on **only** by the `--gpu-scale` CLI flag in Step 2 -- no compose file or
startup script reads `GPU_SCALE_ENABLED`. It's consulted in exactly one unrelated place
(`tasks/utility.py`), to pick which GPU device IDs the system-stats task queries, so a stale
value misreports which GPU is in use without changing any scheduling. Don't set it expecting it
to turn scaling on or off.
:::

Edit `.env` to configure which GPU the scaled workers use and how many run:

```bash
# Which GPU to use for scaled workers
GPU_SCALE_DEVICE_ID=2

# Number of parallel workers
GPU_SCALE_WORKERS=4

# Keep the default single-GPU worker running alongside the scaled workers (1) or disable it (0)
GPU_SCALE_DEFAULT_WORKER=1
```

### Step 2: Start with Scaling

```bash
# Development
./opentr.sh start dev --gpu-scale

# Production
./opentr.sh start prod --gpu-scale

# Reset with scaling
./opentr.sh reset dev --gpu-scale
```

## Performance

| Workers | Throughput | Example (4x 1-hour files) |
|---------|------------|---------------------------|
| 1 worker | 70x realtime | ~3 minutes (sequential) |
| 4 workers | 280x realtime | ~50 seconds (parallel) |

## VRAM Requirements

| Workers | Recommended VRAM | Supported Models |
|---------|------------------|------------------|
| 2 | 12GB+ | large-v2 |
| 4 | 24GB+ | large-v2 |
| 6 | 48GB+ | large-v2 |

## Monitoring

```bash
# Watch GPU usage
watch -n 1 nvidia-smi

# View scaled worker logs
docker compose logs -f celery-worker-gpu-scaled

# Monitor task queue
# Open: http://localhost:5175/flower
```

## Troubleshooting

### Out of Memory Errors

Reduce worker count:
```bash
GPU_SCALE_WORKERS=2  # instead of 4
```

### Poor GPU Utilization

Increase worker count (if VRAM available):
```bash
GPU_SCALE_WORKERS=6  # instead of 4
```

## Next Steps

- [GPU Setup](../installation/gpu-setup.md)
- [Hardware Requirements](../installation/hardware-requirements.md)
- [Environment Variables](./environment-variables.md)
- [Deployment Configuration](../operations/deployment-configuration.md) -- GPU split mode and all other deployment types
