<div align="center">
  <img src="../../assets/logo-banner.png" alt="OpenTranscribe Logo" width="300">

  # Application Architecture
</div>

This directory contains the main OpenTranscribe application code, organized following modern FastAPI and Python best practices.

## 🏗️ Architecture Overview

The application follows a **layered architecture** with clear separation of concerns:

```
┌─────────────────┐
│   API Layer    │  ← HTTP endpoints, request/response handling
├─────────────────┤
│ Service Layer   │  ← Business logic, orchestration
├─────────────────┤
│  Data Layer     │  ← Models, schemas, database operations
├─────────────────┤
│ Infrastructure  │  ← External services, utilities, config
└─────────────────┘
```

## 📁 Directory Structure

Each package has a deeper README or `CLAUDE.md`; the sections below only orient you.

```
app/
├── api/                    # API Layer (see api/README.md)
│   ├── endpoints/         # Route handlers, one module per resource; packages for larger ones
│   │   ├── auth/         # Login, registration, MFA, OIDC/SAML/PKI/proxy, sessions, invitations
│   │   ├── chat/         # RAG chat: conversations, messages, projects, export
│   │   ├── files/        # Upload (incl. multipart), streaming, URL processing, CRUD, export, reprocess
│   │   ├── scim/         # SCIM 2.0 provisioning (mounted at /scim/v2)
│   │   ├── tags/         # Tag CRUD, discovery, operations, sharing
│   │   └── *.py          # admin*, org_admin*, asr_settings, llm_settings, search, speakers, tasks, ...
│   ├── router.py         # Mounts every router under /api
│   ├── deps_context.py   # Request context dependency (tenant scope)
│   └── websockets.py     # Real-time WebSocket handler (/api/ws)
├── auth/                  # Authentication & authorization (see auth/CLAUDE.md)
│   ├── direct_auth.py, ldap_auth.py, pki_auth.py   # Local, LDAP/AD, PKI/X.509
│   ├── oidc/, saml/, proxy/                       # OIDC, SAML, trusted-proxy identity
│   ├── mfa.py, mfa_policy.py                      # TOTP MFA and who must use it
│   ├── password_*.py, lockout.py, rate_limit.py   # Password policy/history/reset, lockout, rate limits
│   ├── token_service.py, cookies.py, session.py   # JWT/refresh tokens, httpOnly cookies, login-flow state
│   ├── roles.py, approval.py, invitations.py      # Role model, account approval, invitations
│   └── audit.py, audit_export.py                  # Authentication audit logging and export
├── core/                  # Core configuration (config.py, constants.py, celery.py, enums.py, exceptions.py,
│                          #   redis.py, security.py, metrics, task liveness/cancellation helpers, tenancy seams)
├── db/                    # base.py (engine, SessionLocal, Base, get_db), session_utils.py, migrations.py
├── legal/                 # Versioned legal documents, registry and acceptance records
├── middleware/            # audit (request IDs), csrf, observability (metrics + access log),
│                          #   upload_limit, robots
├── models/                # SQLAlchemy ORM models (one module per domain; media.py holds the core tables)
├── schemas/               # Pydantic request/response schemas
├── scripts/               # Maintenance scripts that import the app; run inside the container
├── services/              # Business logic layer (see services/README.md)
├── tasks/                 # Celery tasks (see tasks/README.md); transcription/ holds the pipeline stages
├── transcription/         # ASR + diarization engine: model manager, transcriber, diarizers,
│                          #   speaker assignment, VRAM budgeting (see transcription/CLAUDE.md)
├── utils/                 # Shared helpers (see utils/README.md)
├── main.py               # FastAPI application entry point (lifespan, middleware, routers)
└── initial_data.py       # Idempotent seed data (admin user, default tags and prompts)
```

## 🔄 Request Flow

### Typical API Request Flow
```
1. HTTP Request → FastAPI Router
2. Router → Endpoint Handler (api/endpoints/)
3. Endpoint → Service Layer (services/)
4. Service → Database (models/) + External Services
5. Response ← Pydantic Schema (schemas/)
6. HTTP Response ← FastAPI
```

### Background Task Flow
```
1. API Request → Endpoint
2. Endpoint → Task Dispatch (tasks/)
3. Celery Worker → Task Processing
4. Task → Services + Models
5. WebSocket Notification → Client
```

## 🌐 API Layer (`api/`)

### Organization Principle
- **Resource-based routing**: Each file handles one resource type
- **Modular endpoints**: Complex endpoints split into sub-modules
- **Consistent patterns**: Standard CRUD operations
- **Clear separation**: HTTP handling separate from business logic

### Endpoint Structure
```python
# Standard endpoint pattern
@router.get("/resource/{id}", response_model=ResourceSchema)
def get_resource(
    id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get a specific resource by ID."""
    return resource_service.get_by_id(db, id, current_user)
```

### Files Package (`api/endpoints/files/`)
Complex file operations are split by concern:
- `prepare_upload.py`, `upload.py`, `multipart.py`, `complete_upload.py`, `cancel_upload.py` - upload flows (direct and browser-side presigned multipart)
- `crud.py`, `filtering.py`, `management.py` - listing, filtering, recovery and force-delete
- `streaming.py`, `waveform.py`, `captions.py`, `subtitles.py` - playback, waveform and caption/subtitle output
- `transcript_export.py`, `transcript_document_export.py` - text/JSON/CSV/SRT/VTT/Markdown and DOCX/PDF transcript export
- `url_processing.py` - yt-dlp URL ingestion
- `reprocess.py`, `post_processing.py`, `summary_status.py`, `segments.py` - re-runs, post-processing status, summary status, segment edits

## 🔧 Service Layer (`services/`)

### Purpose
- **Business logic encapsulation**: Keep endpoints thin
- **Reusable operations**: Share logic between endpoints
- **Transaction management**: Handle database transactions
- **External service integration**: Manage third-party APIs

### Service Pattern
Services are mostly modules of functions that take a `Session` (a few are classes, e.g. `LLMService`); the shape is:
```python
def create_resource(db: Session, data: CreateSchema, user: User) -> Resource:
    """Create a new resource with business logic."""
    # Validation
    # Business rules
    # Database operations
    # Return result
```

See `services/README.md` for the module inventory.

## 📊 Data Layer (`models/` & `schemas/`)

### Models (`models/`)
- **SQLAlchemy ORM models**: Database table definitions
- **Relationships**: Foreign keys and joins
- **Constraints**: Database-level validation
- **Indexes**: Performance optimization

### Schemas (`schemas/`)
- **Pydantic models**: Request/response validation
- **Type safety**: Runtime type checking
- **Serialization**: JSON conversion
- **Documentation**: Automatic API docs

### Model-Schema Relationship
```python
# SQLAlchemy Model (database)
class MediaFile(Base):
    __tablename__ = "media_file"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String, nullable=False)

# Pydantic Schema (API)
from pydantic import BaseModel
from pydantic import ConfigDict


class MediaFileResponse(BaseModel):
    id: int
    filename: str

    model_config = ConfigDict(from_attributes=True)  # Pydantic v2 ORM mode
```

## Background Tasks (`tasks/`)

### Organization
- **Modular design**: Complex tasks split into modules
- **Single responsibility**: Each task has one clear purpose
- **Error handling**: Robust error recovery with retry logic
- **Progress tracking**: Real-time status updates via `progress_tracker.py` (EWMA ETA)

### Transcription Pipeline (`tasks/transcription/`)
`services/pipeline_plan.py` decides, per file, which engines run (local GPU/CPU or cloud for transcription; ASR-provided, local, or cloud diarization), where, and on which queue; `tasks/transcription/dispatch.py` turns that plan into a Celery chain. The default local path is three stages (a separate `rediarize_task.py` runs speaker detection after the transcript is saved when the plan says so):

```
transcription/
├── preprocess.py        # Stage 1 (transcription.preprocess): download from MinIO, extract audio, metadata
├── core.py              # Stage 2 (transcription.gpu_transcribe): Whisper transcription (+ inline diarization when planned)
├── postprocess.py       # Stage 3 (transcription.postprocess): save, index, notify, dispatch enrichment
├── diarize_task.py      # Stage 2b: diarization only (gpu-split mode, gpu-diarize queue)
├── cpu_task.py          # Stage 2 alternative: CPU Whisper (full image only)
├── cloud_asr.py         # Stage 2 alternative: cloud ASR provider pipeline
├── dispatch.py          # Plan -> Celery chain orchestration
├── finalize.py, downstream.py, embeddings.py, speaker_processor.py  # Result persistence, speaker work, follow-on tasks
├── run_ownership.py, cancellation.py, context.py     # Superseded-run guards, user cancellation, shared failure handling
└── audio_processor.py, metadata_extractor.py, storage.py, notifications.py, waveform_generator.py
```

Postprocess does not hold the GPU for enrichment: speaker embedding, indexing, summarization and similar work is dispatched to other queues afterwards. Stage tasks hold a run lease so a worker lost mid-stage is detected (`core/broker_orphans.py`, `core/task_liveness.py`).

## 🛠️ Utilities (`utils/`)

### Common Patterns
- **Error handling**: Standardized HTTP exceptions
- **Authorization**: Reusable permission helpers and FastAPI dependencies
- **Database helpers**: Common query patterns
- **Task management**: Celery task utilities

### Usage Examples
```python
# Error handling
from app.utils.error_handlers import ErrorHandler
raise ErrorHandler.not_found_error("Resource")

# Authorization — a FastAPI dependency, never a decorator
from app.utils.uuid_helpers import get_file_by_uuid_with_permission
file_obj = get_file_by_uuid_with_permission(db, file_uuid, current_user.id)

# Database helpers
from app.utils.db_helpers import get_user_files_query
query = get_user_files_query(db, user_id)
```

## Core Configuration (`core/`)

### Components
- **config.py**: Environment-based settings (`settings`); `DEPLOYMENT_MODE=lite` selects the GPU-free deployment
- **security.py**: JWT and password hashing utilities
- **celery.py**: Celery app, queue declarations and `task_routes`
- **constants.py**: Language codes, `CeleryQueues`, task priorities, OpenSearch embedding model registry, system defaults
- **enums.py**: Centralized enums (`FileStatus`, `TranscriptionStage`, `PostProcessingTask`, `CollectionVisibility`, takedown enums, ...); `models/media.py` re-exports `FileStatus`
- **exceptions.py**: Custom exception hierarchy (`OpenTranscribeError` base class)
- **redis.py**: Shared Redis singleton via `get_redis()` - use this for all sync Redis access (db 0)
- **Operational helpers**: Prometheus metrics (`metrics.py`, `celery_metrics.py`, `worker_metrics.py`, `db_metrics.py`), task liveness / cancellation / replay (`task_liveness.py`, `task_cancellation.py`, `task_replay.py`, `broker_orphans.py`), and the tenancy and capability seams (`tenancy.py`, `tenant_limits.py`, `capabilities.py`)

Many settings that look like they need `.env` variables are DB-backed `SystemSettings` with coded defaults in `constants.py`, editable in the admin UI.

### Configuration Pattern
```python
from app.core.config import settings
from app.core.redis import get_redis
from app.core.enums import FileStatus
from app.core.exceptions import OpenTranscribeError

database_url = settings.DATABASE_URL
redis = get_redis()
```

## Authentication & Authorization

### Supported Authentication Methods
- **Local (Direct)**: Username/password (`direct_auth.py`)
- **LDAP/Active Directory**: Enterprise directory integration (`ldap_auth.py`)
- **OIDC**: OpenID Connect with any conforming identity provider (`auth/oidc/`)
- **SAML**: SAML 2.0 service provider (`auth/saml/`)
- **PKI/X.509**: Certificate-based authentication for high-security environments (`pki_auth.py`)
- **Trusted proxy**: Identity asserted by a reverse proxy (`auth/proxy/`)
- **MFA/TOTP**: RFC 6238 compliant; compatible with Google Authenticator, Authy, etc.
- **SCIM 2.0**: Provisioning API for identity providers (`api/endpoints/scim/`, bearer tokens via `auth/` + `services/scim_token_service.py`)

Methods run side by side; each user account has an `auth_type`, and each method is enabled or disabled individually (`local_enabled`, `ldap_enabled`, `oidc_enabled`, ...). Configure them in the Admin UI (Settings, Authentication); the database configuration takes precedence over `.env`. There is no `AUTH_TYPE` setting. Details: `auth/CLAUDE.md`.

### Token Flow
1. **Login**: Credentials -> short-lived JWT access token + long-lived refresh token
2. **Request**: Access token as an `Authorization: Bearer` header or the httpOnly `access_token` cookie (the browser SPA)
3. **Validation**: JWT signature and expiration
4. **Refresh**: Refresh token rotation on access token expiry

### Authorization Patterns
- **Ownership**: Users can only access their own resources
- **Role-based**: user / admin / super_admin roles (`auth/roles.py`)
- **Resource-specific**: File-level and collection-level access control, plus organization (tenant) scoping

## 🔄 Middleware (`middleware/`)

### Current Middleware
Registered in `main.py` (the last `add_middleware` call runs outermost):
- **Observability** (`observability.py`): request metrics and structured access log
- **CSRF** (`csrf.py`): double-submit cookie protection
- **Audit** (`audit.py`): request ID tracking and audit logging (FedRAMP AU-2/AU-3 compliance)
- **Robots** (`robots.py`): tells crawlers not to index API responses
- **CORS**: FastAPI's `CORSMiddleware`
- **Upload body limit** (`upload_limit.py`): enforces upload size while the body streams

Authentication itself is not middleware; it is a FastAPI dependency (`api/endpoints/auth/dependencies.py`).

## 📋 Database Initialization (`initial_data.py`)

### Purpose
- **Bootstrap data**: Create initial admin user
- **Development setup**: Ensure required data exists
- **Safe execution**: Idempotent operations (can run multiple times)

## 🚀 Application Entry Point (`main.py`)

### Application Setup
```python
# Simplified from main.py
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: validate secrets, run_migrations() (unless RUN_MIGRATIONS_ON_STARTUP=false),
    # seed initial data, warm services, start background threads
    yield
    # Shutdown: cleanup

app = FastAPI(title=settings.PROJECT_NAME, lifespan=lifespan, ...)
app.add_middleware(...)                                   # see Middleware above
app.include_router(api_router, prefix=settings.API_PREFIX)   # "/api"
app.include_router(metrics_router)                        # /metrics (root)
app.include_router(scim_router)                           # /scim/v2 (root)
```
`/health` and `/health/ready` are defined directly on the app.

## 📖 Development Guidelines

### Adding New Features

1. **Models**: Define database structure in `models/`
2. **Schemas**: Create validation models in `schemas/`
3. **Services**: Implement business logic in `services/`
4. **Endpoints**: Add API routes in `api/endpoints/`
5. **Tests**: Add comprehensive tests

### Code Organization Rules

1. **Keep endpoints thin**: Move logic to services
2. **Use dependency injection**: FastAPI's `Depends()`
3. **Handle errors gracefully**: Use utility error handlers
4. **Document everything**: Docstrings and type hints
5. **Follow patterns**: Consistency across codebase

### Import Organization
Ruff's isort rules (`force-single-line`) order and format imports: one imported name per line.
```python
# Standard library
import os

# Third-party
from fastapi import Depends
from fastapi import FastAPI
from sqlalchemy.orm import Session

# Local application
from app.core.config import settings
from app.models.user import User
from app.schemas.user import UserCreate
```

---

This architecture provides a solid foundation for scalable, maintainable FastAPI applications with clear separation of concerns and modern Python practices.
