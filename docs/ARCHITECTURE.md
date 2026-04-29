# Architecture Overview
<!-- Updated for v0.4.0 -->

This document explains OpenTranscribe's architecture, the reasoning behind key design decisions, and how this approach compares to other open-source applications and industry patterns.

## System Architecture

OpenTranscribe uses a **distributed microservices architecture** orchestrated with Docker Compose. Each service runs in its own container with defined responsibilities, health checks, and resource constraints.

```
                                    +-------------------+
                                    |     Frontend      |
                                    |  (Svelte/TypeScript)|
                                    +--------+----------+
                                             |
                                             v
+----------------+              +-------------------+              +------------------+
|   PostgreSQL   |<------------>|     Backend       |<------------>|    OpenSearch     |
|  (Database)    |              |    (FastAPI)      |              | (Full-text +      |
+----------------+              +--------+----------+              |  Vector Search)   |
                                         |                        +------------------+
+----------------+              +--------v----------+
|     MinIO      |<------------>|      Redis        |
|  (Object       |              |  (Broker + Cache) |
|   Storage)     |              +--------+----------+
+----------------+                       |
                          +--------------+--------------+
                          |              |              |
                    +-----v----+  +------v-----+  +----v-------+
                    |   GPU    |  |  Download   |  |    CPU     |
                    |  Worker  |  |   Worker    |  |   Worker   |
                    +----------+  +------------+  +------------+
                          |
                    +-----v----+  +------------+  +------------+
                    |   NLP    |  |  Embedding  |  |   Celery   |
                    |  Worker  |  |   Worker    |  |    Beat    |
                    +----------+  +------------+  +------------+
```

### Services

| Service | Container | Purpose | Key Tech |
|---------|-----------|---------|----------|
| **Frontend** | `opentranscribe-frontend` | SPA with PWA support | Svelte, TypeScript, Vite |
| **Backend** | `opentranscribe-backend` | REST API, WebSockets, migrations | FastAPI, SQLAlchemy, Alembic |
| **Docs** | `opentranscribe-docs` | Embedded documentation site, proxied at `/docs/` | Docusaurus, NGINX |
| **PostgreSQL** | `opentranscribe-postgres` | Relational data (users, transcripts, speakers) | PostgreSQL 17 |
| **MinIO** | `opentranscribe-minio` | S3-compatible object storage for media files | MinIO |
| **Redis** | `opentranscribe-redis` | Celery broker, caching, pub/sub | Redis 8 |
| **OpenSearch** | `opentranscribe-opensearch` | Full-text search, vector/neural search, ML Commons embeddings | OpenSearch 3.4.0 |
| **Flower** | `opentranscribe-flower` | Celery task monitoring dashboard | Flower |

## Task Queue Architecture

OpenTranscribe uses **Celery with Redis** as its distributed task queue. This is the core of the background processing system — every AI operation (transcription, diarization, summarization) runs as a Celery task on a specialized worker.

### Why a Distributed Task Queue?

1. **GPU isolation** — AI models (WhisperX, PyAnnote) require GPU access and ~2.5GB of VRAM. Isolating them in a dedicated worker container means the web API never competes for GPU resources and a GPU crash doesn't take down the server.

2. **Failure isolation** — If a transcription OOMs or crashes, only the worker process dies. The API, database, and all other tasks are unaffected. Celery automatically restarts the worker.

3. **Independent scaling** — Different workloads have different resource profiles. GPU tasks are VRAM-bound, downloads are network-bound, CPU tasks are compute-bound. Separate workers allow tuning concurrency for each.

4. **Workload separation** — A long transcription job shouldn't block a quick search reindex. Dedicated queues ensure fast tasks aren't stuck behind slow ones.

### Specialized Worker Queues

OpenTranscribe splits work across 6 specialized Celery workers (plus a scheduler), each consuming from dedicated queues:

| Worker | Queue(s) | Concurrency | Pool | Purpose |
|--------|----------|-------------|------|---------|
| **GPU Worker** | `gpu` | 1 (sequential) | `threads` | WhisperX transcription + PyAnnote diarization. Shared model in VRAM. |
| **Download Worker** | `download` | 3 | `prefork` | Media downloads (yt-dlp, URL fetching). Network-bound, no GPU needed. |
| **CPU Worker** | `cpu`, `utility` | 8 | `prefork` | Post-processing, reindexing, maintenance. Pure CPU work. |
| **NLP Worker** | `nlp`, `celery` | 4 | `prefork` | LLM summarization, speaker identification. External API calls. |
| **Embedding Worker** | `embedding` | 1 (sequential) | `threads` | Sentence-transformer embedding generation. Keeps model in memory. |
| **Utility Worker** | `utility` | 4 | `prefork` | File retention cleanup, periodic maintenance tasks. |
| **Beat Scheduler** | _(scheduler)_ | N/A | N/A | Periodic tasks (health checks, cleanup). No task execution. |

**Design rationale for concurrency settings:**
- **Threads pool** (GPU, Embedding): Workers use the `threads` pool (default as of v0.4.0) so all threads within a process share a single loaded model. This avoids model reloads between tasks and reduces VRAM usage compared to the `prefork` model-per-process approach.
- **Prefork pool** (Download, CPU, NLP, Utility): These are I/O-bound or CPU-bound without large model requirements. Higher concurrency increases throughput.

**Unified 3-stage pipeline (v0.4.0):** Each transcription job runs as a Celery chain: `preprocess` → `gpu` → `postprocess`. The preprocess stage (audio extraction, format normalization) and postprocess stage (indexing, embedding, notifications) run on CPU workers, freeing the GPU worker to focus exclusively on AI inference.

### Multi-GPU Options

Two independent GPU scaling modes are available:

**GPU Scale** (`--gpu-scale`) — multiple parallel pipelines on one GPU:

```bash
# .env configuration
GPU_SCALE_ENABLED=true
GPU_SCALE_DEVICE_ID=2      # Target GPU
GPU_SCALE_WORKERS=4        # Parallel workers within one container

./opentr.sh start dev --gpu-scale
```

Creates a single container with multiple Celery worker threads sharing one GPU (`threads` pool). All threads share the loaded model — no reload overhead. Tasks route via the `gpu` queue as normal.

**GPU Split** (`--with-gpu-split`) — transcription and diarization on separate GPUs:

```bash
# .env configuration
GPU_TRANSCRIBE_DEVICE_ID=0   # GPU for WhisperX (Whisper model)
GPU_DIARIZE_DEVICE_ID=1      # GPU for PyAnnote (diarization model)
ENGINE_SHARED_VOLUME_PATH=/tmp/transcription

./opentr.sh start dev --with-gpu-split
```

Starts two dedicated workers: `celery-worker-gpu-transcribe` (queue `gpu-transcribe`) and `celery-worker-gpu-diarize` (queue `gpu-diarize`). A single file's pipeline flows: CPU preprocess → GPU A (Whisper) → GPU B (PyAnnote) → CPU finalize. The shared `transcription-temp` volume carries the decoded WAV between containers. Workers are defined in `docker-compose.yml` under the `gpu-split` profile — no extra overlay file needed.

### Task Flow Example

A typical file upload follows this path (unified 3-stage Celery chain, v0.4.0):

```
User uploads file
  → Backend API receives file, stores in MinIO
  → Creates database record (PostgreSQL)
  → Dispatches Celery chain to workers

  Stage 1 — Preprocess (CPU Worker):
      1. Extracts and normalizes audio from MinIO
      2. Validates format and duration

  Stage 2 — GPU (GPU Worker):
      3. Runs WhisperX transcription with native word-level timestamps (DTW)
      4. Runs PyAnnote diarization (256-dim WeSpeaker embeddings)
      5. Stores transcript segments + speaker data in PostgreSQL

  Stage 3 — Postprocess (CPU Worker):
      6. Indexes transcript → OpenSearch (full-text + semantic)
      7. Dispatches embedding task → Embedding Worker → OpenSearch
      8. Sends WebSocket notification → Frontend updates
```

### Monitoring

**Flower** provides a real-time web dashboard for the task queue:
- Active/completed/failed task counts per worker
- Task execution times and retry history
- Worker status and resource utilization
- Queue depths and throughput

Access at `http://localhost:5175/flower` (default credentials: `admin` / `flower`).

## How Other Applications Handle This

OpenTranscribe's architecture follows established patterns used by major open-source projects. Here's how it compares:

### Similar Architecture (Multi-Container, Task Queue)

| Application | Stars | Task Queue | Workers | Notes |
|-------------|-------|------------|---------|-------|
| **Immich** (photos) | 60k+ | BullMQ + Redis | Separate ML container | Node.js equivalent of Celery. ML container handles face detection, CLIP embeddings. Very similar to OpenTranscribe. |
| **Paperless-ngx** (documents) | 25k+ | Celery + Redis | Separate Celery worker | Same stack as OpenTranscribe. Worker handles OCR and classification. |
| **Authentik** (identity) | 15k+ | Celery + Redis | Separate Celery worker | Same stack. Worker handles email, sync, and background operations. |
| **Sentry** (error tracking) | 40k+ | Celery + Redis/Kafka | Multiple worker types | Production-grade Celery deployment at massive scale. |

**Takeaway:** For web applications with heavy background processing, Celery + Redis (Python) or BullMQ + Redis (Node.js) with separate worker containers is the industry standard.

### Single Container / Single Binary

| Application | Stars | Background Tasks | Why It Works |
|-------------|-------|------------------|--------------|
| **Jellyfin** (media) | 40k+ | Child processes (ffmpeg) | Transcoding is "spawn ffmpeg process." No ML models to keep warm. |
| **Gitea** (git hosting) | 48k+ | In-process goroutines + LevelDB | Go's goroutines are extremely cheap. Tasks are lightweight (webhooks, indexing). |
| **Home Assistant** (automation) | 78k+ | asyncio event loop | Tasks are fast I/O calls (API requests, device commands). No heavy compute. |
| **Vaultwarden** (passwords) | 42k+ | Minimal | Almost no background work — just serves encrypted blobs. |
| **Photoprism** (photos) | 36k+ | In-process Go workers | TensorFlow loaded in same process. Works for single-user deployments. |

**Takeaway:** Single-binary apps work when tasks are lightweight, I/O-bound, or the language has cheap concurrency (Go goroutines, Rust async). They struggle with heavy ML workloads where isolation and scaling matter.

### Desktop Applications

Applications like **Final Cut Pro**, **DaVinci Resolve**, and **Handbrake** handle background tasks very differently because they run on a single machine in a single process:

- **Thread pools / GCD** (Grand Central Dispatch) — macOS native thread scheduling
- **NSOperationQueue** (Apple) / **ThreadPoolExecutor** (Python/Java) — in-process work queues
- **Child processes** — spawn ffmpeg or other CLI tools as subprocesses

No Docker, no Redis, no network — just OS-level concurrency primitives. This works because desktop apps don't need:
- Failure isolation across network boundaries
- Independent scaling of different workload types
- Multi-machine distribution

### Cloud / Serverless

| Technology | Used By | Trade-offs |
|------------|---------|------------|
| **AWS SQS + Lambda** | Netflix, many startups | No infrastructure to manage, but vendor lock-in and cold starts |
| **Google Cloud Tasks + Cloud Run** | Spotify | Managed queue, auto-scaling, pay-per-use |
| **Temporal / Cadence** | Uber, Stripe | Workflow orchestration for complex multi-step pipelines |

These are viable for cloud-hosted SaaS but don't work for self-hosted deployments where users run everything on their own hardware.

## Why This Architecture for OpenTranscribe

The multi-container Celery architecture is the right fit because:

| Requirement | How It's Met |
|-------------|--------------|
| Heavy GPU AI workloads | Isolated GPU worker with shared model (threads pool) |
| Self-hosted deployment | Docker Compose runs anywhere with a GPU |
| GPU-free deployment | `DEPLOYMENT_MODE=lite` uses cloud ASR providers |
| Multiple task types | 6 specialized workers with appropriate concurrency |
| Reliability | Worker crashes don't affect API; Celery auto-restarts |
| Scalability | Add workers per queue independently; multi-GPU support |
| Monitoring | Flower dashboard for production visibility |
| Offline / air-gapped | No cloud dependencies; everything runs locally |
| Enterprise auth | Hybrid multi-method: local, LDAP, OIDC, PKI simultaneously |

### Container Count

A full OpenTranscribe deployment runs **15 containers**:

| Category | Containers | Count |
|----------|-----------|-------|
| **Application** | Frontend, Backend, Docs | 3 |
| **Infrastructure** | PostgreSQL, Redis, MinIO, OpenSearch | 4 |
| **Workers** | GPU, Download, CPU, NLP, Embedding, Utility | 6 |
| **Monitoring/Scheduling** | Flower, Celery Beat | 2 |

> **Note on container overhead:** The "weight" of this deployment comes from the AI models (~2.5GB for WhisperX + PyAnnote), not from the container count. Any architecture running the same models would need similar resources. The containers themselves add minimal overhead — they share the host kernel, and infrastructure services (Postgres, Redis, MinIO) use lightweight Alpine-based images.

### Potential Simplifications

For lighter deployment scenarios, the architecture could be simplified:

| Simplification | Trade-off |
|---------------|-----------|
| Merge CPU + NLP + Embedding workers into one | Fewer containers, but a slow NLP task blocks embedding work |
| Use PostgreSQL as queue broker (Procrastinate) | Drop Redis, but lose pub/sub caching and queue performance |
| Supervisord single-container mode | One container for API + all workers, simpler deployment, but no isolation |
| Replace MinIO with local filesystem | One fewer service, but lose S3 compatibility and web console |

These are viable for single-user or development deployments but sacrifice the isolation and scalability needed for production multi-user environments.

## Related Documentation

- [Docker Deployment](DOCKER_DEPLOYMENT.md) — Building and publishing Docker images
- [Search Architecture](SEARCH_ARCHITECTURE.md) — OpenSearch indexing and neural search
- [GPU Optimization Results](GPU_OPTIMIZATION_RESULTS.md) — PyAnnote GPU performance benchmarks
- [Installation](INSTALLATION.md) — Getting started guide
