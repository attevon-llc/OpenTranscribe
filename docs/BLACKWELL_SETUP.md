# Blackwell / DGX Spark GPU Setup

This guide covers running OpenTranscribe on NVIDIA Blackwell architecture GPUs, specifically the **DGX Spark** with the **GB10** processor (compute capability 12.1).

## Overview

The NVIDIA DGX Spark uses the Blackwell GB10 GPU with SM_121 compute architecture. This requires a specialized container image because:

1. **NVRTC does not recognize SM_121** -- PyTorch's NVRTC JIT compiler and CTranslate2 (used by faster-whisper for transcription) crash when encountering the unknown `compute_121` architecture. The Blackwell Dockerfile patches `torch.cuda.get_device_capability()` to report SM_90 (Hopper), which is binary-compatible via PTX fallback.

2. **Unified memory breaks nvidia-smi** -- DGX Spark uses unified CPU+GPU memory, so `nvidia-smi` reports memory stats as `[N/A]`. The GPU stats collector falls back to `torch.cuda.mem_get_info()` for memory reporting.

3. **ARM64 architecture** -- DGX Spark is ARM64-based, requiring the NVIDIA PyTorch container (`nvcr.io/nvidia/pytorch:25.01-py3`) instead of the standard x86_64 Python base image.

## Prerequisites

- NVIDIA DGX Spark or other Blackwell-architecture GPU system
- Docker with NVIDIA Container Toolkit installed
- `nvidia-smi` available on the host

## Quick Start

The Blackwell overlay is **automatically detected** by `opentr.sh` when a Blackwell GPU (compute capability 12.x) is present:

```bash
# This auto-detects Blackwell and applies the correct overlay
./opentr.sh start dev
```

You can verify detection by checking the startup output for:
```
Blackwell GPU overlay enabled (SM_12x detected)
```

## Manual Setup

If you need to run the Blackwell overlay manually:

```bash
# Build the Blackwell-specific backend image
docker build -t opentranscribe-backend-blackwell:latest \
    -f backend/Dockerfile.blackwell backend/

# Start with the Blackwell overlay
docker compose -f docker-compose.yml \
    -f docker-compose.prod.yml \
    -f docker-compose.blackwell.yml up -d
```

## Architecture Details

### Files

| File | Purpose |
|------|---------|
| `backend/Dockerfile.blackwell` | Specialized ARM64/Blackwell Docker image |
| `docker-compose.blackwell.yml` | Compose overlay replacing GPU worker with Blackwell build |

### SM_121 Compatibility Patches

The Blackwell Dockerfile applies three compatibility patches:

1. **torch.cuda.get_device_capability() spoof** -- Returns `(9, 0)` instead of `(12, 1)` so NVRTC-based JIT compilers (CTranslate2, Triton) don't crash on the unknown compute capability.

2. **torchaudio fbank fix** -- Replaces jiterator `.abs()` call (which fails on Blackwell) with `torch.abs()` in the Kaldi compliance module.

3. **pyannote version check bypass** -- The NVIDIA container bundles torch with dev version strings (e.g., `2.6.0a0`) that fail pyannote's version comparison. This patch disables the check.

### Environment Variables

| Variable | Value | Purpose |
|----------|-------|---------|
| `CUDA_FORCE_PTX_JIT` | `1` | Forces PTX JIT compilation for forward compatibility |
| `TORCH_CUDA_ARCH_LIST` | `9.0` | Targets SM_90 for compiled CUDA extensions |

### Unified Memory Support

The DGX Spark uses unified CPU+GPU memory, which means `nvidia-smi` cannot report traditional framebuffer memory statistics. The GPU stats system handles this gracefully:

1. First attempts to read memory from `nvidia-smi` (works on standard GPUs)
2. If nvidia-smi returns `[N/A]`, falls back to `torch.cuda.mem_get_info()`
3. Reports which source was used via the `memory_source` field

### Cache Path Differences

The NVIDIA base image uses `/home/user/` (UID 1000) instead of `/home/appuser/` (UID 1000) used by the standard Dockerfile.prod. Both map to the same UID, but the volume mount paths differ:

| Standard Image | Blackwell Image |
|---------------|-----------------|
| `/home/appuser/.cache/huggingface` | `/home/user/.cache/huggingface` |
| `/home/appuser/.cache/torch` | `/home/user/.cache/torch` |

The `docker-compose.blackwell.yml` overlay handles this mapping automatically.

## Configuration

### GPU Device Selection

Set the GPU device in your `.env` file:

```bash
GPU_DEVICE_ID=0    # Which GPU to use (default: 0)
```

### Worker Concurrency

The Blackwell overlay defaults to `--concurrency=1` for the GPU worker. This is appropriate for single-GPU DGX Spark systems. The worker pool type is inherited from the base configuration (default: `threads`).

The `threads` pool type is important for the v0.4.0 native pipeline: the warm model singleton (`ModelManager`) is loaded once at worker startup and shared across concurrent tasks. This is safe because the GPU worker serializes task execution through Celery's concurrency limit.

### Native Pipeline Compatibility

v0.4.0 uses the native transcription pipeline (faster-whisper `BatchedInferencePipeline` + PyAnnote v4 direct), replacing the legacy WhisperX path. The Blackwell patches (capability spoof, torchaudio fbank fix, pyannote version check bypass) apply equally to the native pipeline. No additional Blackwell-specific configuration is required for v0.4.0.

## Troubleshooting

### Container fails to start with CUDA errors

Verify that `nvidia-smi` works on the host:
```bash
nvidia-smi --query-gpu=compute_cap --format=csv,noheader
```
Expected output for DGX Spark: `12.1`

### Memory stats show "N/A"

This is expected on unified memory systems. The application falls back to `torch.cuda.mem_get_info()` automatically. If you see "N/A" in the UI, verify that PyTorch is correctly installed in the container:

```bash
docker compose exec celery-worker python -c "import torch; print(torch.cuda.mem_get_info(0))"
```

### Transcription crashes with NVRTC errors

Ensure the Blackwell overlay is being loaded (check for "Blackwell GPU overlay enabled" in startup logs). If using manual compose commands, make sure `docker-compose.blackwell.yml` is included.

## Credits

Blackwell/DGX Spark support was contributed by Alex Baur ([@snmabaur](https://github.com/snmabaur)) from the Swiss National Museum ([PR #154](https://github.com/attevon-llc/OpenTranscribe/pull/154)).
