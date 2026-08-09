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

```
app/
├── api/                    # API Layer
│   ├── endpoints/         # Route handlers organized by resource
│   │   ├── files/        # Modular file management
│   │   │   ├── streaming.py       # Video/audio streaming
│   │   │   ├── upload.py          # File upload with concurrency
│   │   │   ├── url_processing.py  # yt-dlp URL processing
│   │   │   ├── crud.py            # Basic CRUD
│   │   │   ├── management.py      # File recovery/force-delete
│   │   │   ├── filtering.py       # Advanced filtering
│   │   │   ├── reprocess.py       # Selective reprocess
│   │   │   ├── subtitles.py       # Subtitle export
│   │   │   └── waveform.py        # Waveform data
│   │   ├── admin.py               # Admin operations
│   │   ├── asr_settings.py        # ASR provider + local model management
│   │   ├── auth.py                # Authentication (all methods)
│   │   ├── comments.py            # Comment system
│   │   ├── groups.py              # User group management
│   │   ├── llm_settings.py        # User LLM configuration
│   │   ├── media_collections.py   # Collection sharing
│   │   ├── search.py              # Hybrid BM25+neural search
│   │   ├── speakers.py            # Speaker management and merging
│   │   ├── speaker_profiles.py    # Global speaker profiles
│   │   ├── summarization.py       # LLM-powered summarization
│   │   ├── tags.py                # Tag operations
│   │   ├── tasks.py               # Task monitoring
│   │   ├── user_settings.py       # User settings management
│   │   └── users.py               # User management
│   ├── router.py         # Main API router configuration
│   └── websockets.py     # Real-time WebSocket handlers
├── auth/                  # Authentication & Authorization
│   ├── direct_auth.py    # Local password auth
│   ├── ldap_auth.py      # LDAP/Active Directory
│   ├── oidc/            # OpenID Connect (config, discovery, flow, claims, provisioning)
│   ├── pki_auth.py       # PKI/X.509 certificate auth
│   ├── mfa.py            # TOTP multi-factor auth (RFC 6238)
│   ├── password_policy.py # Password strength enforcement
│   ├── rate_limit.py     # Per-IP/per-user rate limiting
│   ├── lockout.py        # Account lockout management
│   ├── session.py        # Session/token management
│   ├── token_service.py  # JWT token operations
│   └── audit.py          # Authentication audit logging
├── core/                  # Core Configuration
│   ├── celery.py         # Celery app + task routing
│   ├── config.py         # Application settings (DEPLOYMENT_MODE, etc.)
│   ├── constants.py      # Language constants, OpenSearch model registry
│   ├── enums.py          # Centralized FileStatus enum (re-exported from models)
│   ├── exceptions.py     # Custom exception hierarchy (OpenTranscribeError base)
│   ├── redis.py          # Shared Redis singleton via get_redis()
│   └── security.py       # JWT and password hashing utilities
├── db/                    # Database Layer
│   ├── base.py           # SQLAlchemy engine, SessionLocal, Base
│   ├── migrations.py     # Startup Alembic runner + version detection
│   └── session_utils.py  # session_scope(), get_refreshed_object()
├── middleware/            # Request/Response Middleware
│   └── audit.py          # Request ID tracking and audit logging
├── models/                # SQLAlchemy ORM Models (~20 files)
├── schemas/               # Pydantic Validation Schemas
├── services/              # Business Logic Layer (~40 modules)
│   ├── interfaces.py          # Protocol interfaces (Storage, Search, Cache, Notification)
│   ├── notification_service.py # Unified send_task_notification() wrapper
│   ├── progress_tracker.py    # EWMA ETA preferred progress tracker
│   ├── migration_progress_service.py  # Atomic Lua increments for concurrent batch migrations
│   ├── opensearch_service.py  # Full-text + neural search, speaker alias indices
│   ├── hybrid_search_service.py  # Hybrid BM25+vector search (OS 3.4 crash fix)
│   ├── profile_embedding_service.py  # Profile centroid management (averaging fix)
│   ├── speaker_matching_service.py   # Cosine score conversion from OS cosinesimil
│   ├── asr/               # ASR service + 8 cloud provider clients
│   └── ...
├── tasks/                 # Background Processing
│   ├── transcription/    # 3-stage pipeline
│   │   ├── preprocess.py         # Stage 1: download, extract audio
│   │   ├── core.py               # Stage 2 orchestrator (GPU)
│   │   ├── postprocess.py        # Stage 3: index, notify, enrich
│   │   ├── dispatch.py           # Task dispatch helpers
│   │   ├── audio_processor.py    # Audio conversion/extraction
│   │   ├── metadata_extractor.py # ExifTool metadata extraction
│   │   ├── speaker_processor.py  # Speaker diarization processing
│   │   ├── storage.py            # Database storage utilities
│   │   ├── notifications.py      # WebSocket notifications
│   │   └── waveform_generator.py # Waveform data generation
│   ├── speaker_tasks.py          # Thin re-export module
│   ├── speaker_identification_task.py  # LLM speaker ID
│   ├── speaker_update_task.py    # Background speaker updates
│   ├── speaker_embedding_task.py # Embedding extraction + reassignment
│   ├── reindex_task.py           # Search reindex with stop/cancel
│   ├── file_retention_task.py    # Auto-deletion by retention policy
│   ├── summarization.py          # Multi-provider LLM summarization
│   ├── analytics.py              # Analytics processing
│   ├── cleanup.py                # Stuck file recovery
│   └── youtube_processing.py     # yt-dlp URL processing
├── utils/                 # Common Utilities (~25 modules)
│   ├── auth_decorators.py        # Authorization decorators
│   ├── db_helpers.py             # Database query helpers
│   ├── error_handlers.py         # Standardized HTTP exceptions
│   ├── transcript_builders.py    # Shared transcript formatting (v0.4.0)
│   ├── task_utils.py             # Celery task management utilities
│   └── ...
├── main.py               # FastAPI Application Entry Point
└── initial_data.py       # Database Initialization (admin user, defaults)
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

### Files Module (`api/endpoints/files/`)
Special modular organization for complex file operations:
- `upload.py` - Enhanced file upload processing with concurrency control
- `crud.py` - Basic CRUD operations
- `filtering.py` - Advanced filtering logic
- `streaming.py` - Video/audio streaming support
- `url_processing.py` - **NEW**: Enhanced URL processing for video links

### New API Endpoints
- **User Settings (`user_settings.py`)**:
  - Recording preferences management
  - User-specific configuration storage
  - Settings validation and defaults
- **Summarization (`summarization.py`)**:
  - Multi-provider LLM integration
  - BLUF format summary generation
  - Intelligent section processing

## 🔧 Service Layer (`services/`)

### Purpose
- **Business logic encapsulation**: Keep endpoints thin
- **Reusable operations**: Share logic between endpoints
- **Transaction management**: Handle database transactions
- **External service integration**: Manage third-party APIs

### Service Pattern
```python
class ResourceService:
    def __init__(self, db: Session):
        self.db = db

    def create(self, data: CreateSchema, user: User) -> Resource:
        """Create a new resource with business logic."""
        # Validation
        # Business rules
        # Database operations
        # Return result
```

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
class MediaFileResponse(BaseModel):
    id: int
    filename: str

    class Config:
        from_attributes = True  # Enable ORM mode
```

## Background Tasks (`tasks/`)

### Organization
- **Modular design**: Complex tasks split into modules
- **Single responsibility**: Each task has one clear purpose
- **Error handling**: Robust error recovery with retry logic
- **Progress tracking**: Real-time status updates via `progress_tracker.py` (EWMA ETA)

### 3-Stage Transcription Pipeline (`tasks/transcription/`)
```
transcription/
├── preprocess.py        # Stage 1: download from MinIO, extract audio
├── core.py              # Stage 2: GPU transcription + speaker diarization
├── postprocess.py       # Stage 3: index, notify, async enrichment dispatch
├── dispatch.py          # Task routing helpers
├── audio_processor.py   # FFmpeg audio conversion
├── metadata_extractor.py # ExifTool media metadata
├── speaker_processor.py # Speaker segment processing
├── storage.py           # Database storage
├── notifications.py     # WebSocket updates via notification_service
└── waveform_generator.py # Waveform data
```

Postprocess no longer blocks GPU for enrichment — speaker embedding and LLM tasks are dispatched asynchronously.

## 🛠️ Utilities (`utils/`)

### Common Patterns
- **Error handling**: Standardized HTTP exceptions
- **Authorization**: Reusable permission decorators
- **Database helpers**: Common query patterns
- **Task management**: Celery task utilities

### Usage Examples
```python
# Error handling
from app.utils.error_handlers import ErrorHandler
raise ErrorHandler.not_found_error("Resource")

# Authorization
from app.utils.auth_decorators import require_file_ownership
@require_file_ownership
def update_file(...): pass

# Database helpers
from app.utils.db_helpers import get_user_files_query
query = get_user_files_query(db, user_id)
```

## Core Configuration (`core/`)

### Components
- **config.py**: Environment-based settings (supports `DEPLOYMENT_MODE=lite` for GPU-free deployments)
- **security.py**: JWT and password hashing utilities
- **celery.py**: Celery app and task routing configuration
- **constants.py**: Language codes, OpenSearch embedding model registry, system defaults
- **enums.py**: Centralized `FileStatus` enum (re-exported from `models/media.py` for compatibility)
- **exceptions.py**: Custom exception hierarchy (`OpenTranscribeError` base class)
- **redis.py**: Shared Redis singleton via `get_redis()` — use this for all sync Redis access (db 0)

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
- **Local (Direct)**: Username/password with bcrypt hashing
- **LDAP/Active Directory**: Enterprise directory integration (`ldap_auth.py`)
- **OIDC**: OpenID Connect with any conforming identity provider (`auth/oidc/`)
- **PKI/X.509**: Certificate-based authentication for high-security environments (`pki_auth.py`)
- **MFA/TOTP**: RFC 6238 compliant; compatible with Google Authenticator, Authy, etc.

Multiple methods can be enabled simultaneously (hybrid auth) via `AUTH_TYPE` config. Configure via Super Admin UI.

### Token Flow
1. **Login**: Credentials → short-lived JWT access token + long-lived refresh token
2. **Request**: Bearer token in Authorization header
3. **Validation**: JWT signature and expiration
4. **Refresh**: Refresh token rotation on access token expiry

### Authorization Patterns
- **Ownership**: Users can only access their own resources
- **Role-based**: User / admin / super_admin roles
- **Resource-specific**: File-level and collection-level access control

## 🔄 Middleware (`middleware/`)

### Current Middleware
- **Audit**: Request ID tracking and audit logging (FedRAMP AU-2/AU-3 compliance)
- **CORS**: Cross-origin request handling (FastAPI built-in)
- **Authentication**: JWT token validation (dependency injection)

## 📋 Database Initialization (`initial_data.py`)

### Purpose
- **Bootstrap data**: Create initial admin user
- **Development setup**: Ensure required data exists
- **Safe execution**: Idempotent operations (can run multiple times)

## 🚀 Application Entry Point (`main.py`)

### Application Setup
```python
# FastAPI app creation
app = FastAPI(title="OpenTranscribe API")

# Middleware registration
app.add_middleware(...)

# Router inclusion
app.include_router(api_router, prefix="/api")

# Startup events
@app.on_event("startup")
async def startup_event():
    # Initialize services
    pass
```

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
```python
# Standard library
import os
from typing import List

# Third-party
from fastapi import FastAPI, Depends
from sqlalchemy.orm import Session

# Local application
from app.core.config import settings
from app.models.user import User
from app.schemas.user import UserCreate
```

---

This architecture provides a solid foundation for scalable, maintainable FastAPI applications with clear separation of concerns and modern Python practices.
