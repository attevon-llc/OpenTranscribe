---
sidebar_position: 3
---

# GPU Setup

OpenTranscribe delivers dramatic performance improvements with NVIDIA GPU acceleration. This guide covers GPU setup, configuration, and optimization for transcription workloads.

## Performance Impact

**GPU vs CPU Transcription Speed**:

| Hardware | Speed | 1-Hour File | 3-Hour Meeting |
|----------|-------|-------------|----------------|
| **CPU Only** | 0.5-1x realtime | ~60-120 min | ~3-6 hours |
| **NVIDIA GPU** | 70x realtime | ~50 seconds | ~2.5 minutes |

:::tip[70x Faster]
With GPU acceleration, OpenTranscribe processes audio/video **70 times faster than realtime**. A 3-hour meeting transcribes in just 2.5 minutes!
:::

## Prerequisites

### Supported GPUs

OpenTranscribe supports NVIDIA GPUs with CUDA capability:

**Consumer GPUs**:
- RTX 30 Series: 3060 (12GB), 3070 (8GB), 3080 (10-12GB), 3090 (24GB)
- RTX 40 Series: 4060 Ti (16GB), 4070 (12GB), 4080 (16GB), 4090 (24GB)
- GTX 16 Series: 1660 (6GB), 1660 Ti (6GB) - Limited performance

**Professional GPUs**:
- RTX A Series: A2000 (6GB), A4000 (16GB), A5000 (24GB), A6000 (48GB)
- Tesla Series: T4 (16GB), P40 (24GB)
- Data Center: V100 (16-32GB), A100 (40-80GB), H100 (80GB)

**Minimum VRAM**: 6GB (transcription only), 8GB (with speaker diarization)

:::warning[macOS Note]
NVIDIA GPUs are not supported on macOS (Apple deprecated CUDA support). macOS users will use CPU-only mode or Apple Silicon optimizations (experimental).
:::

### Software Requirements

- **NVIDIA Driver**: 525.60.13 or newer
- **CUDA Toolkit**: 11.8 or newer (12.x recommended)
- **Docker**: 24.0+ with NVIDIA Container Toolkit
- **Operating System**: Linux (best), Windows with WSL2

## Linux Installation

### Step 1: Install NVIDIA Drivers

**Ubuntu/Debian**:
```bash
# Add NVIDIA driver PPA
sudo add-apt-repository ppa:graphics-drivers/ppa
sudo apt update

# Install recommended driver
sudo ubuntu-drivers autoinstall

# Or install specific version
sudo apt install nvidia-driver-535

# Reboot required
sudo reboot
```

**RHEL/CentOS/Fedora**:
```bash
# Enable NVIDIA repository
sudo dnf config-manager --add-repo https://developer.download.nvidia.com/compute/cuda/repos/rhel8/x86_64/cuda-rhel8.repo

# Install driver
sudo dnf install nvidia-driver nvidia-driver-cuda

# Reboot required
sudo reboot
```

**Verify driver installation**:
```bash
nvidia-smi
```

Expected output:
```
+-----------------------------------------------------------------------------+
| NVIDIA-SMI 535.129.03   Driver Version: 535.129.03   CUDA Version: 12.2    |
|-------------------------------+----------------------+----------------------+
| GPU  Name        Persistence-M| Bus-Id        Disp.A | Volatile Uncorr. ECC |
| Fan  Temp  Perf  Pwr:Usage/Cap|         Memory-Usage | GPU-Util  Compute M. |
|===============================+======================+======================|
|   0  NVIDIA RTX 3080    Off  | 00000000:01:00.0  On |                  N/A |
| 30%   45C    P8    25W / 320W |    512MiB / 12288MiB |      0%      Default |
+-------------------------------+----------------------+----------------------+
```

### Step 2: Install NVIDIA Container Toolkit

**Ubuntu/Debian**:
```bash
# Add NVIDIA Docker repository
distribution=$(. /etc/os-release;echo $ID$VERSION_ID)
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg

curl -s -L https://nvidia.github.io/libnvidia-container/$distribution/libnvidia-container.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

# Install toolkit
sudo apt update
sudo apt install -y nvidia-container-toolkit

# Configure Docker
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

**RHEL/CentOS/Fedora**:
```bash
# Add NVIDIA Container Toolkit repository
sudo dnf config-manager --add-repo https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo

# Install toolkit
sudo dnf install -y nvidia-container-toolkit

# Configure Docker
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

### Step 3: Verify Docker GPU Access

```bash
# Test GPU access in Docker container
docker run --rm --gpus all nvidia/cuda:12.2.0-base-ubuntu22.04 nvidia-smi
```

If successful, you should see the same `nvidia-smi` output as before.

## Windows (WSL2) Installation

### Step 1: Install WSL2

```powershell
# In PowerShell (Administrator)
wsl --install
wsl --set-default-version 2

# Restart required
```

### Step 2: Install NVIDIA Driver (Windows)

1. Download latest **Windows** driver from [NVIDIA Website](https://www.nvidia.com/download/index.aspx)
2. Install driver on Windows (NOT inside WSL2)
3. Reboot Windows

:::warning[Important]
Install the NVIDIA driver on **Windows host**, not inside WSL2. WSL2 will automatically detect and use the Windows driver.
:::

### Step 3: Verify GPU Access in WSL2

```bash
# Inside WSL2 Ubuntu
nvidia-smi
```

If successful, GPU is accessible from WSL2.

### Step 4: Install Docker Desktop

1. Download [Docker Desktop for Windows](https://www.docker.com/products/docker-desktop)
2. Install with **WSL2 backend** enabled
3. Enable GPU support in Docker Desktop settings

## OpenTranscribe GPU Configuration

### Automatic Configuration (Recommended)

The one-liner installer automatically detects and configures GPU settings:

```bash
curl -fsSL https://raw.githubusercontent.com/attevon-llc/OpenTranscribe/master/setup-opentranscribe.sh | bash
```

The installer will:
- ✅ Detect NVIDIA GPU
- ✅ Test GPU accessibility
- ✅ Configure optimal settings in `.env`
- ✅ Set CUDA device ID
- ✅ Enable GPU processing

### Manual Configuration

Edit `.env` file:

```bash
#=============================================================================
# HARDWARE DETECTION & GPU CONFIGURATION
#=============================================================================

# Hardware Configuration
# Options: auto, cuda, mps, cpu
TORCH_DEVICE=cuda

# Compute Type (precision)
# Options: auto, float16, float32, int8
# Recommended: float16 for GPUs with Tensor Cores
COMPUTE_TYPE=float16

# Enable GPU Usage
# Options: auto, true, false
USE_GPU=true

# GPU Device Selection (for multi-GPU systems)
# 0 = first GPU, 1 = second GPU, etc.
GPU_DEVICE_ID=0

# Batch Size (larger = faster but more VRAM)
# Options: auto, or specific number (1, 8, 16, 32)
# Recommended: auto (optimal based on GPU memory)
BATCH_SIZE=auto

# Whisper Model (larger = more accurate but slower)
# Options: tiny, base, small, medium, large-v1, large-v2, large-v3
# Recommended: large-v2 for GPU, small for CPU
WHISPER_MODEL=large-v2
```

## Multi-GPU Configuration

### Selecting GPU Device

For systems with multiple GPUs:

```bash
# Check available GPUs
nvidia-smi -L
```

Output example:
```
GPU 0: NVIDIA RTX A6000 (UUID: GPU-12345678-1234-1234-1234-123456789abc)
GPU 1: NVIDIA RTX 3080 Ti (UUID: GPU-87654321-4321-4321-4321-cba987654321)
GPU 2: NVIDIA RTX A6000 (UUID: GPU-abcdefgh-abcd-abcd-abcd-abcdefghijkl)
```

Configure which GPU to use:
```bash
# Use GPU 1 (RTX 3080 Ti)
GPU_DEVICE_ID=1
```

### Multi-GPU Worker Scaling

For high-throughput systems with multiple GPUs, enable parallel GPU workers:

```bash
# .env configuration
GPU_SCALE_ENABLED=true      # Enable multi-GPU scaling
GPU_SCALE_DEVICE_ID=2       # Which GPU to use for scaled workers
GPU_SCALE_WORKERS=4         # Number of parallel workers

# Start with GPU scaling
./opentr.sh start dev --gpu-scale
```

**Example hardware setup**:
- GPU 0: NVIDIA RTX A6000 (49GB) - Local LLM model (vLLM/Ollama)
- GPU 1: RTX 3080 Ti (12GB) - Default single worker (disabled when scaling)
- GPU 2: NVIDIA RTX A6000 (49GB) - 4 parallel workers (processes 4 videos simultaneously)

**Performance**: Process 4 transcriptions simultaneously, achieving 280x realtime total throughput.

See [Multi-GPU Scaling](../configuration/multi-gpu-scaling.md) for detailed configuration.

## Performance Optimization

### VRAM Requirements by Model

| Model | VRAM (Transcription) | VRAM (+ Diarization) |
|-------|----------------------|----------------------|
| tiny | 1-2GB | 3-4GB |
| base | 1-2GB | 3-4GB |
| small | 2-3GB | 4-5GB |
| medium | 3-5GB | 5-7GB |
| large-v2 | 5-6GB | 7-8GB |
| large-v3 | 5-6GB | 7-8GB |

:::tip[Recommended Settings]
- **8GB GPU**: large-v2 model, single worker
- **12GB GPU**: large-v2 model, 2-3 parallel workers
- **24GB+ GPU**: large-v2 model, 4-6 parallel workers
:::

### Batch Size Tuning

Larger batch sizes increase throughput but require more VRAM:

```bash
# Conservative (6GB+ GPU)
BATCH_SIZE=8

# Balanced (12GB+ GPU)
BATCH_SIZE=16

# Aggressive (24GB+ GPU)
BATCH_SIZE=32

# Auto-detect (recommended)
BATCH_SIZE=auto
```

### Compute Type Selection

| Compute Type | Precision | Speed | VRAM | Quality |
|--------------|-----------|-------|------|---------|
| float32 | Full | Slow | High | Best |
| float16 | Half | Fast | Medium | Excellent |
| int8 | Quantized | Fastest | Low | Good |

**Recommendation**: `float16` for best balance (default on modern GPUs)

## Monitoring GPU Usage

### Real-Time Monitoring

```bash
# Watch GPU usage continuously
watch -n 1 nvidia-smi
```

### Check GPU Memory During Transcription

```bash
# Monitor specific GPU
nvidia-smi -i 0 -l 1

# Show only memory usage
nvidia-smi --query-gpu=memory.used,memory.total --format=csv -l 1
```

### Docker Container GPU Stats

```bash
# Check GPU usage by container
docker stats celery-worker
```

## Troubleshooting

### GPU Not Detected

**Symptom**: OpenTranscribe falls back to CPU mode

**Check**:
```bash
# 1. Verify NVIDIA driver
nvidia-smi

# 2. Check Docker GPU access
docker run --rm --gpus all nvidia/cuda:12.2.0-base-ubuntu22.04 nvidia-smi

# 3. Check OpenTranscribe logs
./opentr.sh logs celery-worker | grep -i cuda
```

**Solutions**:
- Reinstall NVIDIA drivers
- Install NVIDIA Container Toolkit
- Restart Docker daemon: `sudo systemctl restart docker`
- Check `.env` has `USE_GPU=true`

### Out of Memory Errors

**Symptom**: `CUDA out of memory` errors in logs

**Solutions**:
1. Reduce batch size:
   ```bash
   BATCH_SIZE=8  # or smaller
   ```

2. Use smaller model:
   ```bash
   WHISPER_MODEL=medium  # instead of large-v2
   ```

3. Reduce concurrent workers (multi-GPU scaling):
   ```bash
   GPU_SCALE_WORKERS=2  # instead of 4
   ```

4. Close other GPU applications

### Slow Performance Despite GPU

**Check GPU utilization**:
```bash
nvidia-smi
```

If GPU utilization is low (less than 50%):

1. **Check compute type**:
   ```bash
   COMPUTE_TYPE=float16  # not float32
   ```

2. **Increase batch size**:
   ```bash
   BATCH_SIZE=16  # or higher
   ```

3. **Verify CUDA version**:
   ```bash
   nvidia-smi | grep "CUDA Version"
   # Should be 11.8+
   ```

### Mixed GPU/CPU Mode

**Symptom**: Some tasks use GPU, others use CPU

**Cause**: Insufficient VRAM for full GPU pipeline

**Solution**:
- Use smaller model for GPU with less than 8GB VRAM
- Disable speaker diarization temporarily
- Upgrade to GPU with more VRAM

## Verification

### Test GPU Transcription

1. Start OpenTranscribe:
   ```bash
   ./opentr.sh start dev
   ```

2. Upload a test file

3. Check logs for GPU usage:
   ```bash
   ./opentr.sh logs celery-worker | grep -i "cuda\|gpu"
   ```

Success indicators:
```
✅ "Using device: cuda"
✅ "Loading model on CUDA device 0"
✅ "GPU memory available: 12288 MB"
✅ "Transcription completed in 45.2s (70.5x realtime)"
```

### Performance Benchmark

Upload a 1-hour audio file and measure processing time:

- **Expected with GPU**: 45-60 seconds (60-80x realtime)
- **Expected with CPU**: 45-90 minutes (0.7-1.3x realtime)

If GPU performance is significantly slower, review optimization settings.

## Cloud GPU Setup

### AWS EC2

1. Launch GPU instance (g4dn.xlarge or better)
2. Install NVIDIA drivers:
   ```bash
   sudo apt update
   sudo apt install -y ubuntu-drivers-common
   sudo ubuntu-drivers autoinstall
   sudo reboot
   ```
3. Install Docker and NVIDIA Container Toolkit (see Linux installation)
4. Run OpenTranscribe installer

### Google Cloud (GCP)

1. Create VM with GPU (T4, V100, or A100)
2. Install NVIDIA drivers:
   ```bash
   curl https://raw.githubusercontent.com/GoogleCloudPlatform/compute-gpu-installation/main/linux/install_gpu_driver.py --output install_gpu_driver.py
   sudo python3 install_gpu_driver.py
   ```
3. Install Docker and NVIDIA Container Toolkit
4. Run OpenTranscribe installer

### Azure

1. Deploy GPU VM (NC or ND series)
2. Install NVIDIA drivers (pre-installed on some images)
3. Install Docker and NVIDIA Container Toolkit
4. Run OpenTranscribe installer

## Next Steps

- [Docker Compose Installation](./docker-compose.md) - Complete installation
- [Hardware Requirements](./hardware-requirements.md) - Optimize hardware
- [Multi-GPU Scaling](../configuration/multi-gpu-scaling.md) - Scale throughput
- [Environment Variables](../configuration/environment-variables.md) - Fine-tune settings
