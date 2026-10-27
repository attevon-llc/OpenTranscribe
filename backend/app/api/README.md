<div align="center">
  <img src="../../../assets/logo-banner.png" alt="OpenTranscribe Logo" width="250">

  # API Layer Documentation
</div>

This directory contains the FastAPI application's API layer, organized by resource and functionality.

## 🌐 API Architecture

### Design Principles
- **RESTful conventions**: Standard HTTP methods and status codes
- **UUID identifiers**: path parameters are UUIDs (`/{file_uuid}`), never database integers
- **Resource-based routing**: URLs represent resources, not actions
- **Consistent errors**: FastAPI's standard `{"detail": ...}` body; routes return Pydantic response models
- **OpenAPI integration**: Automatic documentation generation
- **Type safety**: Pydantic schemas for request/response validation

## 📁 Directory Structure

```
api/
├── endpoints/              # API route handlers
│   ├── auth/              # Auth package: login, registration, profile, MFA, OIDC, SAML, PKI,
│   │                      #   proxy, sessions, invitations, email verification; dependencies.py holds
│   │                      #   get_current_user / get_current_active_user / get_current_admin_user
│   ├── chat/              # RAG chat: conversations, messages, projects, export, user/admin settings
│   ├── files/             # File package (see below)
│   ├── scim/              # SCIM 2.0 provisioning (mounted at /scim/v2, outside /api)
│   ├── tags/              # Tag CRUD, discovery, operations, sharing
│   ├── admin*.py          # Platform admin: users/stats (admin.py), approvals, SCIM tokens, HF token,
│   │                      #   group mappings, storage integrity, takedown notices, timing, content-access audit
│   ├── org_admin*.py      # Organization admin: audit logs, legal holds, takedown/quarantine, GDPR erase
│   ├── support_access.py  # Support-access grants
│   ├── asr_settings.py, asr_local_model.py, asr_provider_policy.py, local_models.py   # ASR config, local model, provider policy, model library
│   ├── llm_settings.py, llm_status.py, bedrock_settings.py                            # LLM configuration and status
│   ├── engine_settings.py, diarization_settings.py, pipeline_settings.py              # Engine/diarizer/pipeline settings
│   ├── user_settings.py, redaction_settings.py, custom_vocabulary.py                  # Per-user preferences
│   ├── backup_settings.py, media_mirror_settings.py, directory_sync_settings.py, auth_config.py, auth_email_delivery.py, first_run_wizard.py
│   ├── search.py, embedding_migration.py, speaker_attribute_migration.py, combined_speaker_migration.py
│   ├── speakers.py, speaker_profiles.py, speaker_clusters.py, speaker_update.py, transcript_segments.py
│   ├── comments.py, media_collections.py, groups.py, prompts.py, summarization.py, topics.py
│   ├── tasks.py, user_files.py, users.py, usage.py, watch_sources.py, legal.py, takedown_notices_user.py
│   ├── system.py, version.py, metrics.py
│   └── ...                # see router.py for the full mount list
├── deps_context.py        # Tenant-aware request context (get_current_context, require_org_admin)
├── router.py              # Mounts every router (include_router_with_consistency)
└── websockets.py          # Real-time WebSocket handler
```

`endpoints/files/` modules: `prepare_upload`, `upload`, `multipart`, `complete_upload`, `cancel_upload` (upload flows), `crud`, `filtering`, `management` (recovery, force-delete, bulk ops), `streaming` (thumbnail streaming helper; playback uses presigned URLs from `/files/{uuid}/stream-url`), `url_processing` (yt-dlp), `reprocess`, `post_processing`, `segments`, `waveform`, `captions`, `subtitles`, `transcript_export`, `transcript_document_export` (DOCX/PDF), `summary_status`.

## 🛠️ Endpoint Organization

### Standard Endpoint Pattern
Each endpoint module follows consistent patterns:

```python
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_active_user
from app.db.base import get_db
from app.models.user import User

router = APIRouter()


@router.get("/{resource_uuid}", response_model=ResourceSchema)
def get_resource(
    resource_uuid: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Get a specific resource by UUID."""
    # Resolve through a permission-aware helper (e.g. get_file_by_uuid_with_permission)
    # and return the Pydantic model; let HTTPException carry errors.
```

Routes are mounted in `router.py` with `include_router_with_consistency(router, prefix="/things", tags=["things"])`, which also normalizes trailing-slash handling for nginx and can attach a capability gate. Endpoints that need the tenant (organization) use `Depends(get_current_context)` from `deps_context.py` instead of the bare user.

## 📋 API Endpoints Reference

`backend/openapi.json` (also served at `/api/docs` on non-hardened deployments) is the authoritative route list; the tables below are a guide to the main groups, not an inventory. All paths are under `/api` unless noted.

### Authentication (`endpoints/auth/`, `auth_config.py`)
```
POST   /auth/login, /auth/token         # Login (cookie session / token), all enabled methods
POST   /auth/register                   # Self-registration (when enabled)
POST   /auth/token/refresh              # Refresh token rotation
POST   /auth/logout, /auth/logout/all   # Revoke this session / all sessions
GET    /auth/me                         # Current user
GET    /auth/session                    # SPA session probe (200 for anonymous, never 401)
GET    /auth/methods                    # Available auth methods
GET    /auth/password-policy            # Password requirements
POST   /auth/mfa/setup, /verify-setup, /verify, /disable;  GET /auth/mfa/status
GET    /auth/oidc/login, /auth/oidc/callback
GET    /auth/saml/login, /auth/saml/metadata;  POST /auth/saml/acs
POST   /auth/pki/authenticate, /auth/proxy/authenticate
POST   /auth/password-reset/request, /confirm
POST   /auth/verify-email, /verify-email/resend
GET/POST /auth/invitations; POST /auth/invitations/accept
```

### Files (`files/`)
```
GET    /files                                  # List (paged: page, page_size; filters: search, tag, speaker, status, ...)
POST   /files                                  # Upload (multipart form)
POST   /files/prepare, /files/complete         # Presigned upload flow (+ /files/multipart/parts)
POST   /files/process-url                      # yt-dlp URL ingestion
GET    /files/{file_uuid}, /info               # Detail / lightweight info
PUT    /files/{file_uuid}                      # Update metadata
DELETE /files/{file_uuid}                      # Delete
GET    /files/{file_uuid}/stream-url           # Presigned playback URL
GET    /files/{file_uuid}/segments             # Transcript segments
PUT    /files/{file_uuid}/transcript/segments/{segment_uuid}
POST   /files/{file_uuid}/reprocess            # Re-run with different options
GET    /files/{file_uuid}/summary              # Summary (POST /summarize to generate, /retry-summary)
GET    /files/{file_uuid}/export               # Transcript export (txt/json/csv/srt/vtt/md);  POST /export/document for DOCX/PDF
GET    /files/{file_uuid}/subtitles, /captions # Subtitle download / player captions
GET    /files/{file_uuid}/waveform, /analytics
GET    /files/{file_uuid}/status-detail, /post-processing, /summary-status
```

### File Recovery and Management (`files/management.py`, `user_files.py`, `tasks.py`)
```
POST   /files/{file_uuid}/cancel, /retry, /recover   # Cancel / retry / recover a file
DELETE /files/{file_uuid}/force                      # Force delete (admin)
GET    /files/management/stuck                       # Stuck files
POST   /files/management/bulk-action                 # Bulk delete / retry / cancel / recover
POST   /files/management/cleanup-orphaned            # Recover stuck files in bulk (admin; NOT orphan cleanup - that is POST /admin/data-integrity)
POST   /my-files/request-recovery;  GET /my-files/status
```

File processing states (`FileStatus`, `app/core/enums.py`): `pending`, `queued`, `downloading`, `processing`, `completed`, `error`, `cancelling`, `cancelled`, `orphaned`, `quarantined`.

`last_error_message` in status responses is a fixed, user-facing sentence from `ErrorCategorizationService`, never the raw exception text (the raw message is logged server-side with the file's UUID).

### Users (`users.py`)
```
GET    /users, /users/search       # List / search users
GET    /users/me;  PUT /users/me   # Current user profile / update
GET/PUT/DELETE /users/{user_uuid}  # Admin-gated
```
Admin user administration (create, role, lock/unlock, MFA reset, password reset, sessions) lives under `/admin/users`.

### Comments, Tags, Collections, Groups
```
/comments                          # CRUD + /comments/files/{file_uuid}/comments
/tags                              # list/create; PATCH /{tag_uuid}; /{tag_uuid}/merge, /files, /shares; /impact, /collisions, /unused, /cleanup, /promote
/collections                       # CRUD; /{uuid}/media, /{uuid}/shares, PATCH /{uuid}/visibility, /shared-with-me
/groups                            # CRUD + /{group_uuid}/members
```

### Speakers
```
/speakers                          # list/create; /{speaker_uuid} CRUD; /verify, /confirm-gender, /cross-media; POST /{uuid}/merge/{target_uuid}
/speaker-profiles                  # Cross-file speaker profiles (/profiles, /speakers, /collections)
/speaker-clusters                  # Clustering, batch-verify, recluster, stats
PUT    /transcripts/segments/{segment_uuid}/speaker   # Reassign a segment's speaker
```

### Tasks (`tasks.py`)
```
GET    /tasks;  GET /tasks/{task_id}        # List / task status (reads the task table)
GET    /tasks/progress/active               # Live per-stage progress (Redis)
POST   /tasks/retry/{file_uuid}
POST   /tasks/system/fix-file/{file_uuid}, /recover-task/{task_id}, /recover-all-user-files, /startup-recovery
GET    /tasks/system/health
```

### Search (`search.py`)
```
GET    /search, /search/count, /search/filters, /search/suggestions   # Hybrid BM25 + neural search
POST   /search/reindex;  GET /search/reindex/status;  POST /search/reindex/stop
GET    /search/index-health;  POST /search/repair-indices, /search/reembed-degraded
GET/POST /search/models, /search/models/neural/...                    # Neural model register/deploy/activate (admin)
```

### User Settings (`user_settings.py` and siblings)
```
GET/PUT/DELETE /user-settings/recording, /transcription, /download, /redaction, /chat, /speaker-attributes,
               /organization-context, /diarization/pyannote
GET/PUT        /user-settings/ai-summary, /audio-extraction, /auto-label
GET            /user-settings/pipeline          # How the next recording would be processed
GET            /user-settings/all               # All settings (debug)
GET/POST       /user-settings/media-sources
```
Also `GET /user-settings/transcription/system-defaults`, `/download/system-defaults` and `/redaction/defaults`.

### ASR, LLM and Prompts
```
/asr-settings                      # Provider configs, set-active/clear-active, /providers, /local-model/{active,set,restart}, /local-models
/admin/asr-providers               # Cloud ASR provider policy (super_admin)
/admin/local-models                # Local Whisper model library (super_admin)
/llm-settings, /llm                # Per-user LLM configuration, provider status, connection test
/prompts                           # Summary prompts (system, user, shared)
```

### Chat (`endpoints/chat/`)
```
/chat/conversations, /chat/messages, /chat/projects, /chat/context, /chat/answer-budget
/user-settings/chat;  /admin/chat-settings
```

### Admin and Organization Admin
```
/admin/users, /admin/stats, /admin/files/quarantined, /admin/data-integrity, /admin/profile-embeddings/repair
/admin/auth-config, /admin/backup, /admin/engine-settings, /admin/settings/retention-config, /admin/scim-tokens, /admin/takedown-notices, ...
/org-admin/audit-logs, /org-admin/holds, /org-admin/files, /org-admin/gdpr, /org-admin/support-access
/support-access/grants, /takedown-notices/mine
```
Compliance and operations routes (legal holds, takedown, GDPR erase, data integrity) have subtle authority rules; read `app/api/CLAUDE.md` before changing them.

### Other
```
/watch-sources            # Auto-import sources (local / S3 / SMB)
/custom-vocabulary        # Per-user vocabulary terms
/embeddings/migration     # Speaker embedding migration
/usage/me, /legal/..., /version
/system/stats, /system/capabilities
```

## 🔐 Authentication & Authorization

### Authentication Patterns
All protected endpoints use dependency injection:

```python
current_user: User = Depends(get_current_active_user)
```

### Authorization Levels
1. **Public**: No authentication required (a handful of routes, e.g. login, `/health`)
2. **User**: Valid credential (Bearer token or httpOnly cookie) via `get_current_active_user`
3. **Owner / shared**: Resolved through permission-aware helpers (`get_file_by_uuid_with_permission`, `PermissionService`)
4. **Admin**: `get_current_admin_user`; **super_admin**: `get_current_active_superuser`
5. **Organization admin**: `require_org_admin` from `deps_context.py`

### Example Authorization Patterns
```python
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.auth import get_current_admin_user
from app.utils.uuid_helpers import get_file_by_uuid_with_permission

# User must own (or be shared) the file
@router.get("/files/{file_uuid}/thing")
def get_thing(
    file_uuid: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    file_obj = get_file_by_uuid_with_permission(db, file_uuid, current_user.id)
    ...

# Admin only
@router.get("/admin/things")
def list_things(current_user: User = Depends(get_current_admin_user)):
    ...
```

Use `get_current_active_user`, not `get_current_user`, on ordinary routes: only the former applies the account-lifecycle gate (deactivated, unapproved, expired, forced password change).

## 📝 Request/Response Patterns

Routes declare a Pydantic `response_model` and return it directly; there is no shared success envelope. Responses carry pre-formatted display fields (`formatted_duration`, `display_status`, ...) built by `services/formatting_service.py`.

### Paginated Lists
List routes that paginate use `page` / `page_size` query parameters and return a paginated response model (for example `PaginatedMediaFileResponse` for `GET /files`).

### Error Response
```json
{
  "detail": "Resource not found"
}
```
Some routes return a structured `detail` such as `{"code": "...", "message": "..."}` (for example the pipeline-plan 409s); the codes are stable, the messages are fixed sentences.

### Validation Error Response (422)
```json
{
  "detail": [
    {
      "loc": ["body", "email"],
      "msg": "Field required",
      "type": "missing"
    }
  ]
}
```
Validation errors are scrubbed (`app/core/validation_errors.py`) so a rejected credential is never echoed back.

## 🔄 WebSocket Integration (`websockets.py`)

### Real-time Updates
WebSockets provide real-time notifications for:
- **Task progress**: Transcription, analysis, summarization progress updates
- **File status**: Processing status changes with detailed progress tracking
- **Upload progress**: Real-time upload status with concurrent file processing
- **Summarization progress**: AI processing status with section-by-section updates
- **System notifications**: Admin alerts and user-specific notifications
- **Recording status**: Live recording progress and session management

### Connecting
The endpoint is **`/api/ws`** (the router declares `/ws`; `main.py` mounts it under `/api`). The handshake is rejected with close code 4403 when its `Origin` is neither same-origin nor in `CORS_ORIGINS`. After the connection is accepted, the server authenticates from the `access_token` cookie, or, if there is none, waits up to 10 seconds for a first message `{"type": "authenticate", "token": "<jwt>"}`; failures close with 4001/4002/4003. Server code publishes events with `app/utils/websocket_notify.py` (`send_ws_event`, or `send_ws_event_for_file` when the event names a file) over Redis pub/sub.

### Client Usage
```javascript
const ws = new WebSocket('ws://localhost:5174/api/ws');
ws.onopen = () => ws.send(JSON.stringify({ type: 'authenticate', token }));  // not needed with the cookie
ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    // Handle real-time updates
};
```

## 🛡️ Error Handling

### Standard HTTP Status Codes
- **200**: Success
- **201**: Created
- **204**: No Content (deletes)
- **400**: Bad Request (validation errors)
- **401**: Unauthorized (authentication required)
- **403**: Forbidden (insufficient permissions)
- **404**: Not Found
- **422**: Unprocessable Entity (validation failed)
- **500**: Internal Server Error

### Custom Error Handlers
Endpoints raise `fastapi.HTTPException` (directly or via the helpers below); `app/core/exceptions.py` is for the service layer, and `main.py` maps those exceptions to HTTP statuses.
```python
from app.utils.error_handlers import ErrorHandler

raise ErrorHandler.not_found_error("File")
raise ErrorHandler.validation_error("Invalid email format")
raise ErrorHandler.unauthorized_error("Access denied")
```

## 📊 Query Parameters & Filtering

### Common Query Parameters
```python
# Pagination
page: int = Query(1, ge=1)
page_size: int = Query(20, ge=1, le=100)

# Filtering
search: str | None = None
from_date: datetime | None = None
to_date: datetime | None = None

# Multiple values
tag: list[str] | None = Query(None)
status: list[str] | None = Query(None)
```

### Advanced Filtering Example
```python
@router.get("/files")
def list_files(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str | None = None,
    tag: list[str] | None = Query(None),
    speaker: list[str] | None = Query(None),
    from_date: datetime | None = None,
    to_date: datetime | None = None,
    min_duration: float | None = None,
    max_duration: float | None = None,
    file_type: list[str] | None = Query(None),
    status: list[str] | None = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """List files with advanced filtering options."""
    pass
```

## 🧪 Testing Endpoints

### Test Structure
Endpoint tests live in `backend/tests/api/` (and `tests/api/endpoints/`), using the shared fixtures in `backend/tests/conftest.py`:
```python
def test_create_resource(client, auth_headers):
    response = client.post(
        "/api/resources",
        json={"name": "Test Resource"},
        headers=auth_headers,
    )
    assert response.status_code == 201
    assert response.json()["name"] == "Test Resource"
```

### Testing Utilities
- **Test client**: `client` fixture (FastAPI TestClient)
- **Auth headers**: `auth_headers` / `admin_token_headers` fixtures
- **Database**: `db_session` fixture against the live dev stack's PostgreSQL
- Run with `./scripts/run-backend-tests.sh tests/api`; see `backend/README.md` and `backend/tests/CLAUDE.md`

## 📈 Performance Considerations

### Async Endpoints
```python
@router.get("/async-endpoint")
async def async_operation(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """Async endpoint for I/O bound operations."""
    # Use async for I/O operations
    await some_async_operation()

    # Use background tasks for fire-and-forget
    background_tasks.add_task(send_notification)

    return {"status": "success"}
```

### Database Optimization
- **Eager loading**: Use `joinedload()` for related data
- **Pagination**: Implement for large datasets
- **Caching**: Cache frequently accessed data
- **Indexing**: Ensure proper database indexes

## 🔧 Adding New Endpoints

### Step-by-Step Guide

1. **Create/Update Model** (`app/models/`, exported from `models/__init__.py`) and add the matching idempotent Alembic revision (checklist in `app/db/CLAUDE.md`)
   ```python
   class NewResource(Base):
       __tablename__ = "new_resource"
       id: Mapped[int] = mapped_column(Integer, primary_key=True)
       uuid: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True), default=uuid7, unique=True, index=True)  # the API identifier
       name: Mapped[str] = mapped_column(String, nullable=False)
   ```

2. **Create Schemas** (`app/schemas/`)
   ```python
   class NewResourceBase(BaseModel):
       name: str

   class NewResourceCreate(NewResourceBase):
       pass

   class NewResourceResponse(NewResourceBase):
       id: int
       created_at: datetime
   ```

3. **Create Service** (`app/services/`)
   ```python
   def create_new_resource(db: Session, data: NewResourceCreate, user: User) -> NewResource:
       # Business logic here
       ...
   ```

4. **Create Endpoints** (`app/api/endpoints/`)
   ```python
   @router.post("/", response_model=NewResourceResponse)
   def create_resource(resource: NewResourceCreate, ...):
       return service.create(db, resource)
   ```

5. **Add to Router** (`app/api/router.py`)
   ```python
   from .endpoints import new_resource
   include_router_with_consistency(new_resource.router, prefix="/new-resources", tags=["new-resources"])
   ```

6. **Add Tests** (`tests/api/endpoints/`)
   ```python
   def test_create_new_resource(client, auth_headers):
       # Test implementation
       pass
   ```

7. **Refresh the API snapshot**: `backend/venv/bin/python scripts/generate-openapi.py --write` (CI fails when `backend/openapi.json` is stale)

---

This API layer provides a robust, scalable foundation for the OpenTranscribe application with clear patterns, comprehensive documentation, and modern FastAPI practices.
