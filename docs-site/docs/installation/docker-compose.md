---
sidebar_position: 1
title: Docker Compose Installation
---

# Docker Compose Installation

This guide covers installing OpenTranscribe using Docker Compose - the recommended method for all deployment scenarios.

## Quick Installation

The fastest way to install OpenTranscribe is using our one-line installer:

```bash
curl -fsSL https://raw.githubusercontent.com/attevon-llc/OpenTranscribe/master/setup-opentranscribe.sh | bash
```

Skip to [Manual Installation](#manual-installation) if you prefer more control.

:::tip[Choose a variant first]
The installer sets up the **full** image by default (local transcription and speaker detection, using a GPU when it finds one). Pass `--cpu` to run the full image on CPU only, or `--lite` for the **lite** image, which has no local transcription or local speaker detection and uses cloud providers for both. [Deployment Variants](./deployment-variants.md) explains the differences and gives the exact commands.
:::

## Prerequisites

### Required Software

**Docker Engine** (v20.10+)
```bash
# Check Docker version
docker --version

# Expected output
Docker version 20.10.x or higher
```

**Docker Compose** (v2.0+)
```bash
# Check Docker Compose version
docker compose version

# Expected output
Docker Compose version v2.x.x or higher
```

:::warning[Important]
Use `docker compose` (with space), not `docker-compose` (with hyphen). OpenTranscribe requires Docker Compose V2.
:::

**Installing Docker:**
- **Linux**: [docs.docker.com/engine/install](https://docs.docker.com/engine/install/)
- **macOS**: [Docker Desktop for Mac](https://docs.docker.com/desktop/install/mac-install/)
- **Windows**: [Docker Desktop for Windows](https://docs.docker.com/desktop/install/windows-install/) with WSL2

### System Requirements

See [Hardware Requirements](./hardware-requirements.md) for detailed specs.

**Minimum:**
- 8GB RAM
- 4 CPU cores
- 50GB disk space
- Internet connection (for initial setup)

**Recommended:**
- 16GB+ RAM
- 8+ CPU cores
- 100GB+ SSD storage
- NVIDIA GPU with 8GB+ VRAM

### Optional: NVIDIA GPU

For GPU acceleration with the full image (recommended; the lite image never uses a GPU):

1. **NVIDIA GPU** with CUDA support (GTX 1060 or better)
2. **NVIDIA Driver** (version 470.x or higher)
3. **NVIDIA Container Toolkit** installed

See [GPU Setup Guide](./gpu-setup.md) for installation instructions.

## Manual Installation

If you prefer manual installation or need more control:

### Step 1: Clone Repository (Development)

For **development from source**:

```bash
# Clone the repository
git clone https://github.com/attevon-llc/OpenTranscribe.git
cd OpenTranscribe

# Make utility script executable
chmod +x opentr.sh
```

### Step 2: Download Production Files (Deployment)

For **production deployment with Docker Hub images**, keep the base file and the production overlay under their own names. `docker-compose.prod.yml` only adds image names and volumes on top of the service definitions in `docker-compose.yml`, so it cannot be renamed over the base file:

```bash
# Create directory
mkdir opentranscribe && cd opentranscribe

# Download the base file, the production overlay, the management script and the env template
BASE=https://raw.githubusercontent.com/attevon-llc/OpenTranscribe/master
for f in docker-compose.yml docker-compose.prod.yml opentranscribe.sh .env.example; do
  curl -fsSLO "$BASE/$f"
done
chmod +x opentranscribe.sh
```

`opentranscribe.sh` also layers optional overlays (GPU, Blackwell, lite, diar-native, NGINX, backup) when it finds them next to the base file, along with the helper scripts under `scripts/`. The full file list is `release-manifest.txt` in the repository; the one-line installer downloads all of it, which is why it is the recommended route.

### Step 3: Configure Environment

```bash
# Copy environment template
cp .env.example .env

# Edit configuration
nano .env  # or use your preferred editor
```

**Key variables to configure:**

```bash
# Security (REQUIRED - replace every CHANGE_ME in .env.example; the installer
# generates these for you). Production refuses to boot on a placeholder
# JWT_SECRET_KEY or an unset REDIS_PASSWORD.
JWT_SECRET_KEY=your-jwt-secret-key
ENCRYPTION_KEY=your-encryption-key     # decrypts DB-stored secrets: never change after first use
REDIS_PASSWORD=strong-redis-password
OPENSEARCH_PASSWORD=strong-opensearch-password
FLOWER_PASSWORD=strong-flower-password
MINIO_KMS_SECRET_KEY=opentranscribe-key:<base64-32-bytes>   # encryption at rest; generate with: echo "opentranscribe-key:$(openssl rand -base64 32)"

# Generate secrets with
openssl rand -hex 32

# HuggingFace Token (needed for speaker detection on this server; see Step 4)
HUGGINGFACE_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Database (use strong passwords in production)
POSTGRES_PASSWORD=strong-password-here

# Storage
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=strong-minio-password

# GPU Settings (auto-detected by installer)
USE_GPU=true                  # not in .env.example (default: auto); false for CPU-only
WHISPER_MODEL=large-v3-turbo  # large-v3-turbo, large-v3, medium, base
COMPUTE_TYPE=auto             # auto (default), float16, float32 or int8
BATCH_SIZE=auto               # auto (default) or an integer

# Model Cache (where AI models are stored)
MODEL_CACHE_DIR=./models
```

See [Environment Variables](../configuration/environment-variables.md) for all options.

**Optional: Configure Authentication**

OpenTranscribe supports multiple authentication methods. For enterprise deployments, you may want to configure:

- **LDAP/Active Directory** - `LDAP_ENABLED=true`
- **OpenID Connect** - `OIDC_ENABLED=true` (any conforming provider; `KEYCLOAK_*` names still work)
- **PKI/X.509** - `PKI_ENABLED=true`

See [Authentication Overview](../authentication/overview.md) for setup guides.

### Step 4: Set Up HuggingFace

**This step is required for speaker detection on this server** (the full image's default). Speakers detected by a cloud provider (a transcription model that returns speakers, or the pyannote.ai cloud service) do not use this token. The diar-native sidecar's models (speaker detection and voiceprints) are exported on this server from the same gated repository, so a lite install that runs the sidecar for voiceprints needs the token too.

1. **Get a token** at [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens)
2. **Accept the model agreement** for [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1) — the only repo OpenTranscribe actually gates on
3. **Add to .env**: `HUGGINGFACE_TOKEN=hf_xxxxxxxxxxxx`

See [HuggingFace Setup](./huggingface-setup.md) for detailed instructions.

### Step 5: Download AI Models (Optional but Recommended)

Pre-download models to avoid first-use delays:

```bash
./opentranscribe.sh download-models     # production install
bash scripts/download-models.sh         # source checkout
```

Either fetches the whole set (transcription, speaker detection, gender classifier,
NLTK, embeddings, chat reranker, neural search, and content redaction) using the
**image version your deployment is pinned to**. This downloads several GB of AI
models. The exact total depends on which `WHISPER_MODEL` you selected and how
many neural-search models you enable — see
[HuggingFace setup](./huggingface-setup.md).

### Step 6: Start OpenTranscribe

**For Development (source code):**
```bash
./opentr.sh start dev
```

**For Production (Docker Hub images):**
```bash
./opentranscribe.sh start
```

Use the script rather than a bare `docker compose up -d`: it assembles the right chain of compose files (base, production overlay, then GPU, lite, diar-native, NGINX and backup overlays as they apply). `./opentranscribe.sh compose-files` prints the chain it chose.

### Step 7: Verify Installation

Check that all services are running:

```bash
# Check service status
./opentranscribe.sh status

# Check service health
./opentranscribe.sh health
```

Every container should show `Up`, and those with a health check should report `(healthy)`: `opentranscribe-postgres`, `-redis`, `-minio`, `-opensearch`, `-backend`, `-celery-worker` and the other `-celery-*` workers, `-flower`, `-frontend` and `-docs`. A deployment that runs the native diarization sidecar (the default for the full image) also runs a `diar-native` container.

Check logs for any errors:

```bash
# View all logs
./opentranscribe.sh logs

# View specific service logs
./opentranscribe.sh logs backend
./opentranscribe.sh logs celery-worker
```

### Step 8: Access the Application

Open your browser and navigate to:

- **Web Interface**: http://localhost:5173 (`FRONTEND_PORT`)
- **API Documentation**: http://localhost:5174/api/docs (`BACKEND_PORT`; off on hardened/production deployments unless `ENABLE_API_DOCS=true`)
- **Task Monitor** (Flower): http://localhost:5175/flower (`FLOWER_PORT`; login `FLOWER_USER` / `FLOWER_PASSWORD`)
- **MinIO Console**: http://localhost:5179 (`MINIO_CONSOLE_PORT`, loopback only)
- **OpenSearch**: http://localhost:5180 (`OPENSEARCH_PORT`, loopback only)

PostgreSQL (5176), Redis (5177) and the MinIO API (5178) are also published on loopback only.

## Docker Compose Structure

OpenTranscribe uses a base + override pattern:

### File Structure

```
docker-compose.yml          # Base configuration (all environments)
docker-compose.override.yml # Development overrides (auto-loaded; source checkout only)
docker-compose.prod.yml     # Production overrides (Docker Hub images)
docker-compose.gpu.yml      # NVIDIA GPU reservation (docker-compose.blackwell.yml for SM_12x cards)
docker-compose.lite.yml     # Lite (CPU-only, cloud ASR) images
docker-compose.diar-native.yml # Native diarization sidecar (+ -gpu.yml for its GPU reservation)
docker-compose.nginx.yml    # NGINX reverse proxy / HTTPS
docker-compose.offline.yml  # Offline/airgapped overrides
docker-compose.gpu-scale.yml # Multi-GPU scaling overrides
docker-compose.gpu-split.yml # Transcription and diarization on separate GPUs
docker-compose.backup.yml   # Scheduled in-app backups
```

In a production install `./opentranscribe.sh` picks the overlays for you. `./opentr.sh` does the same for a source checkout.

### Usage

**Development** (with hot reload, from a source checkout):
```bash
./opentr.sh start dev
```

**Production** (with Docker Hub images):
```bash
./opentranscribe.sh start
```

**Offline** (airgapped environment): see [Offline Installation](./offline-installation.md).

**Multi-GPU** (parallel processing): see [Multi-GPU Scaling](../configuration/multi-gpu-scaling.md). From a source checkout it is `./opentr.sh start dev --gpu-scale`.

`./opentr.sh` and `./opentranscribe.sh` build the compose file chain for you. Assembling it by hand with `docker compose -f ...` silently drops overlays (for example the GPU or diar-native ones), so only do that after checking `./opentranscribe.sh compose-files`.

## Service Architecture

OpenTranscribe runs as a set of Docker services. The names below are the compose service names; the containers are `opentranscribe-<service>`.

### Core Services

**backend** (FastAPI)
- REST API server
- WebSocket support
- User authentication
- File management
- Host port: 5174 (`BACKEND_PORT`; 8080 inside the container)

**frontend** (Svelte + Vite in dev, NGINX in production)
- Svelte/TypeScript SPA
- Progressive Web App
- Host port: 5173 (`FRONTEND_PORT`)

**docs**
- The embedded documentation site, proxied at `/docs/` in the app
- Host port: 5183 (`DOCS_PORT`)

**postgres** (PostgreSQL 17)
- Main database
- User data, transcriptions, speakers
- Host port: 5176 (`POSTGRES_PORT`, loopback only)

**redis** (Redis 8)
- Celery message broker
- Task queue coordination
- Host port: 5177 (`REDIS_PORT`, loopback only)

### Storage Services

**minio** (MinIO)
- S3-compatible object storage
- Media files, audio, waveforms
- Host ports: 5178 (API, `MINIO_PORT`), 5179 (Console, `MINIO_CONSOLE_PORT`), both loopback only

**opensearch** (OpenSearch 3.4.0)
- Full-text search
- Vector search for semantic search (neural search with ML Commons)
- Alias-based speaker indices for zero-downtime upgrades
- Host ports: 5180 (`OPENSEARCH_PORT`), 5181 (`OPENSEARCH_ADMIN_PORT`), both loopback only

### Worker Services

All workers run the backend image and differ by the queues they consume.

- **celery-worker** - GPU queue: transcription and local speaker detection (3-stage pipeline: preprocess, transcribe, postprocess)
- **celery-cpu-worker** - CPU, utility and CPU-transcribe queues
- **celery-nlp-worker** - NLP (summaries, topics) queue
- **celery-embedding-worker** - search indexing and embeddings
- **celery-download-worker** - media URL downloads
- **celery-cloud-asr-worker** - cloud ASR provider calls
- **celery-redaction** - content redaction
- **celery-beat** - scheduled tasks
- **diar-native** - the native diarization sidecar (the default speaker-detection engine of the full image), added by `docker-compose.diar-native.yml`

The multi-GPU profiles add `celery-worker-gpu-scaled` (`--gpu-scale`) or `celery-worker-gpu-transcribe` / `celery-worker-gpu-diarize` (GPU split).

**flower** (Celery monitoring)
- Industry-standard task monitoring with persistent history
- Worker status, queue visibility, task inspection
- Basic auth protection (configured via `FLOWER_USER`/`FLOWER_PASSWORD`)
- Host port: 5175 (`FLOWER_PORT`, loopback only; 5555 inside the container)

## Management Commands

### Starting and Stopping

```bash
# Start all services
./opentranscribe.sh start

# Stop all services
./opentranscribe.sh stop

# Restart all services
./opentranscribe.sh restart

# Stop and remove volumes (deletes data!)
./opentranscribe.sh clean
```

### Viewing Logs

```bash
# View logs for all services, or one service
./opentranscribe.sh logs
./opentranscribe.sh logs backend
./opentranscribe.sh logs celery-worker
```

### Accessing Containers

```bash
# Open a shell in a container (backend by default)
./opentranscribe.sh shell
./opentranscribe.sh shell postgres

# Access the database (inside the postgres shell)
psql -U postgres -d opentranscribe

# Check worker status (inside the celery-worker shell)
celery -A app.core.celery inspect active
```

### Updating

```bash
# Back up the database first
./opentranscribe.sh backup

# Pull the latest images and restart (production deployment)
./opentranscribe.sh update

# Rebuild images from source (development)
./opentr.sh build
```

## Advanced Configuration

### Custom Ports

Set the host ports in `.env`; the compose files already read them:

```bash
FRONTEND_PORT=5173
BACKEND_PORT=5174
FLOWER_PORT=5175
POSTGRES_PORT=5176
REDIS_PORT=5177
MINIO_PORT=5178
MINIO_CONSOLE_PORT=5179
OPENSEARCH_PORT=5180
OPENSEARCH_ADMIN_PORT=5181
DOCS_PORT=5183
```

Everything except the frontend, backend and docs ports is published on `127.0.0.1` only. `BACKEND_BIND_HOST` (default `0.0.0.0`) controls which interface the API port binds to.

### Resource Limits

```yaml
services:
  celery-worker:
    deploy:
      resources:
        limits:
          cpus: '4'
          memory: 16G
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
```

### Persistent Data

Data is persisted in Docker volumes (`postgres_data`, `minio_data`, `redis_data`, `opensearch_data`, `flower_data` and others, prefixed with the compose project name, normally `opentranscribe_`):

```bash
# List volumes
docker volume ls | grep opentranscribe

# Back up the database (a logical dump, not a volume copy)
./opentranscribe.sh backup
./opentranscribe.sh backup --encrypt   # GPG AES-256, no plaintext on disk

# Restore it (replaces the current database)
./opentranscribe.sh restore ./backups/<backup-file>
```

Copying a live PostgreSQL volume with `tar` can capture an inconsistent state, so use `backup` and `restore` for the database. Media lives in the `minio_data` volume and the search index in `opensearch_data`.

## Troubleshooting

### Services Won't Start

```bash
# Check Docker is running
systemctl status docker

# Check Docker Compose version
docker compose version

# Check for port conflicts
ss -tulpn | grep -E '5173|5174|5175|5176|5177|5178|5179|5180'

# Check logs
./opentranscribe.sh logs
```

### Out of Memory

```bash
# Check Docker resources
docker stats

# Reduce model size in .env
WHISPER_MODEL=medium  # or base
BATCH_SIZE=8          # or lower

# Increase Docker memory limit (Docker Desktop)
# Docker Desktop → Settings → Resources → Memory
```

### GPU Not Working

```bash
# Check NVIDIA driver
nvidia-smi

# Check NVIDIA Container Toolkit
docker run --rm --gpus all nvidia/cuda:12.2.0-base-ubuntu22.04 nvidia-smi

# Check GPU is visible to the worker container
./opentranscribe.sh shell celery-worker
nvidia-smi
```

### Permission Errors

```bash
# Fix model cache permissions
./scripts/fix-model-permissions.sh

# Or manually
sudo chown -R 1000:999 ./models
```

## HTTPS/SSL Setup

HTTPS is required for browser microphone recording when accessing from devices other than localhost (browser security requirement).

### Quick Setup

```bash
# Run the SSL setup command
./opentranscribe.sh setup-ssl
```

This interactive command will:
1. Prompt for your hostname (e.g., `opentranscribe.local`)
2. Generate self-signed SSL certificates
3. Configure NGINX reverse proxy
4. Update your `.env` file

### DNS Configuration

After SSL setup, configure DNS:

**Router DNS:** Add a DNS entry pointing your hostname to your server's IP.

**Local hosts file:** Add to `/etc/hosts` (Linux/Mac) or `C:\Windows\System32\drivers\etc\hosts` (Windows):
```
192.168.1.100  opentranscribe.local
```

### Trust the Certificate

Import `nginx/ssl/server.crt` on each client device:

- **Windows:** Double-click → Install Certificate → Trusted Root CAs
- **macOS:** Double-click → Keychain → Always Trust
- **Linux Chrome:** `chrome://settings/certificates` → Import
- **iOS:** Install profile → Settings → Certificate Trust Settings
- **Android:** Settings → Security → Install certificate

### Restart with HTTPS

```bash
./opentranscribe.sh restart
```

Access at `https://your-hostname`. All services are available through the reverse proxy:
- Frontend: `https://your-hostname`
- API: `https://your-hostname/api`
- Flower: `https://your-hostname/flower/`
- MinIO Console: `https://your-hostname/minio/`

See [NGINX Setup Guide](/docs/configuration/nginx-setup) for advanced configuration including Let's Encrypt certificates.

## Updating OpenTranscribe

### Check for Updates

```bash
./opentranscribe.sh version
```

Shows current version and checks GitHub for newer releases.

### Update Containers Only

```bash
./opentranscribe.sh update
```

Pulls latest Docker images and restarts services. Fast update when only containers changed.

Run `./opentranscribe.sh backup` first. To move to a specific release, or return to the previous one, use `./opentranscribe.sh update --version vX.Y.Z` or `./opentranscribe.sh update --rollback`. The database migration chain is one-way: rolling the images back does not revert the database, so restore a pre-upgrade backup if the older release cannot read the newer schema.

### Full Update (Recommended)

```bash
./opentranscribe.sh update-full
```

Updates everything:
- Docker container images
- Docker Compose configuration files
- NGINX configuration
- Management and helper scripts

**Preserved during updates:**
- `.env` configuration
- SSL certificates
- Database and transcriptions
- Model cache

## Next Steps

- **[Hardware Requirements](./hardware-requirements.md)** - Optimize for your hardware
- **[GPU Setup](./gpu-setup.md)** - Enable GPU acceleration
- **[Configuration](../configuration/environment-variables.md)** - Customize settings
- **[Troubleshooting](./troubleshooting.md)** - Resolve common issues

## Getting Help

- **[FAQ](../faq.md)** - Common questions
- **[GitHub Issues](https://github.com/attevon-llc/OpenTranscribe/issues)** - Report bugs
- **[GitHub Discussions](https://github.com/attevon-llc/OpenTranscribe/discussions)** - Ask questions
