# OpenTranscribe Cross-Platform Compatibility

## Overview

OpenTranscribe now supports **complete cross-platform compatibility** with automatic hardware detection and optimization for:

- **NVIDIA GPUs** with CUDA (Linux/Windows WSL)
- **Apple Silicon** with MPS acceleration (macOS M1/M2)
- **CPU-only processing** (all platforms)

The system automatically detects available hardware and optimizes configuration for maximum performance.

## Quick Start

### 1. Download and Run Setup Script

```bash
# Get the setup script
curl -fsSL https://raw.githubusercontent.com/davidamacey/OpenTranscribe/feat/cross-platform-compatibility/setup-opentranscribe.sh -o setup-opentranscribe.sh

# Make it executable and run
chmod +x setup-opentranscribe.sh
./setup-opentranscribe.sh
```

The setup script will:
- Detect your hardware (CUDA/MPS/CPU)
- Test NVIDIA Container Toolkit (if GPU detected)
- Configure optimal precision and batch sizes
- Create production docker-compose.yml
- Generate secure .env configuration
- Create management scripts

### 2. Start OpenTranscribe (Manual)

```bash
cd opentranscribe
./opentranscribe.sh start
```

This approach gives you control over when Docker images are downloaded and containers are started.

## Hardware Support

### NVIDIA GPU (CUDA)

**Requirements:**
- NVIDIA GPU with CUDA Compute Capability 6.0+
- NVIDIA Driver 470+ (Driver 550+ recommended for Blackwell / RTX 50-series)
- NVIDIA Container Toolkit

**Automatic Configuration:**
- Device: `cuda`
- Precision: `float16` (if supported) or `float32`
- Batch Size: `16` (high-end) to `4` (entry-level)
- Model: `large-v3-turbo` (default in v0.4.0)

**Performance (v0.4.0 benchmarks):**
- RTX A6000 (49GB): ~40.3x realtime single-file, 54.6x peak at concurrency=8
- RTX 3080 Ti (12GB): ~25-30x realtime single-file (constrained by VRAM at high concurrency)

**Blackwell GPU Support (RTX 50-series):**
- Supported via CUDA 12.8+ and Driver 570+
- See `docs/BLACKWELL_SETUP.md` for Blackwell-specific configuration
- `GPU_WORKER_POOL=threads` (default in v0.4.0) is required for Blackwell — prefork mode is not supported due to CUDA context forking limitations

### Apple Silicon (MPS)

**Requirements:**
- macOS 12.3+ with Apple Silicon (M1/M2/M3/M4)
- At least 8GB unified memory

**Automatic Configuration:**
- Device: `mps`
- Precision: `float32`
- Batch Size: `8` (M2 Max/Ultra) to `4` (M1)
- Model: `large-v3-turbo` (recommended; v0.4.0 default)

**Performance:** ~3x faster than real-time (M2 Max benchmark; see `docs/GPU_OPTIMIZATION_RESULTS.md` for MPS details)

**MPS Optimization (v0.4.0):** PyAnnote GPU optimizations include native MPS FFT support, yielding a 1.17x overall diarization speedup and 1.21x embedding speedup on Apple Silicon. See `docs/PYANNOTE_OPTIMIZATION_SUMMARY.md`.

### CPU Processing

**Requirements:**
- Multi-core CPU (4+ cores recommended)
- 8GB+ RAM

**Automatic Configuration:**
- Device: `cpu`
- Precision: `int8` (optimized for CPU)
- Batch Size: `1-4` (based on CPU cores)
- Model: `base` (recommended for speed)

**Performance:** ~1.5x real-time (slower than real-time for large models; `base` model processes at ~4x real-time on 16-core CPU)

**CPU-only without GPU:** Consider `DEPLOYMENT_MODE=lite` to skip the GPU worker entirely and use a cloud ASR provider. This avoids downloading 6GB+ of Whisper/PyAnnote model weights on CPU-only machines. See "Deployment Modes" section below.

## Deployment Modes (v0.4.0)

### Full Mode (Default)

```bash
DEPLOYMENT_MODE=full   # default
```

All services started including GPU worker, PyAnnote diarization, and local Whisper models.
Requires CUDA-capable GPU for production performance.

### API-Lite Mode

```bash
DEPLOYMENT_MODE=lite
ASR_PROVIDER=deepgram   # or assemblyai, openai, etc.
```

GPU worker is not started. All transcription is routed through a cloud ASR provider.
Best for CPU-only deployments or when GPU hardware is unavailable.

| | Full | Lite |
|-|------|------|
| GPU worker | Yes | No |
| Local Whisper models | Downloaded | Not downloaded |
| Cloud ASR | Optional | Required |
| Speaker diarization | PyAnnote (local) | Provider-dependent |
| Docker image size | ~12GB | ~3GB |
| Minimum RAM | 8GB + 6GB VRAM | 4GB RAM only |

## Key Features

### Automatic Hardware Detection

The system automatically detects and configures:
- Available GPUs (NVIDIA CUDA, Apple MPS)
- Optimal precision (float16/float32/int8)
- Memory-appropriate batch sizes
- Docker runtime configuration

### ✅ Production Docker Images

- Pre-built images from Docker Hub
- Single backend image supports all platforms
- Single frontend image for all deployments
- Automatic hardware detection at runtime

### ✅ Smart Configuration

- Environment variables auto-detected
- Override capability for advanced users
- Performance optimization per platform
- Memory management and cleanup

### ✅ Comprehensive Scripts

- `setup-opentranscribe.sh` - Complete installation
- `opentranscribe.sh` - Daily management
- Hardware validation and health checks
- Cross-platform compatibility

## Architecture (v0.4.0)

```
┌─────────────────────────────────────────────────────────────────┐
│                       OpenTranscribe                            │
├─────────────────────────────────────────────────────────────────┤
│  Frontend (Svelte/TypeScript) - Universal for all platforms     │
├─────────────────────────────────────────────────────────────────┤
│  Backend (FastAPI) - Unified cross-platform container           │
│  └─ Hardware Detection Layer                                    │
├─────────────────────────────────────────────────────────────────┤
│  Celery Workers                                                 │
│  ├─ GPU Worker (threads pool) - transcription, diarization      │
│  │   ├─ faster-whisper (large-v3-turbo default)                │
│  │   └─ PyAnnote v4 diarization (CUDA/MPS/CPU)                │
│  ├─ CPU Worker - waveform, base/tiny model routing              │
│  ├─ Download Worker - yt-dlp, Deno JS runtime                   │
│  ├─ NLP Worker - LLM summarization, speaker ID                  │
│  ├─ Embedding Worker - speaker embeddings (NEW in v0.4.0)       │
│  └─ Utility Worker - maintenance, cleanup, retention            │
│                                                                 │
│  Note: In DEPLOYMENT_MODE=lite, GPU Worker is not started.     │
├─────────────────────────────────────────────────────────────────┤
│  Infrastructure Services                                        │
│  ├─ PostgreSQL (Database)                                       │
│  ├─ Redis (Task Queue + Cache)                                  │
│  ├─ MinIO (Object Storage)                                      │
│  ├─ OpenSearch 3.4.0 (Full-text + Neural Search, ML Commons)   │
│  ├─ Flower (Celery monitoring)                                  │
│  ├─ Docs (embedded documentation, proxied at /docs/)           │
│  └─ NGINX (optional reverse proxy, required for PKI mTLS)      │
└─────────────────────────────────────────────────────────────────┘
```

## Configuration Options

### Environment Variables

| Variable | Values | Default | Description |
|----------|--------|---------|-------------|
| `TORCH_DEVICE` | `auto`, `cuda`, `mps`, `cpu` | `auto` | Force specific device |
| `COMPUTE_TYPE` | `auto`, `float16`, `float32`, `int8` | `auto` | Force precision |
| `BATCH_SIZE` | `auto`, `1-32` | `auto` | Processing batch size |
| `WHISPER_MODEL` | `tiny`, `base`, `small`, `medium`, `large-v2`, `large-v3`, `large-v3-turbo` | `large-v3-turbo` | AI model size |
| `GPU_DEVICE_ID` | `0`, `1`, `2`, etc. | `0` | GPU selection |
| `GPU_WORKER_POOL` | `threads`, `prefork` | `threads` | Celery worker pool (threads is default in v0.4.0; required for Blackwell) |
| `DEPLOYMENT_MODE` | `full`, `lite` | `full` | `lite` disables GPU worker, uses cloud ASR only |

**Deprecated in v0.4.0 (silently ignored):**
- `ENABLE_ALIGNMENT` — alignment is always applied when word timestamps are enabled
- `TRANSCRIPTION_ENGINE` — engine selection is automatic based on model and device

### Docker Compose Usage

```bash
# Production deployment (recommended)
./opentranscribe.sh start

# Direct Docker Compose usage
docker compose up -d

# Check configuration
docker compose config
```

## Management Commands

### OpenTranscribe Script (`./opentranscribe.sh`)

```bash
# Start with hardware detection
./opentranscribe.sh start

# View real-time logs
./opentranscribe.sh logs

# Check system health
./opentranscribe.sh health

# Show hardware configuration
./opentranscribe.sh config

# Access container shell
./opentranscribe.sh shell backend

# Full system restart
./opentranscribe.sh restart

# Clean installation (⚠️ removes all data)
./opentranscribe.sh clean
```

### Development Script (`./opentr.sh`)

```bash
# Start development environment
./opentr.sh start dev

# Reset development database
./opentr.sh reset dev

# Rebuild specific services
./opentr.sh rebuild-backend
./opentr.sh rebuild-frontend

# Monitor logs
./opentr.sh logs backend
```

## Performance Comparison (v0.4.0, large-v3-turbo default)

| Platform | Model | Batch | VRAM/RAM | Single-file RTF | Peak RTF (concurrent) |
|----------|-------|-------|----------|-----------------|-----------------------|
| RTX A6000 (49GB) | large-v3-turbo | 16 | 49GB VRAM | **0.025** (40.3x) | **0.018** (54.6x at conc=8) |
| RTX 3080 Ti (12GB) | large-v3-turbo | 8 | 12GB VRAM | ~0.04 (25x) | ~0.04 (VRAM-limited) |
| RTX 4090 | large-v3-turbo | 16 | 24GB VRAM | ~0.03 (33x) | ~0.025 (40x) |
| RTX 3080 | large-v3-turbo | 8 | 10GB VRAM | ~0.05 (20x) | ~0.04 |
| M2 Max (MPS) | large-v3-turbo | 8 | 32GB unified | ~0.33 (3x) | N/A |
| M1 (MPS) | medium | 4 | 16GB unified | ~0.5 (2x) | N/A |
| CPU 16c | base | 4 | 16GB RAM | ~1.5 (0.67x) | N/A |
| Cloud ASR (lite mode) | — | — | 0 VRAM | Varies by provider | Parallel |

*RTF = Real-Time Factor (lower is faster; 0.025 RTF = 40x faster than real-time)*

**Note:** Old benchmark of "70x real-time with large-v2" is superseded. The current production default is `large-v3-turbo` which achieves **40.3x single-file** and **54.6x peak** on the RTX A6000. See `docs/BENCHMARK_RESULTS.md` for full benchmarks.

## Troubleshooting

### NVIDIA GPU Not Detected

```bash
# Check GPU availability
nvidia-smi

# Check Docker GPU support (use CUDA 12+ image for v0.4.0)
docker run --rm --gpus all nvidia/cuda:12.1-base-ubuntu22.04 nvidia-smi

# Install NVIDIA Container Toolkit
# Follow: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html

# Blackwell (RTX 50-series) requires Driver 570+ and CUDA 12.8+
# See docs/BLACKWELL_SETUP.md for Blackwell-specific setup
```

### Apple Silicon Issues

```bash
# Check macOS version (requires 12.3+)
sw_vers -productVersion

# Check Docker platform
docker --version

# Verify MPS availability
python3 -c "import torch; print(torch.backends.mps.is_available())"
```

### Memory Issues

```bash
# Reduce batch size
export BATCH_SIZE=4

# Use smaller model
export WHISPER_MODEL=small

# Check available memory
docker stats
```

### Performance Optimization

```bash
# Force specific device
export TORCH_DEVICE=cuda

# Optimize for memory
export COMPUTE_TYPE=int8

# Increase batch size (if memory allows)
export BATCH_SIZE=32
```

## Development

### Hardware Detection Module

Located at `backend/app/utils/hardware_detection.py`:

```python
from app.utils.hardware_detection import detect_hardware

# Get hardware configuration
config = detect_hardware()
print(config.get_summary())

# Get optimized settings
whisperx_config = config.get_whisperx_config()
```

### Adding New Platforms

1. Update `detect_hardware_acceleration()` in `hardware_detection.py`
2. Add platform-specific configuration in `setup-opentranscribe.sh`
3. Update docker-compose template in setup script
4. Test with the validation workflow

### Testing

```bash
# Test hardware detection
python3 -c "from backend.app.utils.hardware_detection import detect_hardware; print(detect_hardware().get_summary())"

# Validate Docker configuration
docker compose config

# End-to-end test
./opentranscribe.sh health
```

## Contributing

When contributing to cross-platform support:

1. Test on multiple platforms (Linux, macOS, Windows WSL)
2. Verify both GPU and CPU modes work
3. Check memory usage and performance
4. Update documentation for new features
5. Ensure backward compatibility

## License

Same as OpenTranscribe main project.

## Support

- **Issues:** GitHub Issues
- **Discussions:** GitHub Discussions
- **Documentation:** This README and inline code docs

---

**Ready to transcribe on any platform.**
