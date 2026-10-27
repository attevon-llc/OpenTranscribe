---
sidebar_position: 2
---

# Hardware Requirements

OpenTranscribe is designed to run on a wide range of hardware configurations, from minimal setups to high-performance systems. This guide outlines the requirements and recommendations for different use cases.

:::tip[Full image or lite image?]
What you need depends on which variant you install. The **full** image transcribes and detects speakers on this server (a GPU makes it fast; it also runs on CPU). The **lite** image never transcribes or detects speakers locally: both are done by cloud providers you configure, so it needs no GPU but does need a cloud provider account and an internet connection. See [Deployment Variants](./deployment-variants.md) to choose.
:::

## Minimum Requirements

### Basic CPU-Only Setup

For small-scale testing or low-volume transcription work with the full image running on CPU (local Whisper, no cloud account needed):

- **CPU**: Modern x86-64 processor (4+ cores)
- **RAM**: 8GB minimum
- **Storage**: 20GB free space (10GB for Docker images, 10GB for models and data)
- **Operating System**: Linux, macOS, or Windows with WSL2
- **Docker**: Docker Engine 20.10+ and Docker Compose 2.0+

**Performance**:
- Transcription speed: ~0.5-1x realtime (slower than playback)
- Suitable for: Testing, development, occasional use
- Speaker detection on this server also runs on the CPU when a user's speaker-detection setting asks for it; choose Off in Settings to skip it

If you would rather send audio to a cloud provider than transcribe on CPU, the lite image needs no GPU either; see [Deployment Variants](./deployment-variants.md).

:::warning[CPU-Only Limitations]
CPU-only mode provides significantly slower transcription speeds (0.5-1x realtime) compared to GPU acceleration (~40x realtime with large-v3-turbo, full pipeline). For production use or regular transcription work, GPU acceleration is strongly recommended.
:::

## Recommended Configuration

### GPU-Accelerated Setup

For regular use and production deployments:

- **CPU**: Modern multi-core processor (8+ cores recommended)
- **RAM**: 16GB minimum, 32GB recommended
- **GPU**: NVIDIA GPU with 8GB+ VRAM
  - RTX 3060 (12GB) - Good entry-level option
  - RTX 3080/3090 (10-24GB) - Excellent performance
  - RTX 4070/4080/4090 (12-24GB) - Best consumer GPUs
  - Tesla T4 (16GB) - Good for servers/cloud
  - A4000/A5000/A6000 (16-48GB) - Professional workstations
- **CUDA**: an NVIDIA driver recent enough for CUDA 12.x (the image bundles CUDA 12.8 runtime libraries; no host CUDA toolkit is needed). See [GPU Setup](./gpu-setup.md)
- **Storage**: 50GB+ free space
  - 15GB for Docker images
  - 3GB for AI models (cached locally)
  - 32GB+ for media files and database
- **Network**: 100Mbps+ for YouTube downloads

**Performance**:
- Transcription speed: ~40x realtime with large-v3-turbo (full pipeline)
- Example: 1-hour video processes in roughly 1.5 minutes
- Suitable for: Production use, high-volume transcription

:::tip[GPU Memory Requirements]
- **Transcription only**: 4-6GB VRAM
- **Transcription + Diarization**: 6-8GB VRAM
- **Multiple concurrent jobs**: 10-12GB+ VRAM
- **Hybrid mode (low-VRAM NVIDIA GPUs)**: ~1.3GB VRAM for diarization only — transcription runs on CPU
:::

:::info[Hybrid Mode for Low-VRAM GPUs — and why macOS is not one]
If the configured Whisper model cannot fit on your GPU even at the minimum batch size (its peak would exceed 80% of total VRAM), OpenTranscribe automatically activates **hybrid mode** (override with `WHISPER_HYBRID_MODE=true|false|auto`): transcription runs on CPU (`small` model by default, int8) while speaker detection stays on GPU (~1.3 GB VRAM measured for the in-process PyAnnote engine), and still produces speaker-separated transcripts.

On **macOS (Apple Silicon)**, Docker Desktop has no Metal/GPU passthrough, so
every container is CPU-only there regardless of host chip, and there is no MPS
acceleration path inside a container. The image built for Apple Silicon is the
`--lite` image, which transcribes and detects speakers with cloud providers only
(no local Whisper, no local speaker detection); see
[Deployment Variants](./deployment-variants.md).

See [Performance Tuning → Hybrid Mode](../operations/performance-tuning.md#hybrid-mode) for configuration details.
:::

## High-Performance Configuration

### Multi-GPU Setup

For maximum throughput and parallel processing:

- **CPU**: High-end multi-core processor (16+ cores)
- **RAM**: 64GB+ for optimal performance
- **GPUs**: Multiple NVIDIA GPUs (2-4 recommended)
  - Dedicated GPU for transcription workers
  - Separate GPU for LLM inference (if using local LLM)
- **Storage**: NVMe SSD with 200GB+ free space
- **Network**: 1Gbps+ for bulk operations

**Multi-GPU Configuration Example**:
```bash
# Example: 3 GPU setup
GPU 0: RTX A6000 (49GB) - Local LLM inference
GPU 1: RTX 3080 Ti (12GB) - Single transcription worker (default)
GPU 2: RTX A6000 (49GB) - 4 parallel transcription workers (scaled)
```

See [Multi-GPU Scaling](../configuration/multi-gpu-scaling.md) for configuration details.

**Performance**:
- With GPU scaling: Process 4+ videos simultaneously
- Transcription speed: ~40x realtime per worker (full pipeline)
- Total throughput: about 51x realtime with 4 workers (measured, versus about 39x with one; scaling is sub-linear)

## Cloud & Server Deployments

### AWS / Cloud Provider Recommendations

| Use Case | Instance Type | GPU | vCPU | RAM | Estimated Cost |
|----------|--------------|-----|------|-----|----------------|
| Light Use | g4dn.xlarge | T4 (16GB) | 4 | 16GB | ~$0.52/hour |
| Standard | g4dn.2xlarge | T4 (16GB) | 8 | 32GB | ~$0.75/hour |
| High Performance | p3.2xlarge | V100 (16GB) | 8 | 61GB | ~$3.06/hour |
| Multi-GPU | p3.8xlarge | 4x V100 | 32 | 244GB | ~$12.24/hour |

:::tip[Cost Optimization]
- Use spot instances for non-critical workloads (50-70% cost savings)
- Stop instances when not in use
- Consider reserved instances for 24/7 deployments (40-60% savings)
:::

### Self-Hosted Server

For on-premise deployments:

- **Form Factor**: Rackmount server or workstation
- **CPU**: Xeon or EPYC processor (12-32 cores)
- **RAM**: 64-128GB ECC memory
- **GPU**: Professional GPUs (A4000, A5000, A6000 series)
- **Storage**:
  - 500GB-2TB NVMe SSD for OS and Docker
  - 4-16TB HDD/SSD for media storage
  - RAID configuration for redundancy
- **Network**: 10GbE for NAS integration
- **Power**: Redundant PSU recommended
- **Cooling**: Adequate cooling for GPU workloads

## Storage Considerations

### Model Cache

AI models are cached locally to avoid re-downloading:

| Model Category | Size | Purpose |
|----------------|------|---------|
| WhisperX Models | ~1.5GB | Speech recognition |
| PyAnnote Models | ~500MB | Speaker detection and voiceprints on this server |
| Sentence Transformers | ~80MB | Semantic search |
| NLTK Data | ~13MB | Text processing |
| **Total** | **~2.1GB** | All AI models |

**Configuration**: Set `MODEL_CACHE_DIR` in `.env` (default: `./models`)

### Media Storage

Plan storage based on your use case:

| Content Type | Typical Size | 100 Files | 1000 Files |
|--------------|-------------|-----------|------------|
| Audio (podcast) | 50-100MB | 5-10GB | 50-100GB |
| Video (meeting) | 200-500MB | 20-50GB | 200-500GB |
| Video (1080p) | 1-2GB | 100-200GB | 1-2TB |
| Video (4K) | 4-8GB | 400-800GB | 4-8TB |

Add 10-20% overhead for:
- Database storage (transcripts, metadata)
- OpenSearch indices
- Generated waveforms and thumbnails

## Network Requirements

### Bandwidth

- **Minimum**: 10Mbps for basic operation
- **Recommended**: 100Mbps+ for YouTube downloads
- **Optimal**: 1Gbps for bulk operations and NAS integration

### Ports

The following ports must be available:

| Service | Default Port | Purpose |
|---------|-------------|---------|
| Frontend | 5173 | Web interface |
| Backend API | 5174 | API endpoints |
| Flower | 5175 | Task monitoring |
| PostgreSQL | 5176 | Database (loopback only) |
| Redis | 5177 | Task queue (loopback only) |
| MinIO | 5178 (API), 5179 (console) | Object storage (loopback only) |
| OpenSearch | 5180 (API), 5181 (admin) | Search engine (loopback only) |
| Docs | 5183 | Embedded documentation site |

Each is set by a `*_PORT` variable in `.env` (for example `FRONTEND_PORT`, `POSTGRES_PORT`).

## Operating System Support

### Officially Supported

- **Linux**: Ubuntu 20.04+, Debian 11+, RHEL 8+, CentOS Stream 9+
- **macOS**: macOS 12+ (CPU-only, no NVIDIA GPU support)
- **Windows**: Windows 10/11 with WSL2 (GPU support via WSL2 CUDA)

### GPU Support by Platform

| Platform | NVIDIA GPU | Notes |
|----------|------------|-------|
| Linux | ✅ Full support | Best performance |
| macOS | N/A — no Docker Metal passthrough | The `--lite` image: cloud transcription and speaker detection, no local Whisper, no MPS acceleration in a container |
| Windows | ✅ via WSL2 | CUDA in WSL2 required |

## Verification Commands

Check your system meets requirements:

```bash
# Check Docker version
docker --version
docker compose version

# Check NVIDIA GPU
nvidia-smi

# Check available memory
free -h

# Check disk space
df -h

# Check CPU cores
nproc
```

## Performance Benchmarks

Real-world performance examples:

Only the high-end card below was benchmarked (see `docs/BENCHMARK_RESULTS.md`: 37-40x realtime single-file, full pipeline, `large-v3-turbo`). The other figures are estimates and will vary with model, file length and settings.

### Entry-Level GPU (RTX 3060 12GB)

- Concurrent jobs: 1-2 recommended

### Mid-Range GPU (RTX 3080 12GB)

- Concurrent jobs: 2-3 with GPU scaling

### High-End GPU (RTX A6000 48GB)

- 1-hour audio: roughly 1.5 minutes
- 3-hour meeting: roughly 4.5 minutes
- Concurrent jobs: 4-6 with GPU scaling
- Can handle local LLM + transcription simultaneously

## Upgrade Paths

### CPU → GPU

Simplest upgrade for dramatic performance improvement:

1. Add NVIDIA GPU (8GB+ VRAM)
2. Install the NVIDIA driver and the NVIDIA Container Toolkit (see [GPU Setup](./gpu-setup.md))
3. Update `.env` with `GPU_DEVICE_ID`
4. Restart OpenTranscribe

**Performance gain**: roughly 40x or more faster transcription

### Single GPU → Multi-GPU

For scaling throughput:

1. Add second/third GPU to system
2. Configure [Multi-GPU Scaling](../configuration/multi-gpu-scaling.md)
3. Update `.env` with GPU scaling settings
4. Restart with `--gpu-scale` flag

**Performance gain**: higher total throughput (about 1.3x with 4 parallel workers in the measured run; see [Multi-GPU Scaling](../configuration/multi-gpu-scaling.md#performance))

### Local Storage → NAS

For enterprise deployments:

1. Set up NAS with 10GbE connection
2. Mount NAS storage on Docker host
3. Configure the NAS overlay (`docker-compose.nas.yml`, auto-loaded from `.env` —
   see [Fresh Deployments](../operations/fresh-deployments.md)) and set `MODEL_CACHE_DIR`
4. Migrate existing data

**Benefits**: Centralized storage, easier backups, unlimited expansion

## NVIDIA Blackwell GPUs

NVIDIA Blackwell GPUs (GB10x/GB20x series, e.g., DGX Spark) require a separate build due to updated CUDA libraries and driver requirements. See the `docs/BLACKWELL_SETUP.md` file in the repository for configuration details.

## Next Steps

- [Deployment Variants](./deployment-variants.md) - Choose the full or lite image
- [Docker Compose Installation](./docker-compose.md) - Install OpenTranscribe
- [GPU Setup](./gpu-setup.md) - Configure NVIDIA GPU support
- [Environment Variables](../configuration/environment-variables.md) - Optimize configuration
- [Multi-GPU Scaling](../configuration/multi-gpu-scaling.md) - Scale transcription throughput
