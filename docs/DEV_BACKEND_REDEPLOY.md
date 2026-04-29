# Dev & Prod: Build & Deploy with GPU Scaling + NAS

Quick reference for deploying OpenTranscribe in development or production mode with GPU scaling and NAS storage.

---

## Dev Mode Deployment (Recommended for Development)

Dev mode volume-mounts the local source code into containers, giving you **hot reload** for both frontend (Vite) and backend (uvicorn `--reload`). No image rebuild needed for code changes — just save the file.

### Compose File Chain (Dev + GPU + NAS)

```
docker-compose.yml              # Base service definitions
docker-compose.override.yml     # Dev overrides: volume mounts, hot reload, Dockerfile.dev for frontend
docker-compose.nas.yml          # NAS/NVMe bind mounts for minio, postgres, opensearch
docker-compose.gpu.yml          # GPU access for default celery-worker
docker-compose.gpu-scale.yml    # GPU-scaled worker (multiple parallel workers on one GPU)
# gpu-split profile             # GPU split workers (gpu-transcribe + gpu-diarize) — in main yml
```

**Key differences from prod:**
- `override.yml` replaces `prod.yml` + `local.yml`
- Source code is volume-mounted (`./backend:/app`, `./frontend:/app`) — no rebuild for code changes
- Frontend uses `Dockerfile.dev` (Vite dev server on port 5173, not nginx)
- Image names: `opentranscribe-backend:latest` / `opentranscribe-frontend:latest` (no `davidamacey/` prefix)
- Backend runs `uvicorn --reload` for automatic Python hot reload

### Full Dev Deployment (All Services)

```bash
# Deploy everything in dev mode with GPU scaling and NAS
docker compose \
  -f docker-compose.yml \
  -f docker-compose.override.yml \
  -f docker-compose.nas.yml \
  -f docker-compose.gpu.yml \
  -f docker-compose.gpu-scale.yml \
  up -d
```

This starts all 14 services. The frontend dev Dockerfile is built automatically on first run.

### Redeploy Backend + Workers Only (Keep Infrastructure Running)

Use this when you need to restart backend/celery without touching postgres, redis, opensearch, minio, or the frontend:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.override.yml \
  -f docker-compose.nas.yml \
  -f docker-compose.gpu.yml \
  -f docker-compose.gpu-scale.yml \
  up -d --no-deps --force-recreate \
  backend celery-beat celery-cpu-worker celery-download-worker \
  celery-embedding-worker celery-nlp-worker flower \
  celery-worker celery-worker-gpu-scaled
```

### Redeploy Frontend Only

If the frontend container needs recreating (e.g., `node_modules` changed, `Dockerfile.dev` changed, or switching from prod to dev mode):

```bash
docker stop opentranscribe-frontend && docker rm opentranscribe-frontend
docker compose \
  -f docker-compose.yml \
  -f docker-compose.override.yml \
  -f docker-compose.nas.yml \
  -f docker-compose.gpu.yml \
  -f docker-compose.gpu-scale.yml \
  up -d --no-deps --force-recreate frontend
```

### When Do You Need to Restart Containers in Dev Mode?

| Change | Action Needed |
|---|---|
| Python source file edited | **Nothing** — uvicorn `--reload` picks it up |
| Svelte/TS source file edited | **Nothing** — Vite hot reload picks it up |
| `.env` variable changed | Restart affected containers |
| `requirements.txt` changed | Rebuild image: `docker compose -f ... build backend` then recreate |
| `package.json` / `package-lock.json` changed | Rebuild frontend: `docker compose -f ... build frontend` then recreate |
| Docker Compose YAML changed | Recreate affected services with `--force-recreate` |
| Switching from prod → dev mode | Stop and remove ALL containers, then `up -d` with dev compose chain |

### Switching from Production to Dev Mode

**CRITICAL**: If containers were previously started with `docker-compose.prod.yml`, you MUST stop and remove them before switching to dev mode. The frontend especially will be running the production nginx build instead of the Vite dev server.

```bash
# Stop and remove ALL opentranscribe containers
docker compose \
  -f docker-compose.yml \
  -f docker-compose.override.yml \
  -f docker-compose.nas.yml \
  -f docker-compose.gpu.yml \
  -f docker-compose.gpu-scale.yml \
  down

# Start fresh in dev mode
docker compose \
  -f docker-compose.yml \
  -f docker-compose.override.yml \
  -f docker-compose.nas.yml \
  -f docker-compose.gpu.yml \
  -f docker-compose.gpu-scale.yml \
  up -d
```

**Symptoms of running prod frontend in dev mode:**
- Minified JS filenames in browser console (e.g., `BMaf-qko.js`)
- 404 errors on `/api/collections/`, `/api/tags/`, `/api/speakers/`
- "Unable to connect to the server" error on the gallery page
- No hot reload when editing `.svelte` or `.ts` files

### Using opentr.sh (Alternative)

```bash
# Dev mode with GPU scaling (NAS auto-detected from .env)
./opentr.sh start dev --gpu-scale
```

---

## Production Deployment

Production mode uses pre-built Docker images (either locally built or from Docker Hub). No source code is mounted — the code is baked into the image.

### Compose File Chain (Prod + GPU + NAS)

```
docker-compose.yml              # Base service definitions
docker-compose.prod.yml         # Production image tags (davidamacey/opentranscribe-*:latest)
docker-compose.local.yml        # pull_policy: never (use local images, not Docker Hub)
docker-compose.nas.yml          # NAS/NVMe bind mounts
docker-compose.gpu.yml          # GPU access for default celery-worker
docker-compose.gpu-scale.yml    # GPU-scaled worker (multiple parallel workers on one GPU)
# gpu-split profile             # GPU split workers — in main yml, activated via COMPOSE_PROFILES=gpu-split
```

**IMPORTANT**: When testing local code changes in prod mode, you MUST:
1. Rebuild the Docker image from local code
2. Include `docker-compose.local.yml` to prevent pulling old images from Docker Hub

### Full Production Deployment

```bash
# 1. Build backend from local code
docker build -t davidamacey/opentranscribe-backend:latest \
  -f backend/Dockerfile.prod backend/

# 2. Build frontend from local code (if frontend changed)
docker build -t davidamacey/opentranscribe-frontend:latest \
  -f frontend/Dockerfile.prod frontend/

# 3. Deploy everything
docker compose \
  -f docker-compose.yml \
  -f docker-compose.prod.yml \
  -f docker-compose.local.yml \
  -f docker-compose.nas.yml \
  -f docker-compose.gpu.yml \
  -f docker-compose.gpu-scale.yml \
  up -d
```

### Redeploy Backend Only (Prod)

```bash
# 1. Build the image
docker build -t davidamacey/opentranscribe-backend:latest \
  -f backend/Dockerfile.prod backend/

# 2. Redeploy all backend containers
docker compose \
  -f docker-compose.yml \
  -f docker-compose.prod.yml \
  -f docker-compose.local.yml \
  -f docker-compose.nas.yml \
  -f docker-compose.gpu.yml \
  -f docker-compose.gpu-scale.yml \
  up -d --no-deps --force-recreate \
  backend celery-beat celery-cpu-worker celery-download-worker \
  celery-embedding-worker celery-nlp-worker flower \
  celery-worker celery-worker-gpu-scaled
```

### Using opentr.sh (Production)

```bash
# Production with GPU scaling, build from local code
./opentr.sh start prod --build --gpu-scale
```

---

## What Each Overlay Does

| Overlay File | Mode | Purpose |
|---|---|---|
| `docker-compose.yml` | Both | Base service definitions (all workers, infrastructure) |
| `docker-compose.override.yml` | Dev | Volume mounts `./backend:/app` and `./frontend:/app`, Vite dev server, uvicorn `--reload` |
| `docker-compose.prod.yml` | Prod | Sets `image: davidamacey/opentranscribe-*:latest` |
| `docker-compose.local.yml` | Prod | Sets `pull_policy: never` — prevents pulling from Docker Hub |
| `docker-compose.nas.yml` | Both | Bind-mounts NAS/NVMe paths for minio, postgres, opensearch data |
| `docker-compose.gpu.yml` | Both | Gives default `celery-worker` access to `GPU_DEVICE_ID` |
| `docker-compose.gpu-scale.yml` | Both | Enables `celery-worker-gpu-scaled` on `GPU_SCALE_DEVICE_ID` |

**CRITICAL**: Missing overlays cause problems:
- Missing `gpu.yml` → default celery-worker runs on **CPU** (extremely slow transcription)
- Missing `nas.yml` → services use Docker volumes instead of NAS/NVMe paths
- Missing `local.yml` (prod only) → Docker pulls old images from Hub instead of using local builds
- Missing `override.yml` (dev) → no hot reload, no volume mounts

### Docker Compose Flags

| Flag | Purpose |
|---|---|
| `--no-deps` | Don't restart dependency services (postgres, redis, opensearch, minio) |
| `--force-recreate` | Recreate containers even if config hasn't changed |
| `-d` | Detached mode (run in background) |

---

## GPU Layout and .env Configuration

Example dual-GPU + LLM setup:

```
GPU 0: NVIDIA RTX A6000 (49GB) → GPU-scaled workers (5 parallel transcription)
GPU 1: RTX 3080 Ti (12GB)      → Default single worker (2 concurrent)
GPU 2: NVIDIA RTX A6000 (49GB) → External LLM (vLLM, not managed by OpenTranscribe)
```

Relevant `.env` settings:

```bash
# Default worker GPU
GPU_DEVICE_ID=1                    # Which GPU for default celery-worker

# GPU scaling
GPU_SCALE_ENABLED=true
GPU_SCALE_DEVICE_ID=0              # Which GPU for scaled workers
GPU_SCALE_WORKERS=5                # Parallel workers on scaled GPU
GPU_SCALE_DEFAULT_WORKER=1         # 1 = keep default worker (dual-GPU), 0 = disable it
GPU_SCALE_MAX_TASKS=500            # Worker process restarts after N tasks (memory safety)
GPU_MAX_TASKS=100000               # Default worker max tasks (effectively never restart)

# Per-GPU batch sizes
GPU_DEFAULT_BATCH_SIZE=12          # Batch size for default worker (smaller GPU)
# GPU-scaled worker auto-detects batch_size=32 for A6000

# Worker concurrency
GPU_DEFAULT_CONCURRENCY=2          # Concurrent tasks on default worker
DOWNLOAD_CONCURRENCY=5             # Concurrent media downloads
NLP_CONCURRENCY=6                  # Concurrent NLP tasks

# NAS/NVMe storage paths
MINIO_NAS_PATH=/mnt/nas/opentranscribe-minio      # Media → NAS (high capacity)
POSTGRES_DATA_PATH=/mnt/nvm/opentranscribe/pg      # Database → NVMe (fast I/O)
OPENSEARCH_DATA_PATH=/mnt/nvm/opentranscribe/os    # Search index → NVMe (fast vectors)

# YouTube download throttling
YOUTUBE_DOWNLOAD_RATE_LIMIT=30/h   # Celery task-level rate limit (enforced by worker)
YOUTUBE_RECOVERY_BATCH_SIZE=3      # Max YouTube retries per health check cycle
```

---

## Services Overview

After a full GPU-scaled + NAS deployment, these containers run:

| Container | Queue | GPU | Purpose |
|---|---|---|---|
| `backend` | — | — | FastAPI API server (uvicorn `--reload` in dev) |
| `frontend` | — | — | Vite dev server (dev) or nginx (prod) |
| `celery-worker` | `gpu` | GPU 1 (3080 Ti) | Default GPU transcription (concurrency=2) |
| `celery-worker-gpu-scaled` | `gpu` | GPU 0 (A6000) | Scaled GPU transcription (concurrency=5) |
| `celery-download-worker` | `download` | — | Media downloads, rate_limit=30/h |
| `celery-nlp-worker` | `nlp` | — | LLM summarization, speaker ID |
| `celery-embedding-worker` | `embedding` | — | Speaker embeddings, vector ops |
| `celery-cpu-worker` | `cpu` | — | Index maintenance, migrations, utilities |
| `celery-beat` | — | — | Periodic task scheduler |
| `flower` | — | — | Celery monitoring dashboard |
| `postgres` | — | — | Database (NVMe via NAS overlay) |
| `redis` | — | — | Message broker + cache |
| `opensearch` | — | — | Full-text + vector search (NVMe via NAS overlay) |
| `minio` | — | — | Object storage (NAS via NAS overlay) |

---

## Post-Deployment Checks

```bash
# Check all containers are healthy
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Image}}" | grep -E 'backend|celery|flower|frontend'

# Verify GPU-scaled worker is using the correct GPU
docker logs transcribe-app-celery-worker-gpu-scaled 2>&1 | grep -m3 "CUDA\|NVIDIA\|Hardware"

# Verify default worker has GPU (not CPU!)
docker logs opentranscribe-celery-worker 2>&1 | grep -m3 "CUDA\|NVIDIA\|Using CPU"
# If you see "Using CPU" → you forgot docker-compose.gpu.yml

# Verify download worker rate limit
docker exec opentranscribe-celery-download-worker python3 -c \
  "from app.tasks.youtube_processing import process_youtube_url_task; print(f'rate_limit={process_youtube_url_task.rate_limit}')"

# Check OpenSearch indices are healthy (no 503s)
wget -q -O- --post-data '{"query":{"match_all":{}},"size":0}' \
  --header 'Content-Type: application/json' 'http://localhost:5180/speakers/_search' | python3 -m json.tool

# Test search API
TOKEN=$(curl -s -X POST 'http://localhost:5174/api/auth/login' \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d 'username=admin@example.com&password=password' | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
curl -s "http://localhost:5174/api/search?q=test&page=1&page_size=5" -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

---

## Troubleshooting

### Frontend shows "Unable to connect" / 404 errors on collections, tags, speakers

**Symptom**: Browser console shows minified JS filenames and 404 errors on `/api/collections/`, `/api/tags/`, `/api/speakers/`.

**Cause**: The frontend container is running a **production build** (nginx) instead of the **dev build** (Vite). This happens when switching from prod to dev mode without removing the old frontend container.

**Fix**: Stop, remove, and recreate the frontend container with the dev compose chain:
```bash
docker stop opentranscribe-frontend && docker rm opentranscribe-frontend
docker compose \
  -f docker-compose.yml \
  -f docker-compose.override.yml \
  -f docker-compose.nas.yml \
  -f docker-compose.gpu.yml \
  -f docker-compose.gpu-scale.yml \
  up -d --no-deps --force-recreate frontend
```

**How to verify**: Check the frontend container image — it should be `opentranscribe-frontend:latest` (dev), NOT `davidamacey/opentranscribe-frontend:latest` (prod):
```bash
docker inspect opentranscribe-frontend --format '{{.Config.Image}}'
```

### Default worker running on CPU

**Symptom**: `docker logs opentranscribe-celery-worker` shows `Using CPU (no GPU acceleration available)`

**Cause**: `docker-compose.gpu.yml` was not included in the compose command.

**Fix**: Stop the worker, redeploy with `gpu.yml` included:
```bash
docker stop opentranscribe-celery-worker && docker rm opentranscribe-celery-worker
docker compose \
  -f docker-compose.yml -f docker-compose.override.yml \
  -f docker-compose.nas.yml -f docker-compose.gpu.yml -f docker-compose.gpu-scale.yml \
  up -d --no-deps celery-worker
```

### OpenSearch speakers index 503 errors

**Symptom**: Logs show `TransportError(503, 'search_phase_execution_exception')` for the speakers index.

**Cause**: Lucene HNSW vector segment file handles go stale after unclean Docker shutdowns. The on-disk data is intact. The backend auto-repairs this at startup, but if the backend started before OpenSearch was ready, it may not have run.

**Fix**: Close and reopen the index:
```bash
wget -q -O- --post-data '' 'http://localhost:5180/speakers/_close'
wget -q -O- --post-data '' 'http://localhost:5180/speakers/_open'

# Verify recovery
wget -q -O- --post-data '{"query":{"match_all":{}},"size":0}' \
  --header 'Content-Type: application/json' 'http://localhost:5180/speakers/_search'
```

### Stale gpu-scaled container from previous deployment

**Symptom**: `transcribe-app-celery-worker-gpu-scaled` shows `unhealthy` and has errors.

**Fix**: Remove and recreate:
```bash
docker stop transcribe-app-celery-worker-gpu-scaled && docker rm transcribe-app-celery-worker-gpu-scaled
docker compose \
  -f docker-compose.yml -f docker-compose.override.yml \
  -f docker-compose.nas.yml -f docker-compose.gpu.yml -f docker-compose.gpu-scale.yml \
  up -d --no-deps celery-worker-gpu-scaled
```

### Full teardown and clean restart (Dev Mode)

```bash
# Stop everything
docker compose \
  -f docker-compose.yml \
  -f docker-compose.override.yml \
  -f docker-compose.nas.yml \
  -f docker-compose.gpu.yml \
  -f docker-compose.gpu-scale.yml \
  down

# Start fresh
docker compose \
  -f docker-compose.yml \
  -f docker-compose.override.yml \
  -f docker-compose.nas.yml \
  -f docker-compose.gpu.yml \
  -f docker-compose.gpu-scale.yml \
  up -d
```

---

## Without GPU Scaling

Drop `gpu-scale.yml`:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.override.yml \
  -f docker-compose.nas.yml \
  -f docker-compose.gpu.yml \
  up -d
```

## Without NAS

Drop `nas.yml`:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.override.yml \
  -f docker-compose.gpu.yml \
  -f docker-compose.gpu-scale.yml \
  up -d
```
