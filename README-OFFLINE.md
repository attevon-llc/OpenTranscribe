# OpenTranscribe Offline Installation Guide

Complete guide for deploying OpenTranscribe on air-gapped systems with no internet access.

## Table of Contents

- [Overview](#overview)
- [System Requirements](#system-requirements)
- [Building the Offline Package](#building-the-offline-package)
- [Installing on Air-Gapped System](#installing-on-air-gapped-system)
- [Configuration](#configuration)
- [Usage](#usage)
- [Troubleshooting](#troubleshooting)
- [Maintenance](#maintenance)
- [Uninstallation](#uninstallation)

## Overview

The OpenTranscribe offline package provides a complete, self-contained deployment solution for air-gapped environments. The package includes:

- All Docker container images
- Pre-downloaded AI models (~40GB)
  - WhisperX transcription models (large-v3-turbo default + large-v3)
  - PyAnnote speaker diarization models
  - OpenSearch neural search models (semantic search embeddings)
  - NLTK corpora (sentence splitting + topic-extraction stopwords). Fetched at
    runtime if absent, which an airgapped host cannot do — `download-models.sh`
    is what puts them in the package (issue #491)
  - Word-level timestamps natively supported for all 100+ languages
- Configuration files and templates
- Installation and management scripts
- Complete documentation

**Package Size:** 15-20GB compressed (tar.xz), ~65GB uncompressed (compression optional)

## System Requirements

### Build System (Internet-Connected)
Required to create the offline package:

- Ubuntu 20.04+ or similar Linux distribution
- Docker 20.10+
- Docker Compose v2+
- 100GB free disk space
- Fast internet connection
- HuggingFace account and token

### Target System (Air-Gapped)
System where OpenTranscribe will be installed:

- Ubuntu 20.04+ (recommended) or compatible Linux distribution
- Docker 20.10 or later
- Docker Compose v2+
- NVIDIA GPU with CUDA support (recommended)
  - Minimum: 8GB VRAM
  - Recommended: 16GB+ VRAM
- NVIDIA GPU drivers (470.x or later)
- NVIDIA Container Toolkit
- 80GB free disk space
- 16GB RAM minimum (32GB recommended)
- CPU: 4+ cores recommended

**Note:** OpenTranscribe can run without a GPU in CPU mode, but transcription will be significantly slower.

## Building the Offline Package

### Prerequisites

1. **Set HuggingFace Token:**
   ```bash
   export HUGGINGFACE_TOKEN=your_token_here
   ```
   Get your token at: https://huggingface.co/settings/tokens

2. **Clone Repository:**
   ```bash
   git clone https://github.com/davidamacey/opentranscribe.git
   cd opentranscribe
   ```

### Build Process

1. **Run the Build Script:**
   ```bash
   ./scripts/build-offline-package.sh
   ```

   The script will:
   - Validate system requirements
   - Pull all required Docker images from DockerHub
   - Download AI models using your HuggingFace token
   - Package configuration files and scripts
   - **Prompt for compression** (optional - see below)
   - Create integrity checksums

2. **Compression Options:**

   At the end of the build, you'll be prompted:
   ```
   Do you want to compress the package now? (y/n):
   ```

   **Option 1: Compress Now (recommended for transfer)**
   - Takes 30-60 minutes using all CPU threads
   - Creates `.tar.xz` file (15-20GB)
   - Best for network transfer or USB

   **Option 2: Skip Compression**
   - Saves time if testing locally
   - Leaves uncompressed directory (~65GB)
   - Can compress manually later if needed
   - Useful for fast local network transfers

3. **Build Duration:**
   - Image pulling: 10-20 minutes
   - Model downloading: 30-60 minutes
   - Compression: 30-60 minutes (if selected)
   - **Total: 1-2 hours (with compression)**
   - **Total: 30-90 minutes (without compression)**

4. **Output:**

   **If compressed:**
   ```
   offline-package-build/
   ├── opentranscribe-offline-v{version}.tar.xz      (~15-20GB)
   └── opentranscribe-offline-v{version}.tar.xz.sha256
   ```

   **If uncompressed:**
   ```
   offline-package-build/
   ├── opentranscribe-offline-v{version}/            (~65GB directory)
   └── opentranscribe-offline-v{version}.sha256
   ```

5. **Verify Package:**

   **If compressed:**
   ```bash
   cd offline-package-build
   sha256sum -c opentranscribe-offline-v*.tar.xz.sha256
   ```

   **If uncompressed:**
   ```bash
   cd offline-package-build
   # Checksums are stored in the .sha256 file for individual verification
   ```

6. **Manual Compression (Optional):**

   If you skipped compression, you can compress later:
   ```bash
   cd offline-package-build
   tar -cf - opentranscribe-offline-v* | xz -9 -T0 > opentranscribe-offline-v{version}.tar.xz
   sha256sum opentranscribe-offline-v*.tar.xz > opentranscribe-offline-v*.tar.xz.sha256
   ```

### Transfer to Air-Gapped System

Transfer the package to your air-gapped system using your preferred method:

**If compressed:**
- Transfer both `.tar.xz` and `.tar.xz.sha256` files
- USB drive, network transfer, or physical media

**If uncompressed:**
- Transfer entire directory or compress first (see manual compression above)
- For directory transfer: use rsync, network share, or external drive

## Installing on Air-Gapped System

### Pre-Installation Setup

1. **Install Docker** (if not already installed):
   ```bash
   # Follow Docker's official installation guide for your distribution
   # https://docs.docker.com/engine/install/ubuntu/
   ```

2. **Install NVIDIA Drivers and Container Toolkit** (for GPU support):
   ```bash
   # Install NVIDIA drivers (if not already installed)
   ubuntu-drivers devices
   sudo ubuntu-drivers autoinstall

   # Install NVIDIA Container Toolkit
   # https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html
   ```

3. **Verify GPU Setup:**
   ```bash
   nvidia-smi
   docker run --rm --gpus all nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi
   ```

### Installation Steps

1. **Extract Package (if compressed):**

   **If you have a compressed `.tar.xz` file:**
   ```bash
   tar -xf opentranscribe-offline-v*.tar.xz
   cd opentranscribe-offline-v*/
   ```

   **If you transferred the uncompressed directory:**
   ```bash
   cd opentranscribe-offline-v*/
   ```

2. **Run Installer:**
   ```bash
   sudo ./install.sh
   ```

   The installer will:
   - Validate system requirements
   - Verify package integrity
   - Load Docker images (15-30 minutes)
   - Install files to `/opt/opentranscribe/`
   - Copy AI models (10-20 minutes)
   - Create configuration file
   - Set proper permissions

3. **Installation Duration:**
   - System validation: 1-2 minutes
   - Docker image loading: 15-30 minutes
   - Model installation: 10-20 minutes
   - **Total: 30-60 minutes**

4. **Post-Installation:**
   The installer will display next steps when complete.

## Configuration

### Required Configuration

1. **Edit Environment File:**
   ```bash
   sudo nano /opt/opentranscribe/.env
   ```

2. **Set HuggingFace Token (REQUIRED):**
   ```bash
   HUGGINGFACE_TOKEN=your_token_here
   ```

   **Important:** Speaker diarization requires a HuggingFace token. Get one at https://huggingface.co/settings/tokens

### Optional Configuration

The `.env` file contains auto-detected settings. You may customize:

**Security Settings:**
- `POSTGRES_PASSWORD` - Database password (auto-generated)
- `MINIO_ROOT_PASSWORD` - Object storage password (auto-generated)
- `JWT_SECRET_KEY` - JWT signing key (auto-generated)

**AI Model Settings:**
- `WHISPER_MODEL` - Transcription model size (default: large-v3-turbo)
  - Options: tiny, base, small, medium, large-v1, large-v2, large-v3, large-v3-turbo
  - **Note:** `large-v3-turbo` is 6x faster than `large-v3` with excellent accuracy for English and most languages. Use `large-v3` for translation tasks or maximum accuracy on low-resource languages.
- `BATCH_SIZE` - Processing batch size (default: 16)
- `MIN_SPEAKERS` / `MAX_SPEAKERS` - Speaker detection range (default: 1-20, can be increased to 50+ for large events)

**Hardware Settings (auto-detected):**
- `USE_GPU` - Enable GPU acceleration
- `TORCH_DEVICE` - Device type (cuda/cpu)
- `COMPUTE_TYPE` - Precision (float16/int8)
- `GPU_DEVICE_ID` - GPU to use (default: 0)

**LLM Integration (optional):**
For AI summarization and speaker identification features:
- `LLM_PROVIDER` - Provider (openai, anthropic, openrouter)
- Provider-specific API keys and settings

**Note:** LLM features require internet access. Leave `LLM_PROVIDER` empty for offline transcription-only mode.

**Neural Search Settings:**
- OpenSearch neural search is included in the offline package with pre-downloaded embedding models
- Uses `sentence-transformers/all-MiniLM-L6-v2` for semantic search (~80MB model)
- Supports both full-text and neural/semantic search queries
- No additional configuration needed for offline neural search support
- Requires 2GB additional memory per OpenSearch container for embeddings

### Port Configuration

Default ports (configurable in `.env`):
- Frontend: `80`
- Backend API: `8080`
- Flower (task monitor): `5175`
- Database: `5432`
- Redis: `6379`
- MinIO: `9000`
- MinIO Console: `9001`
- OpenSearch: `9200`

## Usage

### Starting OpenTranscribe

```bash
cd /opt/opentranscribe
sudo ./opentr.sh start
```

**Access the application:** http://localhost:80

### Management Commands

All commands run from `/opt/opentranscribe/`:

**Basic Operations:**
```bash
sudo ./opentr.sh start              # Start all services
sudo ./opentr.sh stop               # Stop all services
sudo ./opentr.sh restart-all        # Restart all services
sudo ./opentr.sh status             # Show service status
sudo ./opentr.sh logs               # View all logs (Ctrl+C to exit)
sudo ./opentr.sh logs backend       # View specific service logs
```

**Service Management:**
```bash
sudo ./opentr.sh restart-backend    # Restart backend services only
sudo ./opentr.sh restart-frontend   # Restart frontend only
sudo ./opentr.sh shell backend      # Open shell in backend container
```

**Maintenance:**
```bash
sudo ./opentr.sh health              # Check health of all services
sudo ./opentr.sh backup              # Create database backup
sudo docker system prune -a          # Clean up unused Docker resources
```

### First-Time Setup

1. Start OpenTranscribe:
   ```bash
   cd /opt/opentranscribe
   sudo ./opentr.sh start
   ```

2. Wait for services to start (~30 seconds):
   ```bash
   sudo ./opentr.sh health
   ```

3. Access web interface: http://localhost:80

4. Create your first user account through the web interface

5. Upload an audio or video file to test transcription

### Monitoring

**Service Status:**
```bash
sudo ./opentr.sh status
```

**Task Monitoring:**
Access Flower dashboard at: http://localhost:5175/flower

**Logs:**
```bash
# All services
sudo ./opentr.sh logs

# Specific service
sudo ./opentr.sh logs celery-worker

# Follow logs in real-time
sudo ./opentr.sh logs -f backend
```

## Troubleshooting

### Services Won't Start

**Check Docker status:**
```bash
sudo systemctl status docker
sudo systemctl start docker
```

**Check service logs:**
```bash
cd /opt/opentranscribe
sudo ./opentr.sh logs
```

**Check service health:**
```bash
sudo ./opentr.sh health
```

### GPU Not Detected

**Verify NVIDIA drivers:**
```bash
nvidia-smi
```

**Verify Container Toolkit:**
```bash
docker run --rm --gpus all nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi
```

**Check configuration:**
```bash
grep USE_GPU /opt/opentranscribe/.env
```

**Manual GPU enable:**
Edit `/opt/opentranscribe/.env`:
```bash
USE_GPU=true
TORCH_DEVICE=cuda
COMPUTE_TYPE=float16
```

Then restart:
```bash
sudo ./opentr.sh restart-all
```

### Transcription Fails

**Check HuggingFace token:**
```bash
grep HUGGINGFACE_TOKEN /opt/opentranscribe/.env
```

**Check worker logs:**
```bash
sudo ./opentr.sh logs celery-worker
```

**Check Flower dashboard:**
http://localhost:5175/flower

### Out of Memory

**For systems with limited VRAM:**

Edit `/opt/opentranscribe/.env`:
```bash
WHISPER_MODEL=medium    # or small, base
BATCH_SIZE=8            # reduce from 16
```

Restart services:
```bash
sudo ./opentr.sh restart-backend
```

### Database Issues

**Check database status:**
```bash
sudo ./opentr.sh logs postgres
```

**Access database shell:**
```bash
sudo ./opentr.sh shell postgres
psql -U postgres -d opentranscribe
```

### Port Conflicts

If default ports are in use, edit `/opt/opentranscribe/.env`:
```bash
FRONTEND_PORT=8080     # Change from 80
BACKEND_PORT=8081      # Change from 8080
# etc.
```

Restart:
```bash
sudo ./opentr.sh restart-all
```

### Performance Issues

**CPU Mode:** Transcription in CPU mode is 10-50x slower than GPU mode.

**GPU Optimization:**
- Use `COMPUTE_TYPE=float16` for NVIDIA GPUs
- Increase `BATCH_SIZE` if you have >16GB VRAM
- Use `large-v3-turbo` (default) for balanced speed/accuracy (requires 6GB+ VRAM)
- Use `large-v3` for maximum accuracy or translation tasks (requires 10GB+ VRAM)

**System Resources:**
- Monitor with: `docker stats`
- Increase RAM allocation if needed
- Close other GPU-intensive applications

## Maintenance

### Database Backups

**Create backup:**
```bash
sudo ./opentr.sh backup
```

Backups stored in: `/opt/opentranscribe/backups/`

**Restore backup:**
```bash
cd /opt/opentranscribe
sudo ./opentr.sh stop
docker compose -f docker-compose.yml -f docker-compose.offline.yml run --rm postgres psql -U postgres -d opentranscribe < backups/backup_file.sql
sudo ./opentr.sh start
```

### Updates

For offline systems, updates require a new offline package:

1. Build new package on internet-connected system
2. Transfer to air-gapped system
3. Stop OpenTranscribe: `sudo ./opentr.sh stop`
4. Backup data: `sudo ./opentr.sh backup`
5. Extract new package and run installer
6. Restore data if needed

### Logs Management

**View disk usage:**
```bash
docker system df
```

**Clean old logs:**
```bash
sudo docker system prune -a
```

**Rotate logs:**
Docker automatically rotates logs, but you can manually clean:
```bash
docker system prune -a
```

### Model Updates

To update AI models, you need internet access or a new model package:

1. Stop services: `sudo ./opentr.sh stop`
2. Replace model files in `/opt/opentranscribe/models/`
3. Start services: `sudo ./opentr.sh start`

## Uninstallation

### Automated Uninstallation (Recommended)

**Run the uninstall script:**
```bash
cd /opt/opentranscribe
sudo ./uninstall.sh
```

The uninstall script will:
- Offer to create a database backup before removal
- Stop all OpenTranscribe services
- Remove Docker volumes (with confirmation)
- Optionally remove Docker images
- Remove the installation directory `/opt/opentranscribe/`
- Optionally clean up unused Docker resources

This is the safest and most complete way to uninstall OpenTranscribe.

### Manual Uninstallation

If you prefer to uninstall manually or the script is unavailable:

**Stop and remove services:**
```bash
cd /opt/opentranscribe
sudo ./opentr.sh stop
sudo docker compose -f docker-compose.yml -f docker-compose.offline.yml down -v
```

**Remove installation:**
```bash
sudo rm -rf /opt/opentranscribe
```

**Remove Docker images (optional):**
```bash
docker rmi davidamacey/opentranscribe-backend:latest
docker rmi davidamacey/opentranscribe-frontend:latest
docker rmi postgres:17.5-alpine redis:8.2.2-alpine3.22
docker rmi minio/minio:RELEASE.2025-09-07T16-13-09Z
docker rmi opensearchproject/opensearch:3.4.0
```

**Clean Docker system:**
```bash
docker system prune -a
docker volume prune
```

## Additional Resources

### File Locations

- Installation: `/opt/opentranscribe/`
- Configuration: `/opt/opentranscribe/.env`
- Database data: Docker volume `opentranscribe_postgres_data`
- Object storage: Docker volume `opentranscribe_minio_data`
- AI models: `/opt/opentranscribe/models/`
- Temp files: `/opt/opentranscribe/temp/`
- Backups: `/opt/opentranscribe/backups/`

### Service Architecture

```
Frontend (NGINX + Svelte)
    ↓
Backend (FastAPI)
    ↓
├── PostgreSQL (Database)
├── MinIO (Object Storage)
├── Redis (Message Queue)
├── OpenSearch (Search Engine)
└── Celery Worker (AI Processing)
        ↓
    AI Models (WhisperX, PyAnnote)
```

### Support

For issues and questions:
- GitHub Issues: https://github.com/davidamacey/opentranscribe/issues
- Documentation: https://github.com/davidamacey/opentranscribe

### License

OpenTranscribe is open source software. See LICENSE file for details.

---

**Last Updated:** October 2024
**Version:** 2.0 Offline
