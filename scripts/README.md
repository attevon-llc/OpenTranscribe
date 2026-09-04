# OpenTranscribe Build Scripts

This directory contains scripts for building Docker images, creating offline packages, and managing deployments.

## Quick Start

**Run complete build pipeline:**
```bash
# Interactive mode (recommended for first-time use)
./scripts/build-all.sh --interactive

# Direct execution with auto-detected version
./scripts/build-all.sh

# With specific version
./scripts/build-all.sh v2.1.0

# Background execution (non-interactive)
nohup ./scripts/build-all.sh > build-$(date +%Y%m%d-%H%M%S).log 2>&1 &
```

See [docs/BUILD_PIPELINE.md](../docs/BUILD_PIPELINE.md) for complete documentation.

## Testing & dev-cycle scripts

This README covers the build/offline-package/model-download family only. The ~60 test-gate,
e2e, and release-rehearsal scripts (including the two below) are documented in
[scripts/CLAUDE.md](CLAUDE.md)'s "Which script to reach for" — read that file for the full
picture rather than this list, which stays short on purpose.

```bash
./scripts/run-dev-tests.sh --full     # quick "does my branch work" check (backend+e2e+frontend,
                                       # one command, one consolidated report; minutes)
./scripts/test-matrix.sh all          # exhaustive deployment-mode rehearsal (dev/prod/lite/PKI/
                                       # GPU-scale/fresh-install/upgrade); run before a release
```

## Scripts Overview

- **[build-all.sh](#complete-build-pipeline)** - Complete build pipeline (Docker + security + offline package)
- **[docker-build-push.sh](#docker-build--push-script)** - Build and push Docker images to DockerHub
- **[setup-remote-builder.sh](#remote-arm64-builder-setup)** - Configure remote ARM64 builder for faster builds
- **[security-scan.sh](#security-scanner)** - Comprehensive security scanning for Docker images
- **[build-offline-package.sh](#offline-package-builder)** - Create air-gapped deployment packages
- **[install-offline-package.sh](#installation-script)** - Install OpenTranscribe on offline systems
- **[opentr-offline.sh](#offline-management-wrapper)** - Manage offline installations
- **[download-models.py](#model-downloader)** - Download AI models for offline packaging
- **[fix-model-permissions.sh](#model-cache-permission-fixer)** - Fix permissions for non-root container migration

---

## Complete Build Pipeline

Unified script that executes the complete build, security scan, and packaging workflow with optional interactive mode.

### What It Does

Runs two main phases:
1. **Docker Build & Security Scanning** (~30-45 min)
   - Builds multi-arch images (AMD64 + ARM64)
   - Pushes to Docker Hub
   - Runs security scans (Trivy, Grype, Syft, Hadolint, Dockle)

2. **Offline Package Creation** (~1.5-2 hrs)
   - Pulls images from registry
   - Downloads AI models
   - Creates deployment package

### Usage

```bash
# Interactive mode (recommended for first-time use)
./scripts/build-all.sh --interactive

# Direct execution
./scripts/build-all.sh                      # Auto-detected version
./scripts/build-all.sh v2.1.0               # Specific version
./scripts/build-all.sh -i v2.1.0            # Interactive with version

# Background execution
nohup ./scripts/build-all.sh > build-$(date +%Y%m%d-%H%M%S).log 2>&1 &

# Fast mode (testing)
SKIP_SECURITY_SCAN=true PLATFORMS=linux/amd64 ./scripts/build-all.sh

# Get help
./scripts/build-all.sh --help
```

### Interactive Mode

When using `--interactive` or `-i`, the script will:
1. Check Docker login status
2. Validate HuggingFace token configuration
3. Verify sufficient disk space (100GB+)
4. Prompt for version tag (optional)
5. Display configuration summary
6. Ask for confirmation before starting build
7. Show estimated completion time

Example:
```
./scripts/build-all.sh --interactive

[1/4] Checking Docker login...
✅ Docker logged in as: davidamacey

[2/4] Checking HuggingFace token...
✅ HuggingFace token configured

[3/4] Checking disk space...
✅ Sufficient disk space: 150GB

[4/4] Version configuration...
Current git commit: a1b2c3d
Enter version tag (or press Enter to use git commit SHA): v2.1.0
✅ Version: v2.1.0

Build Configuration
Version:               v2.1.0
Docker Hub User:       davidamacey
Estimated Time:        2-3 hours

Start build pipeline? (y/N) y
```

### Prerequisites

- Docker logged in: `docker login`
- HuggingFace token: `export HUGGINGFACE_TOKEN=hf_...` or in `.env`
- 100GB+ free disk space

### Output

- Docker images on Docker Hub (`:latest` and `:{version}` tags)
- Security reports in `./security-reports/`
- Offline package in `./offline-package-build/`

### Documentation

- [docs/BUILD_PIPELINE.md](../docs/BUILD_PIPELINE.md) - Complete documentation

---

## Offline Package Builder

Create complete offline installation packages for air-gapped deployments.

### Prerequisites

**Internet-connected build system:**
- Docker 20.10+
- Docker Compose v2+
- 100GB free disk space
- HuggingFace account and token
- `tar`, `xz`, `git` installed

### Usage

```bash
# Set your HuggingFace token
export HUGGINGFACE_TOKEN=your_token_here

# Run the builder
./scripts/build-offline-package.sh [version]

# Example with custom version
./scripts/build-offline-package.sh v2.0.1

# Without version (uses git commit SHA)
./scripts/build-offline-package.sh
```

### What It Does

1. **Validates** system requirements and Docker setup
2. **Pulls** all required Docker images from DockerHub:
   - `davidamacey/opentranscribe-backend:latest` (~13.8GB)
   - `davidamacey/opentranscribe-frontend:latest` (~51MB)
   - `postgres:14-alpine` (~220MB)
   - `redis:7-alpine` (~30MB)
   - `minio/minio:latest` (~175MB)
   - `opensearchproject/opensearch:3.4.0` (~800MB)
3. **Downloads** AI models (~38GB):
   - WhisperX models (~1.5GB)
   - PyAnnote diarization models (~500MB)
4. **Packages** configuration files and scripts
5. **Creates** checksums for integrity verification
6. **Compresses** everything with multi-threaded xz compression

### Output

```
offline-package-build/
├── opentranscribe-offline-v{version}.tar.xz      (~15-20GB compressed)
└── opentranscribe-offline-v{version}.tar.xz.sha256
```

### Build Time

- Image pulling: 10-20 minutes
- Model downloading: 30-60 minutes
- Compression: 30-60 minutes
- **Total: 1-2 hours**

### Package Contents

```
opentranscribe-offline-v{version}/
├── install.sh                          # Installation script
├── opentr-offline.sh                   # Management wrapper
├── docker-images/                      # Docker image tar files
│   ├── backend.tar
│   ├── frontend.tar
│   ├── postgres.tar
│   ├── redis.tar
│   ├── minio.tar
│   ├── opensearch.tar
│   └── metadata.json
├── models/                             # Pre-downloaded AI models
│   ├── huggingface/
│   ├── torch/
│   └── model_manifest.json
├── config/
│   ├── docker-compose.offline.yml
│   ├── .env.template
│   └── nginx.conf
├── database/
│   └── init_db.sql
├── scripts/
│   ├── common.sh
│   └── download-models.py
├── checksums.sha256
├── package-info.json
└── README-OFFLINE.md
```

### Verification

```bash
cd offline-package-build
sha256sum -c opentranscribe-offline-v*.tar.xz.sha256
```

### Transfer to Air-Gapped System

Transfer the `.tar.xz` file and `.sha256` checksum file to your offline system using:
- USB drive
- Network file transfer (if available)
- Physical media

---

## Installation Script

Installs OpenTranscribe on air-gapped systems with no internet access.

### Prerequisites (Target System)

- Ubuntu 20.04+ or compatible Linux
- Docker 20.10+
- Docker Compose v2+
- NVIDIA GPU (recommended) with drivers and Container Toolkit
- 80GB free disk space
- Root/sudo access

### Usage

```bash
# Extract package
tar -xf opentranscribe-offline-v*.tar.xz
cd opentranscribe-offline-v*/

# Run installer
sudo ./install.sh
```

### Installation Process

1. **System validation** - Checks Docker, GPU, disk space
2. **Package verification** - Validates checksums
3. **Image loading** - Loads Docker images (15-30 min)
4. **File installation** - Copies files to `/opt/opentranscribe/`
5. **Model installation** - Copies AI models (10-20 min)
6. **Configuration** - Creates `.env` with auto-detected settings
7. **Permissions** - Sets proper file permissions

### Installation Time

- System validation: 1-2 minutes
- Docker images: 15-30 minutes
- Model installation: 10-20 minutes
- **Total: 30-60 minutes**

### Post-Installation

1. Edit configuration:
   ```bash
   sudo nano /opt/opentranscribe/.env
   ```

2. Set HuggingFace token (REQUIRED):
   ```bash
   HUGGINGFACE_TOKEN=your_token_here
   ```

3. Start services:
   ```bash
   cd /opt/opentranscribe
   sudo ./opentr.sh start
   ```

---

## Offline Management Wrapper

Management script for offline OpenTranscribe installations.

### Location

Installed at: `/opt/opentranscribe/opentr.sh`

### Commands

```bash
# Basic operations
sudo ./opentr.sh start              # Start all services
sudo ./opentr.sh stop               # Stop all services
sudo ./opentr.sh restart            # Restart all services
sudo ./opentr.sh status             # Show status
sudo ./opentr.sh logs [service]     # View logs

# Service management
sudo ./opentr.sh restart-backend    # Restart backend only
sudo ./opentr.sh restart-frontend   # Restart frontend only
sudo ./opentr.sh shell [service]    # Open shell in container

# Maintenance
sudo ./opentr.sh health             # Check service health
sudo ./opentr.sh backup             # Create database backup
sudo ./opentr.sh clean              # Clean Docker resources
```

### Examples

```bash
cd /opt/opentranscribe

# Start and check status
sudo ./opentr.sh start
sudo ./opentr.sh status

# View specific logs
sudo ./opentr.sh logs backend
sudo ./opentr.sh logs celery-worker

# Backup before updates
sudo ./opentr.sh backup

# Restart after config changes
sudo ./opentr.sh restart-backend
```

---

## Model Downloader

Python script for downloading AI models during package building.

### Purpose

Downloads all required AI models for offline packaging:
- WhisperX transcription models
- PyAnnote speaker diarization models

### Usage

**Typically run automatically by build-offline-package.sh**, but can be run manually:

```bash
# Set environment variables
export HUGGINGFACE_TOKEN=your_token_here
export WHISPER_MODEL=large-v3-turbo
export DIARIZATION_MODEL=pyannote/speaker-diarization-community-1

# Run in Docker container (container runs as appuser, not root)
docker run --rm \
    --gpus all \
    -e HUGGINGFACE_TOKEN \
    -e WHISPER_MODEL \
    -e DIARIZATION_MODEL \
    -v ./models/huggingface:/home/appuser/.cache/huggingface \
    -v ./models/torch:/home/appuser/.cache/torch \
    -v ./models/nltk_data:/home/appuser/.cache/nltk_data \
    -v ./models/sentence-transformers:/home/appuser/.cache/sentence-transformers \
    -v ./models/opensearch-ml:/home/appuser/.cache/opensearch-ml \
    -v ./scripts/download-models.py:/app/download-models.py \
    davidamacey/opentranscribe-backend:latest \
    python /app/download-models.py
```

### Output

- Downloads models to cache directories
- Creates `model_manifest.json` with metadata
- Reports total cache size and status

---

## Model Cache Permission Fixer

Script to fix model cache directory permissions when migrating to non-root container user.

### Purpose

OpenTranscribe backend containers now run as a non-root user (`appuser`, UID 1000) for security. Existing installations with model cache owned by root need permission updates.

### When to Use

Run this script if:
- You're upgrading from a version that ran containers as root
- Your model cache directory exists in `./models/` (or custom `MODEL_CACHE_DIR`)
- You see permission errors when starting backend/celery containers

### Prerequisites

One of the following:
- Docker installed and running
- `sudo` access on the host system

### Usage

```bash
# From project root
./scripts/fix-model-permissions.sh

# The script will:
# 1. Read MODEL_CACHE_DIR from .env (or use default ./models)
# 2. Check if directory exists
# 3. Fix ownership to UID:GID 1000:1000
# 4. Set correct permissions (755 for dirs, 644 for files)
```

### How It Works

**Primary Method (Docker):**
```bash
docker run --rm \
  -v ./models:/models \
  busybox:latest \
  chown -R 1000:1000 /models
```

**Fallback Method (sudo):**
```bash
sudo chown -R 1000:1000 ./models
sudo find ./models -type d -exec chmod 755 {} \;
sudo find ./models -type f -exec chmod 644 {} \;
```

### Output

```
OpenTranscribe Model Cache Permission Fixer
==============================================

Model cache directory: /mnt/nvm/repos/transcribe-app/models

Fixing permissions using Docker container...
✓ Permissions fixed successfully!

Migration complete!
Your model cache is now ready for the non-root container.
```

### Verification

After running the script, verify permissions:

```bash
ls -la ./models/
# Should show: drwxr-xr-x ... 1000 1000 ... huggingface
#              drwxr-xr-x ... 1000 1000 ... torch
```

### Fresh Installations

This script is **not needed** for fresh installations. The containers will automatically create the cache directories with correct ownership.

### Related Documentation

- [CLAUDE.md - Security Features](../CLAUDE.md#security-features) - Non-root container documentation
- [Issue #91](https://github.com/davidamacey/transcribe-app/issues/91) - Non-root user implementation

---

## Remote ARM64 Builder Setup

Configure your Mac Studio (or other ARM64 machine) as a remote builder for **10-20x faster ARM64 Docker builds** using native compilation instead of QEMU emulation.

### Why Use This?

Building multi-platform Docker images on x86_64 (Ubuntu/Intel) uses QEMU emulation for ARM64 builds, which is **extremely slow**:
- **Without remote builder**: ARM64 builds take 2-3 hours (QEMU emulation)
- **With remote builder**: ARM64 builds take 8-15 minutes (native Mac Studio)

### How It Works

1. **Ubuntu server** orchestrates the build and handles AMD64 compilation
2. **Mac Studio** handles ARM64 compilation natively via SSH
3. **Docker Buildx** automatically distributes platform-specific work
4. Both platforms build **in parallel** and combine into a single multi-arch image
5. Push to Docker Hub happens from Ubuntu server

### Prerequisites

**Remote machine (Mac Studio):**
- Docker Desktop installed and running
- SSH access configured from Ubuntu server
- SSH key-based authentication (recommended)
- Both machines on the same network

**Ubuntu server:**
- SSH client installed
- Docker with buildx support
- Ability to connect to Mac Studio via SSH

### Quick Start

```bash
# 1. Test SSH connection to your Mac Studio first
ssh user@mac-studio.local "echo 'Connection OK'"

# 2. Run interactive setup
./scripts/setup-remote-builder.sh setup

# 3. Test the configuration
./scripts/setup-remote-builder.sh test

# 4. Use with docker-build-push.sh
USE_REMOTE_BUILDER=true ./scripts/docker-build-push.sh
```

### Commands

```bash
# Interactive setup (recommended)
./scripts/setup-remote-builder.sh setup

# Setup with specific host
./scripts/setup-remote-builder.sh setup --host user@192.168.1.100

# Test connectivity and build capability
./scripts/setup-remote-builder.sh test

# Check current configuration
./scripts/setup-remote-builder.sh status

# Remove remote builder configuration
./scripts/setup-remote-builder.sh remove

# Get help
./scripts/setup-remote-builder.sh help
```

### Setup Process

The interactive setup will:

1. **Prompt for remote host** (e.g., `user@mac-studio.local` or `user@192.168.1.100`)
2. **Test SSH connectivity** to ensure access is working
3. **Verify Docker** is running on the remote machine
4. **Check platform** to confirm ARM64 architecture
5. **Create Docker context** for remote connection
6. **Create multi-node buildx builder**:
   - Node 1 (local): `linux/amd64` builds
   - Node 2 (remote): `linux/arm64` builds
7. **Bootstrap the builder** (starts build containers on both nodes)

### SSH Setup (One-Time)

If you haven't set up SSH key-based authentication:

```bash
# On Ubuntu server, generate SSH key if needed
ssh-keygen -t ed25519 -C "buildx-remote"

# Copy public key to Mac Studio
ssh-copy-id user@mac-studio.local

# Test connection (should not prompt for password)
ssh user@mac-studio.local "echo 'SSH key auth working'"
```

### Using the Remote Builder

Once configured, use the remote builder with `docker-build-push.sh`:

```bash
# Enable remote builder
export USE_REMOTE_BUILDER=true

# Build and push (uses Mac Studio for ARM64)
./scripts/docker-build-push.sh

# Build specific component
./scripts/docker-build-push.sh backend

# Disable remote builder (use QEMU emulation)
export USE_REMOTE_BUILDER=false
./scripts/docker-build-push.sh
```

### Build Process Flow

```
Ubuntu Server (AMD64)
├─ Sends build context to both builders via SSH
├─ Local builder: Compiles linux/amd64 natively (8-15 min)
└─ Remote builder (Mac Studio): Compiles linux/arm64 natively (8-15 min)
    └─ SSH connection to Mac Studio
        └─ Docker builds ARM64 image natively

Both images combined → Multi-arch manifest → Push to Docker Hub
Total time: ~15-30 min (parallel builds)
```

### Performance Comparison

| Build Method | Backend Build Time | Speedup |
|-------------|-------------------|---------|
| QEMU emulation (without remote builder) | 2-3 hours | Baseline |
| Native ARM64 (with remote builder) | 15-30 minutes | **10-20x faster** |

### Verification

After setup, verify the configuration:

```bash
# Check builder nodes
./scripts/setup-remote-builder.sh status

# Output should show:
# Name:   opentranscribe-multiarch
# Nodes:
# - Node 1: local (linux/amd64)
# - Node 2: remote-arm64 (linux/arm64)

# Test a build
./scripts/setup-remote-builder.sh test
```

### Troubleshooting

**"Cannot connect via SSH"**
- Ensure SSH is enabled on Mac Studio
- Verify network connectivity: `ping mac-studio.local`
- Test SSH manually: `ssh user@mac-studio.local "echo test"`
- Set up SSH key authentication: `ssh-copy-id user@mac-studio.local`

**"Docker is not accessible on remote machine"**
- Ensure Docker Desktop is running on Mac Studio
- Verify Docker works locally on Mac Studio: `docker ps`

**"Remote machine is not ARM64"**
- Check architecture on Mac Studio: `uname -m` (should show `arm64`)
- The script will still work but won't provide the speed benefit

**Build fails with "failed to ping builder"**
- The remote builder may be sleeping. Restart builder:
  ```bash
  docker buildx inspect opentranscribe-multiarch --bootstrap
  ```

**"Unknown platform" errors**
- Ensure both machines have buildx support:
  ```bash
  docker buildx version
  ```

### Advanced Configuration

**Custom builder name:**
```bash
./scripts/setup-remote-builder.sh setup --name my-builder
USE_REMOTE_BUILDER=true REMOTE_BUILDER_NAME=my-builder ./scripts/docker-build-push.sh
```

**Using Docker context instead of SSH:**
```bash
# Create Docker context manually
docker context create mac-studio --docker "host=ssh://user@mac-studio.local"

# Then follow normal setup
./scripts/setup-remote-builder.sh setup
```

### Cleanup

To remove the remote builder configuration:

```bash
# Remove builder and context
./scripts/setup-remote-builder.sh remove

# Manually remove if needed
docker buildx rm opentranscribe-multiarch
docker context rm remote-arm64
```

### Related Documentation

- [Docker Buildx documentation](https://docs.docker.com/buildx/working-with-buildx/)
- [docker-build-push.sh](#docker-build--push-script) - Main build script

---

## Docker Build & Push Script

Quick solution for building and pushing Docker images to Docker Hub locally while GitHub Actions handles automated deployments.

### Prerequisites

1. **Docker with buildx support** (Docker Desktop or Docker Engine 19.03+)
2. **Docker Hub account** logged in:
   ```bash
   docker login
   ```

### Usage

#### Basic Usage

```bash
# Build and push both backend and frontend
./scripts/docker-build-push.sh

# Build and push only backend
./scripts/docker-build-push.sh backend

# Build and push only frontend
./scripts/docker-build-push.sh frontend

# Auto-detect changes and build only what changed
./scripts/docker-build-push.sh auto
```

#### Environment Variables

```bash
# Use remote ARM64 builder (10-20x faster!)
USE_REMOTE_BUILDER=true ./scripts/docker-build-push.sh

# Use different Docker Hub username
DOCKERHUB_USERNAME=myusername ./scripts/docker-build-push.sh

# Build for specific platform only (faster for testing)
PLATFORMS=linux/amd64 ./scripts/docker-build-push.sh backend

# Build for multiple platforms (default)
PLATFORMS=linux/amd64,linux/arm64 ./scripts/docker-build-push.sh

# Disable cache for clean builds
NO_CACHE=true ./scripts/docker-build-push.sh

# Skip security scanning for faster iteration
SKIP_SECURITY_SCAN=true ./scripts/docker-build-push.sh
```

### Image Tagging Strategy

Each build creates two tags:
- `latest` - Always points to the most recent build
- `<commit-sha>` - Specific commit for version tracking

**Examples:**
- `davidamacey/opentranscribe-backend:latest`
- `davidamacey/opentranscribe-backend:a1b2c3d`
- `davidamacey/opentranscribe-frontend:latest`
- `davidamacey/opentranscribe-frontend:a1b2c3d`

### Features

✅ **Multi-architecture builds** - Supports both AMD64 and ARM64
✅ **Smart change detection** - Auto mode only builds changed components
✅ **Colored output** - Easy to read build progress
✅ **Automatic Docker Hub login check** - Prompts if not logged in
✅ **Git-based versioning** - Tags images with commit SHA
✅ **Buildx builder management** - Creates and reuses builder instance

### Build Times

**Frontend (~2 minutes):**
- AMD64: ~1 min
- ARM64: ~1 min (QEMU) or ~1 min (native with remote builder)
- Image size: ~51 MB

**Backend (varies significantly):**

| Build Method | AMD64 Time | ARM64 Time | Total Time |
|-------------|-----------|-----------|-----------|
| **QEMU emulation** (default) | ~8-15 min | **2-3 hours** | **2-3 hours** |
| **Remote builder** (recommended) | ~8-15 min | ~8-15 min | **15-30 min** |
| Single platform | ~8-15 min | N/A | ~8-15 min |

- Image size: ~13.8 GB

**⚡ Performance Tip:** Set up a [remote ARM64 builder](#remote-arm64-builder-setup) for **10-20x faster builds**!

### Tips for Faster Builds

1. **Use remote ARM64 builder (FASTEST for multi-platform):**
   ```bash
   # One-time setup
   ./scripts/setup-remote-builder.sh setup

   # Then for all future builds
   USE_REMOTE_BUILDER=true ./scripts/docker-build-push.sh
   ```

2. **Build single platform for testing:**
   ```bash
   PLATFORMS=linux/amd64 ./scripts/docker-build-push.sh backend
   ```

3. **Use auto mode during development:**
   ```bash
   ./scripts/docker-build-push.sh auto
   ```

4. **Build only what you need:**
   ```bash
   # Just changed frontend code
   ./scripts/docker-build-push.sh frontend
   ```

5. **Skip security scanning during iteration:**
   ```bash
   SKIP_SECURITY_SCAN=true ./scripts/docker-build-push.sh backend
   ```

### Workflow Integration

This script complements the GitHub Actions workflow:

- **GitHub Actions**: Automated builds on push to main
- **Local Script**: Quick manual builds and testing
- **Use Cases**:
  - Testing production builds locally
  - Pushing urgent fixes manually
  - Building when GitHub runners have issues
  - Pre-release testing

### Troubleshooting

**Error: "Cannot connect to Docker daemon"**
```bash
# Start Docker
sudo systemctl start docker  # Linux
# or open Docker Desktop on Mac/Windows
```

**Error: "no builder instance found"**
```bash
# The script will auto-create, but you can manually create:
docker buildx create --name opentranscribe-builder --use
docker buildx inspect --bootstrap
```

**Error: "denied: requested access to resource is denied"**
```bash
# Login to Docker Hub
docker login
```

**Build is very slow (ARM64 taking hours)**
```bash
# Option 1: Use remote ARM64 builder (RECOMMENDED - 10-20x faster)
./scripts/setup-remote-builder.sh setup
USE_REMOTE_BUILDER=true ./scripts/docker-build-push.sh

# Option 2: Build for single platform only
PLATFORMS=linux/amd64 ./scripts/docker-build-push.sh backend

# Option 3: Check Docker resources
docker info | grep -i cpu
docker info | grep -i memory
```

### Pulling Images

Users can pull the images using:

```bash
# Latest version
docker pull davidamacey/opentranscribe-backend:latest
docker pull davidamacey/opentranscribe-frontend:latest

# Specific version
docker pull davidamacey/opentranscribe-backend:a1b2c3d
```

### Next Steps

See Issue #81 for the full roadmap including:
- [ ] Self-hosted GitHub Actions runner setup
- [ ] Automated change detection in CI/CD
- [ ] Image size optimization
- [ ] Build cache improvements

### Related Files

- [docker-build-push.sh](./docker-build-push.sh) - Main build script
- [../backend/Dockerfile.prod](../backend/Dockerfile.prod) - Backend production Dockerfile
- [../frontend/Dockerfile.prod](../frontend/Dockerfile.prod) - Frontend production Dockerfile
- [../.github/workflows/docker-publish.yml](../.github/workflows/docker-publish.yml) - GitHub Actions workflow
