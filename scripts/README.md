# OpenTranscribe Scripts

This directory holds the build and release tooling (Docker images, offline packages, model downloads), the test gates, and operational and benchmark helpers. The sections below cover the build/offline/model family in depth; every other script is listed with a one-line purpose in [All other scripts](#all-other-scripts).

## Quick Start

**Run complete build pipeline:**
```bash
# Interactive mode (recommended for first-time use)
./scripts/build-all.sh --interactive

# Direct execution with auto-detected version
./scripts/build-all.sh

# With specific version (names the offline package only, see Complete Build Pipeline)
./scripts/build-all.sh 2.1.0

# Background execution (see the caveat under Complete Build Pipeline)
nohup ./scripts/build-all.sh > build-$(date +%Y%m%d-%H%M%S).log 2>&1 &
```

See [docs/BUILD_PIPELINE.md](../docs/BUILD_PIPELINE.md) for complete documentation.

## Testing & dev-cycle scripts

The test-gate, e2e and release-rehearsal scripts are listed in [All other scripts](#all-other-scripts)
below; [scripts/CLAUDE.md](CLAUDE.md)'s "Which script to reach for" has the long-form detail.

```bash
./scripts/run-dev-tests.sh --full     # quick "does my branch work" check (backend+e2e+frontend,
                                       # one command, one consolidated report; minutes)
./scripts/run-integration-tests.sh    # the pre-merge gate (needs the live dev stack)
./scripts/test-matrix.sh all          # exhaustive deployment-mode rehearsal (dev/prod/lite/PKI/
                                       # GPU-scale/fresh-install/upgrade); run before a release
python3 scripts/check-requirements-resolve.py   # prove every backend requirements file resolves
                                       # (pip --dry-run) and lite/CI match prod; ~5 min, needs network
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
- **[uninstall-offline-package.sh](#uninstall)** - Remove an offline installation (destructive)
- **[All other scripts](#all-other-scripts)** - Tests, release, benchmarks, operations, fixtures

---

## Complete Build Pipeline

Unified script that executes the complete build, security scan, and packaging workflow with optional interactive mode.

### What It Does

Runs two main phases:
1. **Docker Build & Security Scanning** (~30-45 min per the script's own estimate)
   - Runs `docker-build-push.sh all` (backend, lite, frontend, docs; each on the platforms it declares, see `docker-build-push.sh list-platforms`)
   - Pushes to Docker Hub
   - Runs security scans (Trivy, Grype, Syft, Hadolint, Dockle)

2. **Offline Package Creation** (~1.5-2 hrs per the script's own estimate)
   - Runs `build-offline-package.sh <version>`
   - Pulls images from registry
   - Downloads AI models
   - Creates deployment package

**The version argument drives both phases.** `build-all.sh` hands it to `docker-build-push.sh`
(as `VERSION`) for the pushed image tags and to `build-offline-package.sh` for the package name
(`opentranscribe-offline-v<version>`); `2.1.0` and `v2.1.0` are equivalent. With no argument it
uses the repo-root `VERSION` file, else `dev-<git-sha>` (see
[Docker Build & Push Script](#docker-build--push-script)).

**Warning:** this pushes to Docker Hub (`DOCKERHUB_USERNAME`, default `davidamacey`) and, with
`NO_CACHE` defaulting to `true` here, does clean rebuilds. Use `PLATFORMS=linux/amd64` and
`SKIP_SECURITY_SCAN=true` for a cheaper dry run of the plumbing.

### Usage

```bash
# Interactive mode (recommended for first-time use)
./scripts/build-all.sh --interactive

# Direct execution
./scripts/build-all.sh                      # Version defaults to the VERSION file (else dev-<git-sha>)
./scripts/build-all.sh 2.1.0                # Specific version (image tags + offline package name)
./scripts/build-all.sh -i 2.1.0             # Interactive with version

# Background execution (the offline phase still prompts for Whisper/OpenSearch model
# selection; run it interactively unless you have verified those prompts are satisfied)
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
Enter version tag (or press Enter to use git commit SHA): 2.1.0
✅ Version: 2.1.0

Build Configuration
Version:               2.1.0
Docker Hub User:       davidamacey
Estimated Time:        2-3 hours

Start build pipeline? (y/N) y
```

### Prerequisites

- Docker logged in: `docker login`
- HuggingFace token: `export HUGGINGFACE_TOKEN=hf_...` or in `.env`
- 100GB+ free disk space

### Output

- Docker images on Docker Hub (`:latest` and `:vX.Y.Z` from the `VERSION` file/env, as capability legs plus an index; see below)
- Security reports in `./security-reports/`
- Offline package in `./offline-package-build/linux/`

### Documentation

- [docs/BUILD_PIPELINE.md](../docs/BUILD_PIPELINE.md) - Complete documentation

---

## Offline Package Builder

Create complete offline installation packages for air-gapped deployments.

### Prerequisites

**Internet-connected build system:**
- Docker (running) and Docker Compose v2+
- At least 80 GB free disk space (the script warns and asks to continue below that)
- HuggingFace account and token (`HUGGINGFACE_TOKEN`, from the environment or `.env`)
- `tar`, `xz`, `git` installed
- An NVIDIA GPU is used for the model-download container by default (`--gpus all`, or
  `--gpus device=$GPU_DEVICE_ID` when that is set)

### Usage

```bash
# Set your HuggingFace token
export HUGGINGFACE_TOKEN=your_token_here

# Run the builder
./scripts/build-offline-package.sh [version] [--local]

# Example with custom version (no leading "v": the script adds it)
./scripts/build-offline-package.sh 2.0.1

# Without version (uses the git short SHA)
./scripts/build-offline-package.sh

# Use locally built images instead of pulling from Docker Hub
./scripts/build-offline-package.sh 2.0.1 --local
```

The script prompts interactively for the Whisper model and the OpenSearch neural-search models
(helpers in `offline-common.sh`), and for the GPU to use. Set `OT_IMAGE_TAG` to pin which
application image tag is pulled (default `latest`); the packaged `docker-compose.offline.yml` is
rewritten to the same tag. `INCLUDE_PASSWORD_BLOCKLIST=false` skips building the breached-password
list (otherwise a multi-hour, resumable API-driven step).

**Warning:** the script `rm -rf`s `./offline-package-build/linux` at the start of every run.

### What It Does

1. **Validates** commands, Docker and disk space
2. **Pulls** (or, with `--local`, uses) the images:
   - `davidamacey/opentranscribe-backend`, `-frontend` and `-docs` at `${OT_IMAGE_TAG:-latest}`
   - the infrastructure images read from `docker-compose.yml` (postgres, redis, minio, opensearch;
     the pinned versions live there, not here)
3. **Downloads** AI models inside the backend image via `scripts/download-models.py`: Whisper
   model, PyAnnote diarization models, NLTK data, sentence-transformers, OpenSearch ML models,
   the native diarizer (`diar-native`) export, and the password blocklist
4. **Packages** configuration (`docker-compose.yml`, the offline/gpu-scale/diar-native overlays,
   `.env.example`, `frontend/nginx.conf`) and the scripts
5. **Creates** checksums, `package-info.json` and the compressed archive (multi-threaded `xz -9`)

Sizes depend on the model selection (the script's own banner estimates 15-20 GB compressed,
~60 GB uncompressed, and 5-40 GB of models); check `du -sh` on the output. The script asks
whether to compress at the end; answering no leaves only the directory and the per-file
`.sha256`.

### Output

```
offline-package-build/linux/
├── opentranscribe-offline-v{version}/            (uncompressed package directory)
├── opentranscribe-offline-v{version}.tar.xz      (if you chose to compress)
├── opentranscribe-offline-v{version}.tar.xz.sha256   (if compressed)
└── opentranscribe-offline-v{version}.sha256      (per-file checksums; only if you skipped compression)
```

### Package Contents

```
opentranscribe-offline-v{version}/
├── install.sh                          # Installation script (install-offline-package.sh)
├── uninstall.sh                        # uninstall-offline-package.sh
├── opentr-offline.sh                   # Management wrapper (installed as opentr.sh)
├── docker-images/                      # one <image>.tar per image ('/' and ':' become '_'), plus metadata.json
├── models/                             # Pre-downloaded AI models
│   ├── huggingface/  torch/  nltk_data/  sentence-transformers/
│   ├── opensearch-ml/  diar-native/  password-blocklist/
│   └── model_manifest.json
├── config/
│   ├── docker-compose.yml
│   ├── docker-compose.offline.yml
│   ├── docker-compose.gpu-scale.yml
│   ├── docker-compose.diar-native.yml
│   ├── docker-compose.diar-native-gpu.yml
│   └── nginx.conf
├── scripts/
│   └── common.sh
├── .env.example
├── checksums.sha256
├── package-info.json
└── README-OFFLINE.md
```

There is no `database/init_db.sql` in the package: the schema is created by Alembic on backend
startup.

### Verification

```bash
cd offline-package-build/linux
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

- Ubuntu or Debian (the installer warns on any other distribution)
- Docker and Docker Compose v2 (`docker compose`)
- NVIDIA GPU (recommended) with drivers and Container Toolkit; without one it falls back to CPU
- 80GB free disk space (the installer refuses to continue below that)
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

1. **System validation** - Checks Docker, Compose, GPU, disk space
2. **Package verification** - Validates `checksums.sha256`
3. **Image loading** - Loads Docker images
4. **File installation** - Copies files to `/opt/opentranscribe/` (the wrapper becomes `opentr.sh`, plus `uninstall.sh`)
5. **Model installation** - Copies AI models and chowns them to `1000:999`
6. **Configuration** - Creates `.env` from `.env.example` with generated secrets, detected GPU settings and `HF_HUB_OFFLINE=1`
7. **Permissions** - Sets file permissions (`.env` is mode 600)

Image loading and model copying are the slow steps (the installer itself warns the model copy can
take 10-20 minutes).

### Post-Installation

1. Edit configuration:
   ```bash
   sudo nano /opt/opentranscribe/.env
   ```

2. Review the generated settings. No `HUGGINGFACE_TOKEN` is needed on the target system: the
   models are pre-installed and the installer sets `HF_HUB_OFFLINE=1` (the token is only needed on the
   internet-connected machine that builds the package).

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
sudo ./opentr.sh start [--gpu-scale]    # Start all services (--gpu-scale: multi-GPU workers)
sudo ./opentr.sh stop               # Stop all services
sudo ./opentr.sh restart [--gpu-scale]  # Restart all services
sudo ./opentr.sh status             # Show status
sudo ./opentr.sh logs [service]     # View logs

# Service management
sudo ./opentr.sh restart-backend    # Restart backend only
sudo ./opentr.sh restart-frontend   # Restart frontend only
sudo ./opentr.sh shell [service]    # Open shell in container

# Maintenance
sudo ./opentr.sh health             # Check service health
sudo ./opentr.sh backup             # Create database backup
sudo ./opentr.sh clean              # DESTRUCTIVE: `down -v`, removes containers AND volumes (database); model cache kept
```

### Uninstall

`/opt/opentranscribe/uninstall.sh` (source: `uninstall-offline-package.sh`, run with sudo)
removes the installation. It asks for a typed `yes` first, then prompts separately for a backup,
removing volumes, removing images and a Docker system cleanup. **Destructive:** the volume
removal is `docker compose down -v`, and it ends with `rm -rf /opt/opentranscribe` (models
included).

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

Downloads the AI models during package building and for pre-populating a model cache.

### Purpose

`download-models.py` downloads (see its module docstring and `DOWNLOAD_GROUPS`):
- the local Whisper model named by `WHISPER_MODEL` (must be in the local model catalog, default `large-v3-turbo`)
- PyAnnote speaker diarization models
- NLTK data, Sentence-Transformers and OpenSearch neural-search models, a chat reranker, the
  wav2vec2 gender classifier, redaction models (`DOWNLOAD_REDACTION_MODELS`, default true) and the
  native diarizer export

`--only <group>` (repeatable) restricts it to one group. The groups are the keys of
`DOWNLOAD_GROUPS`: `whisperx`, `pyannote`, `nltk`, `sentence-transformers`, `reranker`,
`speaker-attributes`, `opensearch`, `redaction`, `diar-native`.

### Usage

**Typically run automatically by `build-offline-package.sh`.** For a host-side cache, use the
wrapper, which runs `download-models.py` inside the backend image for you:

```bash
./scripts/download-models.sh [model_cache_dir]          # default ./models
WHISPER_MODEL=large-v3-turbo OPENSEARCH_MODELS="all-MiniLM-L6-v2,all-mpnet-base-v2" \
    ./scripts/download-models.sh ./models
DOWNLOAD_ALL_OPENSEARCH_MODELS=true ./scripts/download-models.sh ./models
```

`HUGGINGFACE_TOKEN` is required for every group except `nltk`. To run the Python script by
hand, mirror what `download-models.sh` does:

```bash
export HUGGINGFACE_TOKEN=your_token_here
export WHISPER_MODEL=large-v3-turbo
export DIARIZATION_MODEL=pyannote/speaker-diarization-community-1

# Container runs as appuser (UID 1000), not root
docker run --rm \
    --gpus all \
    -e HUGGINGFACE_TOKEN -e WHISPER_MODEL -e DIARIZATION_MODEL \
    -v ./models/huggingface:/home/appuser/.cache/huggingface \
    -v ./models/torch:/home/appuser/.cache/torch \
    -v ./models/nltk_data:/home/appuser/.cache/nltk_data \
    -v ./models/sentence-transformers:/home/appuser/.cache/sentence-transformers \
    -v ./models/opensearch-ml:/home/appuser/.cache/opensearch-ml \
    -v ./models/diar-native:/models \
    -v ./scripts/download-models.py:/app/download-models.py:ro \
    davidamacey/opentranscribe-backend:latest \
    python /app/download-models.py
```

### Output

- Downloads models to cache directories
- Creates `model_manifest.json` with metadata
- Reports total cache size and status

`download-password-blocklist.sh` is the separate installer for the breached-password list.

---

## Model Cache Permission Fixer

Script to fix model cache directory permissions when migrating to non-root container user.

### Purpose

OpenTranscribe backend containers now run as a non-root user (`appuser`, UID 1000, GID 999) for security. Existing installations with model cache owned by root need permission updates.

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
# 3. Fix ownership to $CONTAINER_UID_GID (default 1000:999)
# 4. Set correct permissions (755 for dirs, 644 for files)
```

### How It Works

**Primary Method (Docker):**
```bash
docker run --rm \
  -v ./models:/models \
  busybox:latest \
  sh -c "chown -R 1000:999 /models && find /models -type d -exec chmod 755 {} \; && find /models -type f -exec chmod 644 {} \;"
```

**Fallback Method (sudo):**
```bash
sudo chown -R 1000:999 ./models
sudo find ./models -type d -exec chmod 755 {} \;
sudo find ./models -type f -exec chmod 644 {} \;
```

### Output

```
OpenTranscribe Model Cache Permission Fixer
==============================================

Model cache directory: <repo>/models

Fixing permissions using Docker container...
✓ Permissions fixed successfully!

Migration complete!
Your model cache is now ready for the non-root container.
```

### Verification

After running the script, verify permissions:

```bash
ls -la ./models/
# Should show: drwxr-xr-x ... 1000 999 ... huggingface
#              drwxr-xr-x ... 1000 999 ... torch
```

### Fresh Installations

This script is **not needed** for fresh installations. The containers will automatically create the cache directories with correct ownership.

### Related Documentation

- [scripts/CLAUDE.md](CLAUDE.md) - why the group is 999 and not 1000 (issue #580)
- `fix-shared-volume-perms.sh` - the equivalent fix for the pipeline's shared named volumes

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

| Build Method | Multi-arch backend build time (historical estimate; only `lite` and the web images still build arm64) | Speedup |
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

Builds the production images and pushes them to Docker Hub from your machine. **For a release use
`./scripts/release.sh` instead** (see [scripts/CLAUDE.md](CLAUDE.md) and
`docs-site/docs/developer-guide/releasing.md`); it drives this script through its `build` and
`publish` stages. `.github/workflows/docker-publish.yml` is retired, so GitHub Actions does not
publish images.

**Warning:** every path except `BUILD_MODE=local` ends in `buildx --push`. `:vX.Y.Z` (and
`:latest`, unless `PUSH_LATEST=false`) reach Docker Hub when the build finishes.

### Prerequisites

1. **Docker with buildx support**
2. **Docker Hub account** logged in:
   ```bash
   docker login
   ```

### Usage

```bash
./scripts/docker-build-push.sh [target]
```

| Target | Builds |
|--------|--------|
| `all` (default) | backend, lite, frontend, docs |
| `backend` | full/CUDA backend (`opentranscribe-backend`) |
| `lite` | CPU backend (`opentranscribe-backend-lite`) |
| `frontend` / `docs` | frontend / docs image |
| `blackwell` | Blackwell arm64 backend; only on request, never by `all`/`auto` |
| `auto` | only components whose files changed in git (a `backend` change builds backend and lite) |
| `scan` | security scan only (pulls the published images; no build) |
| `cleanup` | delete old partial version tags (`vX`, `vX.X`) from Docker Hub |
| `list-platforms` | print `component<TAB>capability<TAB>platforms` and exit (needs no Docker) |
| `help` | usage |

#### Environment Variables

```bash
# Version: VERSION env var, else the repo-root VERSION file, else v0.0.0 (a "v" is added if missing)
VERSION=v1.2.3 ./scripts/docker-build-push.sh all

# Use the remote ARM64 builder (10-20x faster than QEMU for arm64 legs)
USE_REMOTE_BUILDER=true ./scripts/docker-build-push.sh

# Different Docker Hub namespace
DOCKERHUB_USERNAME=myusername ./scripts/docker-build-push.sh

# PLATFORMS is an explicit OVERRIDE, not a default: unset, each component builds only the
# platforms it declares (./scripts/docker-build-push.sh list-platforms)
PLATFORMS=linux/amd64 ./scripts/docker-build-push.sh backend

# Build without cache (default false here; build-all.sh defaults it to true)
NO_CACHE=true ./scripts/docker-build-push.sh

# Skip security scanning for faster iteration
SKIP_SECURITY_SCAN=true ./scripts/docker-build-push.sh
```

Other variables (see `./scripts/docker-build-push.sh help` for the authoritative list):
`BUILD_MODE=local` (host-arch only, `--load`, pushes nothing), `PUSH_LATEST=false`,
`BUILD_CACHE_REGISTRY`, `DRY_RUN=true` (print the buildx invocations and exit),
`REMOTE_BUILDER_NAME`, `FAIL_ON_SECURITY_ISSUES`, `FAIL_ON_CRITICAL`, `PUSH_SECURITY_REPORTS=true`
(git-commits and pushes `security-reports/` to the current branch).

### Image Tagging Strategy

Capability lives in the repository (`opentranscribe-backend` is CUDA,
`opentranscribe-backend-lite` is CPU) and is restated in the tag (issue #680):

- `vX.Y.Z-<cap>-<arch>` legs, for example `vX.Y.Z-cuda-amd64`, `vX.Y.Z-cpu-amd64`, `vX.Y.Z-cpu-arm64`
- `vX.Y.Z`: a multi-arch index assembled from that repository's own legs
- `latest`: a digest copy of the index
- `frontend` and `docs` carry no capability and get a single multi-platform `vX.Y.Z` / `latest`
- a `dev-<git-sha>` VERSION builds a throwaway tag and never moves `latest`

Ask the script for the platform set rather than copying it: `./scripts/docker-build-push.sh list-platforms`.

### Features

- Per-component platform table and capability legs
- Smart change detection (`auto`)
- Automatic Docker Hub login check
- Buildx builder management (reuses `opentranscribe-builder`, or `opentranscribe-multiarch` with `USE_REMOTE_BUILDER=true`)
- Security scanning of every built leg (Hadolint, Dockle, Trivy, Grype, Syft) into `./security-reports/`

### Build Times

arm64 legs under QEMU emulation take hours; with the [remote ARM64 builder](#remote-arm64-builder-setup)
they build natively in minutes. These figures vary a lot by host; the script's own `--help` and the
[Performance Comparison](#performance-comparison) above are the reference.

### Tips for Faster Builds

1. **Use the remote ARM64 builder for any arm64 leg (today: `lite`, `frontend`, `docs`):**
   ```bash
   ./scripts/setup-remote-builder.sh setup     # one-time
   USE_REMOTE_BUILDER=true ./scripts/docker-build-push.sh
   ```
2. **Build a single platform for testing:** `PLATFORMS=linux/amd64 ./scripts/docker-build-push.sh backend`
3. **Use `auto` during development:** `./scripts/docker-build-push.sh auto`
4. **Build only what you need:** `./scripts/docker-build-push.sh frontend`
5. **Skip security scanning during iteration:** `SKIP_SECURITY_SCAN=true ./scripts/docker-build-push.sh backend`
6. **Build without publishing:** `BUILD_MODE=local ./scripts/docker-build-push.sh backend`

### Troubleshooting

**Error: "Cannot connect to Docker daemon"**
```bash
sudo systemctl start docker  # Linux
# or open Docker Desktop on Mac/Windows
```

**Error: "Remote builder 'opentranscribe-multiarch' not found"**
```bash
./scripts/setup-remote-builder.sh setup
# or unset USE_REMOTE_BUILDER to use QEMU emulation
```

**Error: "denied: requested access to resource is denied"**
```bash
docker login
```

**Build is very slow (arm64 taking hours)**
```bash
USE_REMOTE_BUILDER=true ./scripts/docker-build-push.sh      # recommended
PLATFORMS=linux/amd64 ./scripts/docker-build-push.sh backend  # single platform
```

### Pulling Images

```bash
docker pull davidamacey/opentranscribe-backend:latest
docker pull davidamacey/opentranscribe-backend-lite:latest
docker pull davidamacey/opentranscribe-frontend:latest

# A specific release
docker pull davidamacey/opentranscribe-backend:vX.Y.Z
```

### Related Files

- [docker-build-push.sh](./docker-build-push.sh) - Main build script
- [security-scan.sh](./security-scan.sh) - Scanner behind the post-build scan; `list-components` / `list-repos` print its component table
- [../backend/Dockerfile.prod](../backend/Dockerfile.prod) - Backend (CUDA) production Dockerfile
- [../backend/Dockerfile.lite](../backend/Dockerfile.lite) - Backend (CPU) production Dockerfile
- [../frontend/Dockerfile.prod](../frontend/Dockerfile.prod) - Frontend production Dockerfile
- [release.sh](./release.sh) - The release pipeline that normally drives this script

---

## All other scripts

One line each, taken from each script's own header. Read the header (or `--help` where the
script has one) before running anything: many of these write to the live dev stack. Subsystem
detail, rationale and destructive-operation notes are in [scripts/CLAUDE.md](CLAUDE.md).

### Destructive or live-stack-mutating scripts

| Script | What it does |
|--------|--------------|
| `reset-retries.sh [file_uuid]` | `UPDATE media_file SET retry_count = 0` in the `opentranscribe-postgres` container. With no argument it hits every file; no confirmation or dry-run. |
| `fix-database-issues.sql`, `fix-false-error-files.sql` | Manual status-repair SQL. The backend recovery tasks (`backend/app/tasks/recovery.py`) now do this automatically; run only for emergency repair, by hand through `psql`. |
| `comprehensive-database-review.sql` | Read-only duplicate/stuck-task/index/integrity report. |
| `cleanup-test-users.py` | Removes orphaned test users. Dry-run by default; `--execute-unambiguous` or `--execute` deletes. Prints its `Target:` first; check it. |
| `cleanup-test-data.py` | Signature-scoped sweep of orphaned test media, collections, tags, watch sources, speaker profiles, conversations and test stores. Dry-run by default; `--execute-unambiguous` (Tier A) / `--execute` (Tier A + B), `--planes`, `--min-age-minutes`. |
| `cleanup-test-leftovers.sh` | Unattended wrapper around the Tier A sweep; also `./opentr.sh test-cleanup`. Requires `--project` or `--fresh`. |
| `test-model-download.sh` | `rm -rf ./test-model-cache` relative to the current directory. |
| `test-all-auth.sh`, `test-fedramp.sh`, `test-ldap-auth.sh`, `run-auth-e2e.sh` | Auth test drivers against the live stack. `test-all-auth.sh` and `test-fedramp.sh` register users and never clean up (use `cleanup-test-users.py`); `run-auth-e2e.sh` changes admin auth config (restored on an exit trap) and stops the frontend container. |
| `uninstall-offline-package.sh` | `down -v`, `docker rmi`, `rm -rf /opt/opentranscribe` (prompted). |
| `release-tests/*` | Release rehearsals; they delete Docker volumes and require the live stack stopped. Owned by `release-tests/README.md`. |
| `frontend-check.sh` | On failure can invoke the `claude` CLI and `git add` modified frontend files; pass `--no-claude` to avoid that. |
| `push-security-reports.sh` | Git-commits and pushes `security-reports/`. |

### Tests, gates and quality tooling

| Script | Purpose |
|--------|---------|
| `run-integration-tests.sh` | The pre-merge gate: unit/API suite, FIPS pass, integration and gpu markers, schema drift, dependency parity and more, against the live stack. |
| `run-dev-tests.sh` | Chained dev-cycle runner (backend gate, e2e, frontend) with one report; manages test overlays. |
| `run-backend-tests.sh` | Runs the backend suite once and keeps the artifacts; `--summary` only re-reports the last run (no tests run). |
| `run-diarization-gpu-tests.sh` | Runs the container-only `-m gpu` diarization suites on real GPU hardware. |
| `run-mutation-tests.sh` | Opt-in mutation testing of the security-critical modules; `--check-baseline` reads results. |
| `test-matrix.sh` | Dispatcher for the full local deployment-mode test matrix. |
| `validate-deployments.sh` | Checks that every documented deployment permutation yields a valid compose config. |
| `e2e/run-e2e.sh`, `e2e/run-e2e-smoke.sh`, `e2e/update-visual-baselines.sh` | Playwright e2e suite, a smoke subset, and the supported way to refresh visual baselines. See `backend/tests/e2e/README.md`. |
| `frontend-check.sh` | Frontend gate (eslint, i18n parity, svelte-check, vitest, test auditors, vite build). Also the pre-commit hook. |
| `safe-precommit.sh`, `safe-precommit-selftest.sh` | Concurrency guard around `pre-commit`, and its self-test. |
| `mypy-hook-negative-control.sh`, `mypy_negative_control_fixture.py.txt` | Proves the mypy pre-commit hook catches deliberately mistyped code. |
| `audit-tests.py`, `analyze-test-timing.py` | Test-quality auditor and JUnit timing analyser. |
| `audit-route-coverage.py`, `audit-error-disclosure.py`, `audit-session-lifetime.py`, `session-lifetime-allowlist.txt` | Static audits: routes no test references, stored failure text on the wire, DB sessions held across slow work (and its allowlist). |
| `triage-mutants.py`, `mutation-verify-apply.py`, `mutation-baselines.tsv` | Mutation-testing helpers and the ratchet baselines. |
| `check-dependency-parity.sh` | Fails when the venv and container dependency versions diverge. |
| `check-schema-drift.py` | Compares the live database schema with the SQLAlchemy models. |
| `check-lan-hostnames.sh` | Refuses real internal LAN identifiers in tracked files. |
| `check-architecture-diagram-drift.py` | Fails when a rendered architecture diagram no longer matches its spec. |
| `verify_vendored.py` | Verifies or generates `MANIFEST.json` for a vendored copy of repo documents. |
| `update-a11y-baseline.py` | Prints paste-ready lines for `backend/tests/e2e/a11y-allowlist.txt`. |
| `dev-test-auth-config.py` | Reads/writes one boolean `auth_config` row for `run-dev-tests.sh`. |
| `testrun-registry.sh`, `lib/test-data-sweep.sh`, `lib/test_artifact_sweep.py`, `lib/dev-test-overlays.sh`, `lib/diar-native-expected.sh`, `lib/compose-project.sh` | Shared helpers sourced by the runners above (liveness registry, end-of-run sweep, overlay orchestration). |
| `tests/test-publish-platforms.sh`, `tests/test-scan-not-a-pass.sh`, `tests/test-verify-apply-mutant.sh` | Offline tests for the publish-platform table, the scan exit codes and the mutant-apply helper (the first two are pre-commit hooks). |
| `lite-smoke.sh`, `gpu-scale-smoke.sh`, `diar-native-smoke.sh`, `verify-diar-native-e2e.sh` | Smoke checks for `--lite`/`--cpu`, `--gpu-scale` and the native diarizer deployments. |
| `test-download-recovery.sh`, `test-playlist-recovery-fix.sh`, `test-search-indexing-recovery.sh` | Recovery-path verification scripts; need the stack up. |
| `test-watch-e2e.sh`, `setup-watch-source-test-data.sh` | Watch-source end-to-end test and its test-data seeder (needs `--with-watch`). |
| `pki/setup-test-pki.sh`, `pki/generate-test-env.sh`, `pki/test-pki-auth.sh`, `pki/run-pki-e2e-leg.sh`, `pki/nginx-pki-dev.conf` | Test PKI generation, env/overlay generation, curl smoke test, the e2e matrix leg, and the dev nginx config. |
| `mock-llm-server.py`, `mock-asr-server.py` | OpenAI-compatible and Gladia-style mock servers. Start them through `./opentr.sh start dev --with-mock-llm` / `--with-mock-asr`, never by hand. |
| `seed-fresh-deployment.sh` | Uploads a few small media files into a fresh deployment; idempotent per file. |

### Release and publishing

| Script | Purpose |
|--------|---------|
| `release.sh`, `release/NN-*.sh`, `release/*.yaml|py|sh` | The release pipeline and its stage scripts, criteria and helpers. The only supported way to cut a release. |
| `release-rehearse-safe.sh` | Entry point for the rehearsal-only stages (preflight to rehearse); cannot reach tag/publish/promote/finish. |
| `release-tests/` | Fresh-install, upgrade and lite-mode rehearsals; see `release-tests/README.md`. |
| `verify-install-paths.sh` | Proves the checked-out installer can install and update every published release. |
| `security-scan.sh`, `generate-sbom-report.py`, `push-security-reports.sh` | Image scanning (per leg), SBOM report, and publishing the reports. |
| `lib/manifest_platform_check.py`, `lib/sbom_license.py`, `lib/release_notes_body.py` | Helpers used by the publish/scan stages. |
| `generate-openapi.py`, `generate-platform-access-matrix.py`, `generate-pipeline-docs.py`, `generate-roadmap.py`, `generate-architecture-diagrams.sh` | Generated artifacts: OpenAPI snapshot, platform access matrix and pipeline docs (each `--write`/`--check`), roadmap data (`--check`, needs an authenticated `gh`), and architecture diagrams. |
| `create-workflow-gif-0.9s.sh`, `create-workflow-gif-v0.4.0.sh`, `organize-all-screenshots.py` | One-off docs screenshot and GIF helpers. |
| `build-windows-installer.sh`, `offline-common.sh` | Windows installer builder and the shared functions it shares with `build-offline-package.sh`. |

### Operations and data protection

| Script | Purpose |
|--------|---------|
| `speaker-profiles-backup.sh` | `backup`, `restore`, `list` of speaker profiles (PostgreSQL + OpenSearch). |
| `voiceprint-backup.py` | Export / import / verify the OpenSearch voiceprint indices (used by `./opentr.sh backup`/`restore`). |
| `fix-shared-volume-perms.sh` | Repairs ownership of the pipeline's shared named volumes; idempotent. |
| `generate-ssl-cert.sh` | Self-signed certificates for HTTPS access (needed for browser microphone recording). |
| `download-password-blocklist.sh` | Builds the breached-password list under `<model_cache_dir>/password-blocklist/`. |
| `diagnose-file-integrity.sh` | Diagnostic for files stuck or inconsistent in the database. |
| `bulk-processing-cheatsheet.sh` | Cheatsheet of commands for monitoring large batch imports. |
| `apply-pyannote-patch.sh` | Apply/revert PyAnnote optimization patches inside the GPU worker container. |
| `compare-patch-results.sh` | Compare diarization output before and after a patch. |
| `gpu-profile-test.sh` | Reprocesses videos of varied length to record per-stage VRAM (readable via `GET /api/admin/gpu-profiles`). |
| `gpu-device-order-probe.py` | Prints the CUDA ordinal to physical card mapping without touching a GPU. |
| `inject-eval-corpus.sh`, `fetch-rag-eval-data.sh`, `reindex_eval_corpus.py`, `verify-eval-corpus-settled.py`, `rag-eval-manifests/` | RAG evaluation corpus fetch, injection (no ASR), reindex and settle-wait, with corpus manifests. |
| `lib/env_reader.py` | python-dotenv `.env` reader shared by the shell scripts. Dev/CI only; not shipped in the install manifest. |

### Benchmarks, probes and experiments

Mostly accreted one-off experiments; `run_benchmark.py` (via `./opentr.sh bench`) is the maintained
orchestrator. They upload files to the live stack and generally do not delete them.

| Script | Purpose |
|--------|---------|
| `run_benchmark.py`, `collate_benchmark.py`, `compare_baseline.py` | Engine-optimization benchmark orchestrator, master-table collation, and the upload-speed A/B comparison. `run_benchmark.py` runs `docker compose down -v` on project `otbench` and a legacy cleanup aimed at the `transcribe-app` dev project (`--no-cleanup-legacy` suppresses it). |
| `benchmark_e2e.py`, `benchmark_parallel.py`, `benchmark_concurrency_sweep.sh`, `benchmark_concurrent_uploads.py`, `benchmark_upload_baseline.py`, `benchmark_upload_matrix.py`, `benchmark_transcription.sh`, `benchmark-thorough.py`, `benchmark_projection.py` | Pipeline, parallelism, concurrency and upload benchmarks. |
| `benchmark_engine_compare.py`, `benchmark_engine_queue.py`, `benchmark_engine_single.py` | Engine-vs-pipeline regression gate and Engine throughput/latency benchmarks. |
| `benchmark-diarization.py`, `benchmark-pyannote-direct.py`, `benchmark-pyannote-mps.py`, `mps-speaker-compare.py`, `profile-mps-ops.py`, `diarization-der.py`, `diarization-der-compare.py`, `diar-native-der-parity.py`, `diar-native-handoff-ab.py`, `diar-openblas-der-ab.py` | Diarization timing and accuracy (DER) measurements, including Apple Silicon (MPS) variants. |
| `vram-probe-diarization.py`, `vram-probe-diarization-cpu.py`, `vram-probe-diarization-mps.py`, `whole-stack-vram-probe.py` | VRAM characterization harnesses. |
| `onnx_benchmark_providers.py`, `preconvert-onnx-models.py`, `spike-onnx-export-v2.py`, `spike-onnx-seg-der-impact.py` | ONNX conversion and execution-provider experiments. |
| `benchmark_rag.py`, `benchmark_router.py`, `probe_chat_rag.py`, `judge_chat_answers.py`, `compare_probe_arms.py`, `rag_corpus_agreement.py`, `overview_content_oracle.py`, `build_probe_question_set.py`, `build-elitr-question-set.py`, `measure_embedding_window.py`, `verify-embedding-models.py` | RAG retrieval and chat-quality evaluation tooling. |
| `bench-transcript-document.py` | Times DOCX/PDF transcript rendering on a synthetic recording (manual only). |
| `soak_seed_audio.py` | Seeds `benchmark/test_audio/` with a tier-distributed subset of production audio. |
