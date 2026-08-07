<div align="center">
  <img src="../assets/logo-banner.png" alt="OpenTranscribe Logo" width="400">

  # Backend
</div>

A modern FastAPI-based backend for AI-powered transcription and media processing. This is OpenTranscribe v0.4.0.

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
- **Interactive Docs**: http://localhost:5174/docs
- **ReDoc**: http://localhost:5174/redoc
- **Flower Dashboard**: http://localhost:5175/flower

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
- **Python 3.11+** - Minimum Python version (3.12 recommended)
- **FastAPI** - High-performance async web framework
- **SQLAlchemy 2.0** - Modern Python SQL toolkit and ORM
- **Alembic** - Database migration management (v000–v355 migrations)
- **Celery** - Distributed task queue; GPU worker, CPU worker, and embedding worker
- **Redis** - Task broker and caching (shared singleton via `app/core/redis.py`)
- **PostgreSQL** - Primary database
- **MinIO** - S3-compatible object storage
- **OpenSearch 3.4** - Full-text search, vector search, and neural search (ML Commons)

### AI/ML Stack
- **WhisperX** - Advanced speech recognition (default: `large-v3-turbo`, 40x realtime on A6000)
- **PyAnnote v4** - Speaker diarization, voice fingerprinting, cross-video speaker matching
- **10 ASR Providers** - Local GPU, Deepgram, AssemblyAI, OpenAI Whisper API, Google, Azure, AWS, Speechmatics, Gladia, pyannote.ai
- **Multi-Provider LLM Integration** - vLLM, OpenAI, Ollama, Anthropic, OpenRouter
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
Required environment variables for AI processing:

| Variable | Description | Default |
|----------|-------------|---------|
| `WHISPER_MODEL` | Whisper model to use (`large-v3-turbo`, `large-v3`, `large-v2`) | `large-v3-turbo` |
| `DIARIZATION_MODEL` | PyAnnote diarization model | `pyannote/speaker-diarization-community-1` |
| `BATCH_SIZE` | Batch size for processing (reduce if low on GPU memory) | `16` |
| `COMPUTE_TYPE` | Computation precision (`float16` or `int8`) | `float16` |
| `MIN_SPEAKERS` | Minimum number of speakers to detect (optional) | `1` |
| `MAX_SPEAKERS` | Maximum number of speakers to detect (optional, can be 50+ for large events) | `20` |
| `HUGGINGFACE_TOKEN` | HuggingFace API token for diarization models | Required |
| `MODEL_CACHE_DIR` | Host directory to cache downloaded models | `./models` |
| `DEPLOYMENT_MODE` | Set to `lite` for GPU-free cloud ASR deployments | (unset) |

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

You also need to accept the user agreement for the following models:
- [Segmentation](https://huggingface.co/pyannote/segmentation)
- [Speaker-Diarization-Community-1](https://huggingface.co/pyannote/speaker-diarization-community-1) (no agreement required)

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
├── app/                        # Main application package
│   ├── api/                   # API layer
│   │   ├── endpoints/         # API route handlers
│   │   │   ├── files/        # File management (upload, streaming, url_processing, etc.)
│   │   │   ├── asr_settings.py  # ASR provider management + local model restart
│   │   │   ├── groups.py     # User group management
│   │   │   ├── media_collections.py  # Collection sharing
│   │   │   ├── system.py     # System stats endpoint
│   │   │   └── *.py          # Individual endpoint modules
│   │   ├── router.py         # Main API router
│   │   └── websockets.py     # WebSocket handlers
│   ├── auth/                 # Authentication modules (local, LDAP, Keycloak, PKI, MFA)
│   ├── core/                 # Core configuration and setup
│   │   ├── constants.py      # Language constants, system defaults, OpenSearch model registry
│   │   ├── enums.py          # Centralized FileStatus enum
│   │   ├── exceptions.py     # Custom exception hierarchy
│   │   └── redis.py          # Shared Redis singleton (get_redis())
│   ├── db/                   # Database utilities
│   │   └── migrations.py     # Automatic startup Alembic runner
│   ├── models/               # SQLAlchemy ORM models
│   ├── schemas/              # Pydantic validation schemas
│   ├── services/             # Business logic layer
│   │   ├── interfaces.py     # Protocol interfaces (Storage, Search, Cache, Notification)
│   │   ├── notification_service.py  # Unified send_task_notification() wrapper
│   │   ├── progress_tracker.py      # EWMA ETA progress tracking
│   │   └── ...               # 40+ service modules
│   ├── tasks/                # Background task processing
│   │   └── transcription/    # 3-stage pipeline (preprocess, gpu_transcription, postprocess)
│   ├── utils/                # Common utilities
│   │   └── transcript_builders.py  # Shared transcript formatting
│   ├── main.py              # FastAPI application entry point
│   └── initial_data.py      # Database initialization
├── alembic/                  # Alembic migrations (v000–v355)
├── scripts/                  # Utility scripts
├── tests/                    # Test suite
│   ├── e2e/                  # Playwright E2E tests
│   └── *.py                  # Backend unit/integration tests
├── requirements.txt          # Python dependencies
├── Dockerfile.prod           # Production container (multi-stage, non-root)
└── README.md                 # This file
```

## 🛠️ Development Guide

### Environment Setup

1. **Use the OpenTranscribe utility script**:
   ```bash
   ./opentr.sh start dev    # Start development environment
   ./opentr.sh logs backend # View backend logs
   ./opentr.sh shell backend # Access backend container
   ```

2. **Local development** (outside Docker):
   ```bash
   source backend/venv/bin/activate
   pip install -r backend/requirements.txt
   uvicorn app.main:app --reload --host 0.0.0.0 --port 8080
   ```

### Development Workflow

1. **Code Changes**:
   - Backend code is mounted as a volume in development
   - Changes are automatically reloaded via `--reload` flag
   - No container restart needed for code changes

2. **Database Changes**:
   ```bash
   # Create new Alembic migration in backend/alembic/versions/
   # Update app/models/ SQLAlchemy models
   # Update app/schemas/ Pydantic schemas
   # Update app/db/migrations.py detection logic
   ./opentr.sh reset dev  # Drops DB and runs full migration chain
   ```

3. **Dependency Changes**:
   ```bash
   # Add to requirements.txt
   ./opentr.sh restart-backend  # Rebuild container with new deps
   ```

### Code Style and Standards

- **Python 3.11+**: Minimum version; use `from __future__ import annotations` for modern syntax
- **Linting**: `ruff check --fix .` then `ruff format .` (replaces black/isort/flake8)
- **Line length**: 100 characters
- **Type Hints**: Required on all new functions
- **Docstrings**: Google-style docstrings for functions/classes
- **Imports**: stdlib → third-party → local (ruff handles ordering)
- **Error Handling**: Use `app/core/exceptions.py` hierarchy and structured error responses
- **Async/Await**: Prefer async functions for I/O operations

### Adding New Features

1. **API Endpoints**: Add to `app/api/endpoints/` (organize by feature like `/files/`, `/user-settings/`)
2. **Database Models**: Add to `app/models/`
3. **Validation Schemas**: Add to `app/schemas/`
4. **Business Logic**: Add to `app/services/` (LLM service, upload service, etc.)
5. **Background Tasks**: Add to `app/tasks/` (transcription, summarization, notifications)
6. **Core Components**: Add shared utilities to `app/core/` (constants, configurations)
7. **Tests**: Add to `tests/`

### New API Endpoints Added

- **User Settings API** (`/api/user-settings/`):
  - GET `/recording` - Get user recording preferences
  - PUT `/recording` - Update recording settings (duration, quality, auto-stop)
  - DELETE `/recording` - Reset to default settings
  - GET `/transcription` - Get user transcription preferences (speaker settings, language, garbage cleanup)
  - PUT `/transcription` - Update transcription settings
  - DELETE `/transcription` - Reset to default settings
  - GET `/transcription/system-defaults` - Get system defaults and available language options
  - GET `/all` - Get all user settings for debugging

- **System API** (`/api/system/`):
  - GET `/stats` - System statistics (CPU, memory, disk, GPU) accessible to all authenticated users

- **Enhanced File Processing**:
  - Improved upload handling with concurrency control
  - Better streaming support for large files
  - Enhanced URL processing with metadata extraction
  - POST `/{file_id}/analytics/refresh` - Refresh analytics computation for a media file
  - Pagination support for large transcripts

- **Advanced Speaker Management**:
  - Enhanced speaker suggestions with consolidated profile matching
  - Automatic profile creation and assignment workflow
  - Cross-video speaker recognition with embedding similarity
  - LLM-powered speaker identification using conversational context
  - Speaker merge functionality with segment reassignment

- **Enhanced Data Processing**:
  - Server-side analytics computation with comprehensive speaker metrics
  - Intelligent data formatting service for consistent display
  - Error categorization service with user-friendly suggestions
  - Task filtering service for optimized data retrieval

- **LLM Improvements**:
  - Model auto-discovery for OpenAI-compatible providers
  - Multilingual output language support (12 languages)
  - Improved endpoint configuration handling

## 📚 API Documentation

### Interactive Documentation
- **Swagger UI**: http://localhost:5174/docs
- **ReDoc**: http://localhost:5174/redoc

### API Structure
```
/api/
├── /auth              # Authentication (login, register, refresh, MFA, password policy, lockout)
├── /files             # File management with streaming support
├── /files/streaming   # Streaming and upload progress endpoints
├── /files/upload      # Enhanced upload handling with concurrency
├── /files/url-processing # URL processing for yt-dlp supported platforms
├── /users             # User management
├── /user-settings     # User-specific settings (recording, transcription preferences)
├── /system            # System statistics (CPU, memory, disk, GPU)
├── /comments          # Comment system
├── /tags              # Tag management
├── /speakers          # Speaker management, merge, and cross-video matching
├── /speaker-profiles  # Global speaker profile management
├── /summarization     # LLM-powered summarization endpoints
├── /llm-settings      # User-specific LLM configuration management
├── /asr-settings      # ASR provider management + local model set/restart
├── /groups            # User group management
├── /collection-shares # Collection sharing with permissions
├── /file-retention    # File retention policy management
├── /tasks             # Task monitoring with enhanced progress tracking
├── /search            # Hybrid BM25+neural search
└── /admin             # Admin operations (+ /admin/profile-embeddings/repair)
```

### Authentication
- **JWT Tokens** for API authentication
- **Role-based access** (user, admin)
- **File ownership** validation

### Response Format
```json
{
  "data": {...},
  "message": "Success message",
  "status": "success"
}
```

### Error Format
```json
{
  "detail": "Error description",
  "status_code": 400,
  "error_type": "validation_error"
}
```

## Database Management

### Approach (All Environments)
Alembic is the sole authority for database schema in all environments. Migrations run automatically on backend startup (`app/db/migrations.py` detects version and stamps untracked DBs).

- **Migrations**: `backend/alembic/versions/` — v000 (bootstrap) through v355
- **Models**: SQLAlchemy models in `app/models/`
- **Validation**: Pydantic schemas in `app/schemas/`
- **Quick Reset**: `./opentr.sh reset dev` drops DB and reruns full migration chain
- **Production**: Backend auto-runs `alembic upgrade head` on startup

### Database Scripts
```bash
# Development
./opentr.sh reset dev              # Drop and recreate with full migration chain
python scripts/db_inspect.py       # Inspect database state

# Migrations
alembic upgrade head               # Apply pending migrations
alembic current                    # Show current migration version
alembic downgrade -1               # Roll back one migration
```

## ⚙️ Background Tasks

### Task System
- **Celery** with Redis broker (three worker types: GPU worker, CPU worker, embedding worker)
- **Flower** monitoring at http://localhost:5175/flower
- **3-stage transcription pipeline**: `preprocess_for_transcription` → `transcribe_gpu_task` → `finalize_transcription`
- Async enrichment decoupling — postprocess no longer blocks GPU for enrichment

### Worker Types
- **`celery-worker`**: GPU worker — runs `preprocess`, `gpu_transcription`, `postprocess` tasks
- **`celery-cpu-worker`**: CPU worker — summarization, analytics, cleanup, maintenance
- **`celery-embedding-worker`**: Embedding worker — speaker embedding extraction and reassignment

### Available Tasks
- **Transcription**: 3-stage pipeline with WhisperX + speaker diarization (100+ languages)
- **Cloud ASR**: Deepgram, AssemblyAI, OpenAI, Google, Azure, AWS, Speechmatics, Gladia
- **Speaker Embedding**: Extraction and reassignment (`speaker_embedding_task.py`)
- **Speaker Identification**: LLM-powered name suggestions (`speaker_identification_task.py`)
- **Speaker Updates**: Background speaker record updates (`speaker_update_task.py`)
- **Reindexing**: Search reindexing with stop/cancel support (`reindex_task.py`)
- **File Retention**: Auto-deletion based on admin policy (`file_retention_task.py`)
- **Summarization**: Multi-provider LLM, BLUF format, 12 output languages
- **Analytics**: Transcript analytics and metrics
- **Cleanup**: Stuck file detection and recovery

### Task Monitoring
```bash
# View task status
./opentr.sh logs celery-worker

# Monitor with Flower
# http://localhost:5175/flower
```

## Testing

### Test Suite
```bash
# Activate venv
source backend/venv/bin/activate

# Run all backend tests (parallel, recommended)
pytest backend/tests/ -n 4 --ignore=backend/tests/e2e -q

# Run specific test file
pytest backend/tests/api/endpoints/test_auth_comprehensive.py -v

# ASR settings tests (68 tests)
pytest backend/tests/test_asr_settings.py -v

# E2E tests (requires dev environment running)
pytest backend/tests/e2e/ -v

# Run with coverage
pytest --cov=app backend/tests/ --ignore=backend/tests/e2e
```

### Test Structure
- **Unit/Integration Tests** (`tests/`): API endpoints, auth, ASR settings, FIPS compliance, MFA, PKI
- **E2E Tests** (`tests/e2e/`): Playwright browser tests — login, registration, auth flows, MFA, PKI, LDAP/Keycloak
- **Auth tests**: See `tests/AUTH_TEST_SETUP.md` for credentials and container setup

### Test Configuration
- **Database**: PostgreSQL with transaction rollback isolation (uses Docker-exposed port 5176)
- **Disabled by default**: S3/MinIO, Celery AI imports, Redis, OpenSearch
- **Optional test suites**: Enable with env vars (`RUN_LLM_TESTS`, `RUN_FIPS_TESTS`, `RUN_MFA_TESTS`, `RUN_PKI_TESTS`, `RUN_AUTH_E2E`)

## 🚀 Deployment

### Production Setup
1. **Environment Variables**: Configure production settings
2. **Database**: Run Alembic migrations
3. **Storage**: Configure MinIO/S3 for file storage
4. **Search**: Set up OpenSearch cluster
5. **Workers**: Deploy Celery workers with GPU support
6. **Monitoring**: Set up logging and metrics

### Environment Variables
```bash
# Core
DATABASE_URL=postgresql://user:pass@host:5432/db
SECRET_KEY=your-secret-key

# Storage
MINIO_ENDPOINT=your-minio-endpoint
MINIO_ACCESS_KEY=your-access-key
MINIO_SECRET_KEY=your-secret-key

# AI/ML
HUGGINGFACE_TOKEN=your-hf-token

# Search
OPENSEARCH_URL=your-opensearch-url
```

### Health Checks
- **Application**: `/health` endpoint
- **Database**: Connection validation
- **Storage**: MinIO connectivity
- **Search**: OpenSearch status
- **Workers**: Celery worker health

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
- Supports CUDA 12.8 and cuDNN 9 for AI models

**Model Caching:**
- Models cached in user home directory (`/home/appuser/.cache`)
- Persistent storage between container restarts
- No re-downloads required after initial setup

**Migration for Existing Deployments:**
```bash
# Fix permissions for existing model cache
./scripts/fix-model-permissions.sh

# Restart containers with new image
docker compose restart backend celery-worker
```

## 🤝 Contributing

### Development Process
1. **Fork** the repository
2. **Create** feature branch: `git checkout -b feature/amazing-feature`
3. **Follow** code style and add tests
4. **Commit** changes: `git commit -m 'Add amazing feature'`
5. **Push** branch: `git push origin feature/amazing-feature`
6. **Create** Pull Request

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
- **API Docs**: http://localhost:5174/docs
- **Architecture**: [app/README.md](app/README.md)
- **Database**: [app/db/README.md](app/db/README.md)
- **Scripts**: [scripts/README.md](scripts/README.md)

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
./opentr.sh restart-backend     # Restart backend only

# Database
./opentr.sh reset dev           # Reset database
python scripts/db_inspect.py    # Inspect database

# Testing
pytest tests/                   # Run tests
pytest --cov=app tests/         # With coverage
```

---

**Built with ❤️ using FastAPI, SQLAlchemy, and modern Python technologies.**
