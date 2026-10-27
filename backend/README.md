<div align="center">
  <img src="../assets/logo-banner.png" alt="OpenTranscribe Logo" width="400">

  # Backend
</div>

A FastAPI-based backend for AI-powered transcription and media processing. The current release version is in the repository-root `VERSION` file.

## Quick Start

### Prerequisites
- Docker and Docker Compose
- Git

### Development Setup
```bash
# Clone the repository
git clone <repository-url>
cd transcribe-app

# Start development environment
./opentr.sh start dev

# Check health
curl http://localhost:5174/health
```

### API Access
- **API Base URL**: http://localhost:5174/api
- **Interactive Docs**: http://localhost:5174/api/docs
- **ReDoc**: http://localhost:5174/api/redoc
- **OpenAPI snapshot**: `backend/openapi.json` (regenerate from the repo root with `backend/venv/bin/python scripts/generate-openapi.py --write`; CI runs `--check`)
- **Flower Dashboard**: http://localhost:5175/flower (basic auth: `FLOWER_USER` / `FLOWER_PASSWORD` from `.env`)

The interactive docs are mounted only when the deployment is not hardened; set `ENABLE_API_DOCS=true` to publish them on a hardened one (`_resolve_docs_urls()` in `app/main.py`).

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Directory Structure](#directory-structure)
- [Development Guide](#development-guide)
- [API Documentation](#api-documentation)
- [Database Management](#database-management)
- [Background Tasks](#background-tasks)
- [Testing](#testing)
- [Deployment](#deployment)
- [Contributing](#contributing)

## 🏗️ Architecture Overview

OpenTranscribe backend is built with modern Python technologies:

### Core Technologies
- **Python 3.12+** - Minimum version (`requires-python` in `pyproject.toml`); the production image runs 3.13
- **FastAPI** - High-performance async web framework
- **SQLAlchemy 2.0** - Modern Python SQL toolkit and ORM
- **Alembic** - Database migration management (`alembic/versions/`, `v010_baseline` onward; run automatically at startup)
- **Celery** - Distributed task queue with per-purpose workers (GPU, CPU, download, cloud ASR, NLP, embedding, redaction, plus optional GPU-split and GPU-scale workers)
- **Redis** - Task broker and caching (shared singleton via `app/core/redis.py`)
- **PostgreSQL** - Primary database
- **MinIO** - S3-compatible object storage
- **OpenSearch 3.4** - Full-text search, vector search, and neural search (ML Commons)

### AI/ML Stack
- **WhisperX** - Advanced speech recognition (default: `large-v3-turbo`, 40x realtime on A6000)
- **PyAnnote v4 / diar-native** - Speaker diarization (the native Rust/ONNX sidecar by default, with an in-process PyAnnote fallback), voice fingerprinting, cross-video speaker matching
- **10 ASR Providers** - Local GPU, Deepgram, AssemblyAI, OpenAI Whisper API, Google, Azure, AWS, Speechmatics, Gladia, pyannote.ai
- **Multi-Provider LLM Integration** - OpenAI, vLLM, Ollama, Anthropic, OpenRouter, AWS Bedrock, and custom OpenAI-compatible endpoints
- **Intelligent Context Processing** - Section-by-section analysis for unlimited transcript lengths
- **FFmpeg** - Media processing and conversion
- **OpenSearch Neural Search** - Hybrid BM25+vector search with ML Commons sentence-transformers

#### AI Processing Features
- **Fast Batch Processing**: WhisperX with faster-whisper; `large-v3-turbo` is default (40x GPU realtime, 54x peak at concurrency=8)
- **100+ Language Transcription**: Support for 100+ languages with configurable source language
- **Word-level Timestamps**: Native word-level timing via faster-whisper cross-attention DTW (all 100+ languages)
- **Optional English Translation**: Toggle to translate non-English audio (use `large-v3` for translation; turbo cannot translate)
- **Advanced Speaker Diarization**: PyAnnote.audio v4 with voice fingerprinting and GPU/MPS optimizations
- **Cross-Video Speaker Recognition**: Embedding-based speaker matching across files, alias-based OpenSearch indices (`speakers` alias → versioned index)
- **Speaker Profile Management**: Global profiles persisting across transcriptions; cosine scores correctly converted from OpenSearch `cosinesimil` space
- **AI-Powered Speaker Suggestions**: LLM speaker ID with confidence scoring and manual verification workflow
- **Speaker Merge**: Combine duplicate speakers with segment reassignment
- **LLM-Powered Summarization**: BLUF format with action items, decisions, speaker analysis; 12 output languages
- **Cloud ASR**: `DEPLOYMENT_MODE=lite` for GPU-free cloud ASR-only deployments
- **Universal Media URL Support**: yt-dlp integration for 1800+ platforms
- **Auto-Cleanup Garbage Segments**: Configurable detection and removal of erroneous segments
- **Admin-Pinned ASR Model**: Admin sets local Whisper model via Super Admin UI; workers preload it at startup

#### AI/ML Configuration
Main environment variables for AI processing (`.env.example` is the authoritative, fully commented list; several of these are also editable in the admin UI, which wins over `.env`):

| Variable | Description | Default |
|----------|-------------|---------|
| `WHISPER_MODEL` | Whisper model to use (`large-v3-turbo`, `large-v3`, `large-v2`) | `large-v3-turbo` |
| `DIARIZATION_MODEL` | PyAnnote diarization model | `pyannote/speaker-diarization-community-1` |
| `BATCH_SIZE` | Transcription batch size: `auto` or an integer (reduce if low on GPU memory) | `auto` |
| `COMPUTE_TYPE` | Computation precision: `auto`, `float16`, `float32` or `int8` | `auto` |
| `MIN_SPEAKERS` | Minimum number of speakers to detect | `1` |
| `MAX_SPEAKERS` | Maximum number of speakers to detect (can be 50+ for large events) | `20` |
| `NUM_SPEAKERS` | Force an exact speaker count, overriding the min/max range | (empty) |
| `ENGINE_DIARIZER_BACKEND` | `native` (diar-native sidecar) or `pyannote` (in-process fork) | `native` |
| `ENGINE_DIARIZER_REQUIRE_SIDECAR` | Strict mode: fail a diarization task instead of falling back to in-process PyAnnote when the sidecar cannot serve | `false` |
| `HUGGINGFACE_TOKEN` | HuggingFace token for the gated diarization model; a super admin can instead save one in Settings, which takes precedence | (empty) |
| `MODEL_CACHE_DIR` | Host directory to cache downloaded models | `./models` |
| `DEPLOYMENT_MODE` | `full` or `lite` (`lite` = GPU-free, cloud ASR) | `full` |

**Note**: Language settings (source language, translate to English, LLM output language) are user-configurable via the Settings UI and stored per-user in the database.

#### Model Caching
OpenTranscribe automatically caches AI models for persistence across container restarts:

- **WhisperX Models**: Cached via HuggingFace Hub (~1.5GB)
- **PyAnnote Models**: Cached via PyTorch/HuggingFace (~500MB)
- **Sentence Transformers**: For neural search (~80MB)
- **OpenSearch ML Models**: `all-MiniLM-L6-v2` for hybrid search (~80MB)
- **Total Storage**: ~2.5GB for complete model cache

Models are downloaded once on first use and automatically reused. Set `MODEL_CACHE_DIR` in your `.env` to specify the host directory for model storage.

#### HuggingFace Authentication
You must obtain a HuggingFace API token to use the speaker diarization functionality. Create an account at [HuggingFace](https://huggingface.co/) and generate a token at https://huggingface.co/settings/tokens.

You also need to accept the user agreement for [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1) (click "Agree and access repository"; it is auto-approved). It is the only gated model either diarization engine loads.

#### Troubleshooting AI Processing
- **High GPU Memory Usage**: Try reducing `BATCH_SIZE` or changing `COMPUTE_TYPE` to `int8`
- **Slow Processing**: Consider using a smaller model like `medium` or `small`
- **Speaker Identification Issues**: Adjust `MIN_SPEAKERS` and `MAX_SPEAKERS` if you know the approximate speaker count (no hard limit - can be set to 50+ for large conferences)

#### AI/ML References
- [WhisperX GitHub Repository](https://github.com/m-bain/whisperX)
- [Pyannote Audio](https://github.com/pyannote/pyannote-audio)

### Architecture Principles
- **Modular Design** - Clear separation of concerns
- **Service Layer** - Business logic abstraction
- **Async/Await** - Non-blocking request handling
- **Background Processing** - CPU-intensive tasks offloaded to workers
- **RESTful API** - Standard HTTP methods and status codes
- **Real-time Updates** - WebSocket notifications for long-running tasks

## 📁 Directory Structure

```
backend/
├── app/                        # Main application package (see app/README.md)
│   ├── api/                   # Routers (api/router.py) and endpoint modules
│   │   ├── endpoints/         # One module per feature; packages for larger ones
│   │   │                      #   (auth/, chat/, files/, scim/, tags/)
│   │   ├── router.py          # Mounts every router under /api
│   │   └── websockets.py      # WebSocket handler (/api/ws)
│   ├── auth/                  # Auth methods (local, LDAP, OIDC, SAML, PKI, proxy), MFA, SCIM tokens
│   ├── core/                  # Config, constants, Celery app, enums, exceptions, Redis, metrics
│   ├── db/                    # Engine/session helpers and the startup Alembic runner
│   ├── legal/                 # Versioned legal documents and acceptance tracking
│   ├── middleware/            # CSRF, audit, observability, upload limit, robots
│   ├── models/                # SQLAlchemy ORM models
│   ├── schemas/               # Pydantic request/response schemas
│   ├── scripts/               # Operator/maintenance scripts that import the app (run in the container)
│   ├── services/              # Business logic layer
│   ├── tasks/                 # Celery tasks (transcription/ holds the pipeline stages)
│   ├── transcription/         # ASR + diarization engine (WhisperX, PyAnnote, diar-native client)
│   ├── utils/                 # Shared helpers
│   ├── main.py                # FastAPI application entry point (lifespan runs migrations)
│   └── initial_data.py        # Idempotent seed data (admin user, default tags, default prompts)
├── alembic/                   # Alembic migrations (versions/v<NNN>_<description>.py)
├── scripts/                   # Standalone utility and benchmark scripts (see scripts/README.md)
├── tests/                     # Test suite (unit/, api/, services/, integration/, e2e/, ...)
├── third_party/               # Vendored assets (bundled fonts)
├── openapi.json               # Generated API snapshot (CI checks it is current)
├── requirements*.txt          # Pinned dependency sets (see CLAUDE.md for which image uses which)
├── Dockerfile.prod            # Production image (multi-stage, non-root); also .lite, .blackwell, .test
└── README.md                  # This file
```

## 🛠️ Development Guide

### Environment Setup

1. **Use the OpenTranscribe utility script** (always go through it; it composes the right compose overlays):
   ```bash
   ./opentr.sh start dev    # Start development environment
   ./opentr.sh logs backend # View backend logs
   ./opentr.sh shell backend # Access backend container
   ```

2. **Host venv** (for pytest, ruff, mypy, pre-commit outside Docker; the app itself runs in the containers):
   ```bash
   cd backend
   python3.13 -m venv venv || python3.12 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   pip install --no-deps -r requirements-nodeps.txt   # whisperx / faster-whisper / gliner
   pip install -r requirements-dev.txt
   ```
   See `backend/CLAUDE.md` for why the two-step install and the pinning rules matter.

### Development Workflow

1. **Code Changes**:
   - `./backend` is bind-mounted into the dev containers; the API runs `uvicorn --reload --reload-dir app`, so edits under `app/` reload the API automatically
   - Celery workers do not hot-reload: run `./opentr.sh restart-backend` to pick up task changes

2. **Database Changes** (full checklist in `app/db/CLAUDE.md`):
   ```bash
   # Add a hand-written, idempotent revision in backend/alembic/versions/
   # Update app/models/ SQLAlchemy models
   # Update app/schemas/ Pydantic schemas
   # Add a detection guard in app/db/migrations.py (_detect_schema_version)
   ./opentr.sh reset dev  # DELETES DATA: drops the DB and runs the full migration chain
   ```

3. **Dependency Changes**:
   ```bash
   # Edit the pinned version in requirements.txt (every spec is an exact ==)
   ./opentr.sh rebuild-backend  # Rebuild the backend images with the new deps
   ```

### Code Style and Standards

- **Python 3.12+**: Minimum version (`pyproject.toml`)
- **Linting**: `ruff check --fix .` then `ruff format .` (replaces black/isort/flake8)
- **Line length**: 100 characters
- **Type Hints**: Required on all new functions
- **Docstrings**: Google-style docstrings for functions/classes
- **Imports**: stdlib → third-party → local (ruff handles ordering)
- **Error Handling**: Use the `app/core/exceptions.py` hierarchy; endpoints raise `HTTPException` (responses carry FastAPI's standard `{"detail": ...}` body)
- **Async/Await**: Prefer async functions for I/O operations
- **Pre-commit**: run `scripts/safe-precommit.sh run --all-files` from the repo root before committing

### Adding New Features

1. **API Endpoints**: Add to `app/api/endpoints/` and mount the router in `app/api/router.py` (organize by feature, e.g. the `files/` package)
2. **Database Models**: Add to `app/models/`
3. **Validation Schemas**: Add to `app/schemas/`
4. **Business Logic**: Add to `app/services/` (LLM service, upload service, etc.)
5. **Background Tasks**: Add to `app/tasks/`; declare any new queue in `CeleryQueues` (`app/core/constants.py`) because Celery runs with `task_create_missing_queues=False`
6. **Core Components**: Add config, constants and enums to `app/core/`; shared helpers go in `app/utils/`
7. **Tests**: Add to `tests/` (see Testing below)
8. **API snapshot**: regenerate `openapi.json` when routes or schemas change (`scripts/generate-openapi.py --write`)

## 📚 API Documentation

### Interactive Documentation
- **Swagger UI**: http://localhost:5174/api/docs
- **ReDoc**: http://localhost:5174/api/redoc
- **Committed snapshot**: `backend/openapi.json` (the authoritative route list)

Both UIs are off on hardened deployments unless `ENABLE_API_DOCS=true`.

### API Structure

Top-level route groups under `/api` (`app/api/router.py`; each prefix maps to a module or package in `app/api/endpoints/`). `backend/openapi.json` lists every path.

```
/api/
├── /auth              # Login, register, refresh, MFA, OIDC/SAML/PKI/proxy, password policy, invitations
├── /files             # Upload (incl. multipart), streaming, URL processing, export, summaries, topics, waveforms
├── /my-files          # A user's own file status and recovery requests
├── /users             # User management and search
├── /user-settings     # Per-user preferences (transcription, recording, redaction, pipeline, diarization, ...)
├── /system            # Stats, capabilities, config probes
├── /comments, /tags, /collections      # Comments, tags, collections (sharing, visibility)
├── /speakers, /speaker-profiles, /speaker-clusters, /speaker-attributes
├── /prompts           # Summary prompts (incl. shared prompts)
├── /llm, /llm-settings                 # LLM status and per-user LLM configuration
├── /asr-settings      # ASR provider config, local model set/restart
├── /custom-vocabulary # Vocabulary terms
├── /search            # Hybrid BM25 + neural search, index health, reindex
├── /chat              # RAG chat: conversations, messages, projects
├── /tasks             # Task monitoring, progress, retry, stuck-task recovery
├── /groups            # User groups
├── /watch-sources     # Auto-import sources (local / S3 / SMB)
├── /embeddings        # Embedding migration controls
├── /legal, /usage, /version
├── /org-admin         # Organization admin: audit logs, legal holds, quarantine, GDPR erase, support access
├── /support-access, /takedown-notices  # Support-access grants and takedown notices
├── /admin             # Platform admin: auth config, backup, engine settings, local models, retention, SCIM tokens, ...
└── /ws                # WebSocket for real-time notifications
```

Outside `/api`: `/health`, `/health/ready`, `/metrics` (Prometheus, not in the schema) and the SCIM 2.0 provisioning API at `/scim/v2`.

### Authentication
- **JWT** access tokens, accepted either as an `Authorization: Bearer` header (API clients, Swagger UI) or as the httpOnly `access_token` cookie (the browser SPA)
- **Roles**: `user`, `admin`, `super_admin` (`app/auth/roles.py`); organization admins act through `/org-admin`
- **Ownership / sharing** checks on files, collections and other per-user resources

### Response Format
Endpoints return their own Pydantic response models (`app/schemas/`), not a shared envelope. Errors use FastAPI's standard body:

```json
{
  "detail": "Error description"
}
```

Request-validation failures (422) return `detail` as a list of scrubbed field errors (`app/core/validation_errors.py`).

## Database Management

### Approach (All Environments)
Alembic is the sole authority for database schema in all environments. `run_migrations()` in `app/db/migrations.py` runs from the FastAPI lifespan on every backend startup: an empty database gets the full chain, an existing untracked database is fingerprinted, stamped and upgraded, and a tracked one gets any pending revisions (guarded by a PostgreSQL advisory lock). Details: `app/db/README.md`.

- **Migrations**: `backend/alembic/versions/` - hand-written, idempotent revisions named `v<NNN>_<description>.py`, starting at `v010_baseline`. Derive the current head with `python3 scripts/release-tests/lib/alembic-head.py backend` (run from the repo root) rather than reading a number from prose.
- **Models**: SQLAlchemy models in `app/models/`
- **Validation**: Pydantic schemas in `app/schemas/`
- **Quick Reset**: `./opentr.sh reset dev` drops the DB and reruns the full migration chain (deletes all data)

### Database Scripts
Run these inside the backend container (`./opentr.sh shell backend`), where the database environment is configured; `alembic.ini` is cwd-relative, so run `alembic` from `/app` (the `backend/` directory).
```bash
alembic current                    # Show current migration version
alembic upgrade head               # Apply pending migrations (normally automatic at startup)
alembic downgrade -1               # Roll back one migration
python scripts/db_inspect.py       # Debug dump of the tag tables (scripts/README.md lists the other helpers)
```

## ⚙️ Background Tasks

### Task System
- **Celery** with a Redis broker; queues are declared once in `CeleryQueues` (`app/core/constants.py`) and routed in `app/core/celery.py`
- **Flower** monitoring at http://localhost:5175/flower
- **Composed transcription pipeline**: a per-file plan (`app/services/pipeline_plan.py`) chains the stage tasks in `app/tasks/transcription/` (`transcription.preprocess` -> `transcription.gpu_transcribe` / `transcription.diarize_gpu` / `transcription.cpu_transcribe` or a cloud-ASR task -> `transcription.postprocess`), so a file only visits the stages and queues its settings require
- Enrichment (embeddings, indexing, summaries, redaction, ...) is dispatched asynchronously after postprocess, so it does not hold the GPU

### Worker Services
Defined in `docker-compose.yml` (queue lists are the `-Q` arguments there; which task goes to which queue is `task_routes` in `app/core/celery.py`):

| Service | Queues | Role |
|---|---|---|
| `celery-worker` | `gpu` | Local GPU transcription and diarization |
| `celery-download-worker` | `download` | Media/URL download (yt-dlp) |
| `celery-cpu-worker` | `cpu`, `utility`, `cpu-transcribe` | CPU transcription, analytics, speaker embeddings, search maintenance, cleanup |
| `celery-cloud-asr-worker` | `cloud-asr` | Cloud ASR provider calls |
| `celery-nlp-worker` | `nlp`, `celery` | LLM work: summarization, topic extraction, speaker identification, auto-labeling |
| `celery-embedding-worker` | `embedding` | Transcript search indexing (embedding generation) |
| `celery-redaction` | `redaction` | Content redaction detection |
| `celery-beat` | n/a | Periodic schedule |
| `celery-worker-gpu-transcribe`, `celery-worker-gpu-diarize` | `gpu-transcribe`, `gpu-diarize` | Split GPU stages (`gpu-split` profile) |
| `celery-worker-gpu-scaled` | GPU queue | Parallel GPU workers (`gpu-scale` profile, `--gpu-scale`) |

In `DEPLOYMENT_MODE=lite` the GPU workers are scaled to zero and GPU-preferring work is routed to the `cpu` queue.

### Task Modules
`app/tasks/` is organised by concern (see `app/tasks/README.md`): the `transcription/` package; speaker embedding, identification, clustering, merge and update tasks; search indexing, reindex and integrity tasks; summarization, topic extraction and auto-labeling; redaction; media download and YouTube processing; watch sources; backup, cleanup, recovery and chat-retention maintenance.

### Task Monitoring
```bash
# View task status
./opentr.sh logs celery-worker

# Monitor with Flower
# http://localhost:5175/flower
```

## Testing

Tests run against the **live dev stack** (real PostgreSQL on `localhost:5176`, MinIO, OpenSearch where reachable), so start it first with `./opentr.sh start dev`.

### Test Suite
```bash
# Whole backend suite (no e2e); keeps the log and JUnit XML so results can be re-read without re-running
./scripts/run-backend-tests.sh
./scripts/run-backend-tests.sh tests/unit      # a subset (path relative to backend/)
./scripts/run-backend-tests.sh --summary       # re-report the last run

# Direct pytest from the host venv (parallelism, timeout and marker filters come from pyproject addopts)
cd backend && source venv/bin/activate
pytest tests/unit -v
pytest tests/api/endpoints/test_auth_comprehensive.py -v
pytest --cov=app tests/ --ignore=tests/e2e

# Pre-merge gate (unit/API + security suites + integration-marked tests)
./scripts/run-integration-tests.sh

# E2E (Playwright; requires the dev stack)
./scripts/e2e/run-e2e.sh
```

### Test Structure
- **Unit/API tests** (`tests/unit/`, `tests/api/`, `tests/services/`, `tests/redaction/`, `tests/transcription/`, `tests/onnx/` plus top-level `test_*.py` files): API endpoints, auth, ASR settings, FIPS/FedRAMP controls, MFA, PKI, search, chat
- **Integration tests** (`tests/integration/`, marker `integration`): need the running stack; deselected by default
- **E2E tests** (`tests/e2e/`): Playwright browser tests (login, upload, gallery, search, settings, auth flows)
- **Auth tests**: see `tests/AUTH_TEST_SETUP.md` for credentials and container setup

### Test Configuration
- `pyproject.toml` `addopts` runs `-n auto`, a 300 s per-test timeout, and deselects `integration` and `gpu` tests by default; select them with `-m integration` / `-m gpu`
- Coverage is enforced (`fail_under` in `pyproject.toml`)
- A few expensive checks are opt-in behind `RUN_*` environment variables (for example `RUN_INDEX_AUDIT`, `RUN_EXPORT_CAPABILITY_TEST`); the older `RUN_LLM_TESTS` / `RUN_MFA_TESTS` / `RUN_PKI_TESTS` / `RUN_FIPS_TESTS` style gates no longer skip anything - those suites run by default
- Markers, fixtures and gates are documented in `backend/tests/CLAUDE.md`

## 🚀 Deployment

### Production Setup
Production runs pre-built images from Docker Hub (`davidamacey/opentranscribe-backend`, plus `-backend-lite` for the CPU-only variant); local source changes are not in them until you rebuild. For a locally built production stack use `./opentr.sh start prod --build`. Installation and operations guides live in `docs-site/docs/`.

1. **Environment Variables**: generate `.env` from `.env.example` (the installer does this); production refuses to boot with placeholder secrets
2. **Database**: migrations run automatically on backend startup (`app/db/migrations.py`)
3. **Storage**: MinIO/S3 for media
4. **Search**: OpenSearch (3.4 with the ML Commons plugin)
5. **Workers**: Celery workers per the Worker Services table above; GPU workers need the NVIDIA runtime
6. **Monitoring**: Flower, Prometheus metrics at `/metrics`

### Environment Variables
`.env.example` is the authoritative list; these are the names `backend/app/core/config.py` reads. The most important ones:

```bash
# Core
POSTGRES_HOST=postgres          # DATABASE_URL overrides the assembled connection string
POSTGRES_USER=postgres
POSTGRES_PASSWORD=...
POSTGRES_DB=opentranscribe
JWT_SECRET_KEY=...              # production refuses the placeholder
ENCRYPTION_KEY=...              # decrypts DB-stored secrets; never change after first use

# Storage (in .env, MINIO_PORT / OPENSEARCH_PORT are the HOST-published ports;
# docker-compose.yml sets the in-container values 9000 / 9200 shown here)
MINIO_HOST=minio
MINIO_PORT=9000
MINIO_ROOT_USER=...
MINIO_ROOT_PASSWORD=...

# AI/ML (optional fallback; can also be set in the admin UI)
HUGGINGFACE_TOKEN=...

# Search
OPENSEARCH_HOST=opensearch
OPENSEARCH_PORT=9200
```

### Health Checks
- **Liveness**: `GET /health` returns `{"status": "healthy", "version": ...}`
- **Readiness**: `GET /health/ready` probes PostgreSQL and Redis (critical, failure returns 503) and OpenSearch and MinIO (reported but non-fatal)
- **Workers**: Flower, or `./opentr.sh status`

### Container Security

OpenTranscribe backend follows Docker security best practices:

**Non-Root User Implementation:**
- Containers run as `appuser` (UID 1000) instead of root
- Follows principle of least privilege for enhanced security
- Compliant with security scanning tools (Trivy, Snyk, etc.)

**Multi-Stage Build:**
- Build dependencies isolated from runtime image
- Minimal attack surface with only required runtime packages
- Reduced image size and faster deployments

**GPU Access:**
- User added to `video` group for GPU device access
- Compatible with NVIDIA Container Runtime
- PyTorch wheels target CUDA 12.8 (`cu128`) with cuDNN 9

**Model Caching:**
- Models cached in user home directory (`/home/appuser/.cache`)
- Persistent storage between container restarts
- No re-downloads required after initial setup

**Migration for Existing Deployments:**
```bash
# Fix permissions for existing model cache
./scripts/fix-model-permissions.sh

# Restart the backend and workers
./opentr.sh restart-backend
```

## 🤝 Contributing

### Development Process
1. **Fork** the repository
2. **Create** a feature branch: `git checkout -b feature/amazing-feature`
3. **Follow** the code style and add tests
4. **Commit** with a conventional commit message (`<type>(<scope>): <summary>`), after `scripts/safe-precommit.sh run --all-files` passes
5. **Push** the branch and open a Pull Request

### Code Review Checklist
- [ ] Code follows style guidelines
- [ ] Tests added for new functionality
- [ ] Documentation updated
- [ ] No breaking changes (or properly documented)
- [ ] Performance impact considered
- [ ] Security implications reviewed

### Reporting Issues
- Use GitHub Issues for bug reports
- Include environment details
- Provide reproduction steps
- Include relevant logs

## 📞 Support

### Documentation
- **API Docs**: http://localhost:5174/api/docs (non-hardened deployments)
- **Architecture**: [app/README.md](app/README.md)
- **Database**: [app/db/README.md](app/db/README.md)
- **Scripts**: [scripts/README.md](scripts/README.md)
- **Per-package agent/contributor notes**: the `CLAUDE.md` files beside each package (for example `backend/CLAUDE.md`)

### Getting Help
- **GitHub Issues**: Bug reports and feature requests
- **Discussions**: General questions and discussions
- **Documentation**: Check existing docs first

### Useful Commands
```bash
# Development
./opentr.sh start dev           # Start development
./opentr.sh logs backend        # View logs
./opentr.sh shell backend       # Access container
./opentr.sh restart-backend     # Restart backend and workers

# Database
./opentr.sh reset dev           # Reset database (deletes data)

# Testing
./scripts/run-backend-tests.sh  # Run the backend suite
```

---

**Built with FastAPI, SQLAlchemy, and modern Python technologies.**
