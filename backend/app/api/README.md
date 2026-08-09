<div align="center">
  <img src="../../../assets/logo-banner.png" alt="OpenTranscribe Logo" width="250">

  # API Layer Documentation
</div>

This directory contains the FastAPI application's API layer, organized by resource and functionality.

## 🌐 API Architecture

### Design Principles
- **RESTful conventions**: Standard HTTP methods and status codes
- **Resource-based routing**: URLs represent resources, not actions
- **Consistent responses**: Standardized success and error formats
- **OpenAPI integration**: Automatic documentation generation
- **Type safety**: Pydantic schemas for request/response validation

## 📁 Directory Structure

```
api/
├── endpoints/              # API route handlers
│   ├── files/             # Modular file management
│   │   ├── upload.py          # File upload with concurrency
│   │   ├── crud.py            # Basic CRUD
│   │   ├── management.py      # File recovery, force-delete, bulk ops
│   │   ├── filtering.py       # Advanced filtering
│   │   ├── streaming.py       # Video/audio streaming with range support
│   │   ├── url_processing.py  # yt-dlp URL download processing
│   │   ├── reprocess.py       # Selective reprocess (model, diarization)
│   │   ├── subtitles.py       # SRT/VTT subtitle export
│   │   ├── waveform.py        # Waveform data endpoints
│   │   ├── prepare_upload.py  # Pre-upload session creation
│   │   ├── cancel_upload.py   # Upload cancellation
│   │   └── summary_status.py  # Per-file summary status
│   ├── admin.py               # Admin operations + /admin/profile-embeddings/repair
│   ├── asr_settings.py        # ASR provider management + local model set/restart
│   ├── auth.py                # Authentication (all 4 methods + MFA + lockout + password policy)
│   ├── auth_config.py         # Auth provider configuration (LDAP/OIDC/PKI settings)
│   ├── comments.py            # Comment system
│   ├── custom_vocabulary.py   # Per-user custom vocabulary
│   ├── embedding_migration.py # Speaker embedding migration endpoints
│   ├── groups.py              # User group management
│   ├── llm_settings.py        # User LLM configuration
│   ├── llm_status.py          # LLM provider status
│   ├── media_collections.py   # Collection sharing with permissions
│   ├── prompts.py             # Shared/custom prompt management
│   ├── search.py              # Hybrid BM25+neural search
│   ├── speakers.py            # Speaker management, merge, cross-video matching
│   ├── speaker_profiles.py    # Global speaker profile management
│   ├── speaker_clusters.py    # Speaker cluster management
│   ├── speaker_attribute_migration.py # Speaker attribute migration
│   ├── speaker_update.py      # Background speaker updates
│   ├── summarization.py       # LLM-powered summarization
│   ├── system.py              # System statistics
│   ├── tags.py                # Tag operations
│   ├── tasks.py               # Task monitoring
│   ├── topics.py              # LLM-extracted topics
│   ├── transcript_segments.py # Transcript segment editing
│   ├── user_files.py          # User file operations
│   ├── user_settings.py       # User settings management
│   └── users.py               # User management
├── router.py              # Main API router configuration
└── websockets.py          # Real-time WebSocket handlers
```

## 🛠️ Endpoint Organization

### Standard Endpoint Pattern
Each endpoint module follows consistent patterns:

```python
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.models.user import User
from app.api.endpoints.auth import get_current_active_user

router = APIRouter()

@router.get("/", response_model=List[ResourceSchema])
def list_resources(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """List all resources for the current user."""
    pass

@router.post("/", response_model=ResourceSchema)
def create_resource(
    resource: ResourceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Create a new resource."""
    pass

@router.get("/{resource_id}", response_model=ResourceSchema)
def get_resource(
    resource_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Get a specific resource by ID."""
    pass

@router.put("/{resource_id}", response_model=ResourceSchema)
def update_resource(
    resource_id: int,
    resource: ResourceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Update a specific resource."""
    pass

@router.delete("/{resource_id}", status_code=204)
def delete_resource(
    resource_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """Delete a specific resource."""
    pass
```

## 📋 API Endpoints Reference

### Authentication (`auth.py`, `auth_config.py`)
```
POST   /auth/login              # User login (all methods)
POST   /auth/register           # User registration
POST   /auth/refresh            # Token refresh (rotation)
GET    /auth/me                 # Current user info
GET    /auth/methods            # Available auth methods
POST   /auth/mfa/setup          # Setup TOTP MFA
POST   /auth/mfa/verify         # Verify MFA code
DELETE /auth/mfa/disable        # Disable MFA
GET    /auth/password-policy    # Password requirements
POST   /auth/lockout/clear      # Clear account lockout (admin)
```

### Files (`files/`)
```
GET    /files               # List user files (with filtering)
POST   /files               # Upload new file
GET    /files/{id}          # Get file details
PUT    /files/{id}          # Update file metadata
DELETE /files/{id}          # Delete file
GET    /files/{id}/content  # Download file content
GET    /files/{id}/video    # Stream video (no auth)
GET    /files/{id}/stream-url # Get streaming URL
PUT    /files/{id}/transcript # Update transcript
```

### Enhanced File Management (`files/management.py`)
New endpoints for file recovery, error handling, and system maintenance:

```
GET    /files/{id}/status-detail    # Detailed file status with recovery options
POST   /files/{id}/cancel           # Cancel active file processing
POST   /files/{id}/retry            # Retry failed file processing
POST   /files/{id}/recover          # Attempt to recover stuck file
DELETE /files/{id}/force            # Force delete file (admin only)
GET    /files/management/stuck      # Get list of stuck files
POST   /files/management/bulk-action # Perform bulk operations (delete, retry, cancel, recover)
POST   /files/management/cleanup-orphaned # Clean up orphaned files (admin only)
```

**Enhanced File Safety Features:**
- **Pre-deletion Safety Checks**: Prevents deletion of files with active processing
- **Force Deletion**: Admin-only override for stuck/orphaned files
- **Intelligent Retry Logic**: Retry with attempt limits and state cleanup
- **Bulk Operations**: Process multiple files efficiently
- **Auto-Recovery**: Detect and recover stuck files automatically
- **Detailed Status Information**: Comprehensive file state with actionable recommendations

**New File Status States:**
- `PENDING` → File uploaded, waiting for processing
- `PROCESSING` → AI transcription/diarization in progress
- `COMPLETED` → Successfully processed with transcript available
- `ERROR` → Processing failed, can be retried
- `CANCELLING` → User requested cancellation (NEW)
- `CANCELLED` → Successfully cancelled (NEW)
- `ORPHANED` → Task lost/stuck, needs recovery (NEW)

**Status Detail Response Example:**
```json
{
  "file_id": 123,
  "filename": "interview.mp3",
  "status": "error",
  "can_delete": true,
  "can_retry": true,
  "can_cancel": false,
  "is_stuck": false,
  "retry_count": 1,
  "max_retries": 3,
  "last_error_message": "No speech detected in audio file",
  "actions_available": ["delete", "retry"],
  "recommendations": [
    "This file can be retried for processing.",
    "Check if the file contains clear speech content."
  ]
}
```

### Users (`users.py`)
```
GET    /users               # List users (admin only)
POST   /users               # Create user (admin only)
GET    /users/me            # Current user profile
PUT    /users/me            # Update current user
GET    /users/{id}          # Get user (admin only)
PUT    /users/{id}          # Update user (admin only)
DELETE /users/{id}          # Delete user (admin only)
```

### Comments (`comments.py`)
```
GET    /comments            # List comments for file
POST   /comments            # Create comment
GET    /comments/{id}       # Get comment
PUT    /comments/{id}       # Update comment
DELETE /comments/{id}       # Delete comment
```

### Tags (`tags.py`)
```
GET    /tags                # List user tags
POST   /tags                # Create tag
GET    /tags/{id}           # Get tag
PUT    /tags/{id}           # Update tag
DELETE /tags/{id}           # Delete tag
POST   /tags/{id}/files     # Tag files
DELETE /tags/{id}/files     # Untag files
```

### Speakers (`speakers.py`)
```
GET    /speakers            # List user speakers
GET    /speakers/{id}       # Get speaker
PUT    /speakers/{id}       # Update speaker
POST   /speakers/merge      # Merge speakers
```

### Tasks (`tasks.py`)
```
GET    /tasks               # List user tasks
GET    /tasks/{id}          # Get task status
DELETE /tasks/{id}          # Cancel task
```

### Search (`search.py`)
```
GET    /search              # Search across files/transcripts
GET    /search/files        # Search files only
GET    /search/transcripts  # Search transcripts only
```

### User Settings (`user_settings.py`)
```
GET    /user-settings/recording            # Get user recording preferences
PUT    /user-settings/recording            # Update recording settings
DELETE /user-settings/recording            # Reset to defaults
GET    /user-settings/transcription        # Get transcription preferences
PUT    /user-settings/transcription        # Update transcription settings
DELETE /user-settings/transcription        # Reset transcription to defaults
GET    /user-settings/transcription/system-defaults  # System defaults + language options
GET    /user-settings/all                  # All user settings (debug)
```

### Summarization (`summarization.py`)
```
POST   /summarization/{file_id}     # Generate AI summary
GET    /summarization/{file_id}     # Get existing summary
DELETE /summarization/{file_id}     # Delete summary
```

**Features:**
- Multi-provider: OpenAI, Claude, vLLM, Ollama, OpenRouter, custom endpoints
- BLUF format with action items, decisions, speaker analysis
- 12 output languages
- Intelligent section processing for transcripts of any length
- Real-time WebSocket progress notifications

### URL Processing (`files/url_processing.py`)
```
POST   /files/process-url           # Process URLs via yt-dlp (1800+ platforms)
GET    /files/url-status/{task_id}  # Get URL processing status
```

**Features:**
- yt-dlp integration for 1800+ platforms
- Best support: YouTube, Dailymotion, TikTok
- Limited support: Vimeo, Instagram, Facebook (may require auth)
- Anti-blocking measures for YouTube (client rotation, proper headers)
- User-friendly error messages for auth-required content

### Admin (`admin.py`)
```
GET    /admin/stats                        # System statistics
GET    /admin/users                        # All users management
GET    /admin/files                        # All files management
POST   /admin/users/{id}/toggle            # Toggle user status
DELETE /admin/cleanup                      # System cleanup
POST   /admin/profile-embeddings/repair    # Repair speaker profile embeddings
```

### ASR Settings (`asr_settings.py`) — v0.4.0
```
GET    /asr-settings/providers             # List all ASR providers
GET    /asr-settings/local-model           # Get current local model
POST   /asr-settings/local-model/set       # Set local Whisper model (admin)
POST   /asr-settings/local-model/restart   # Gracefully restart GPU worker (admin)
GET    /asr-settings/providers/{id}/validate # Validate cloud provider connection
```

### Groups (`groups.py`) — v0.4.0
```
GET    /groups                  # List user groups
POST   /groups                  # Create group
GET    /groups/{id}             # Get group
PUT    /groups/{id}             # Update group
DELETE /groups/{id}             # Delete group
POST   /groups/{id}/members     # Add member
DELETE /groups/{id}/members/{user_id} # Remove member
```

### Collection Shares (`media_collections.py`) — v0.4.0
```
POST   /media-collections/{id}/share       # Share collection
GET    /media-collections/{id}/shares      # List shares
DELETE /media-collections/{id}/shares/{share_id} # Revoke share
```

## 🔐 Authentication & Authorization

### Authentication Patterns
All protected endpoints use dependency injection:

```python
current_user: User = Depends(get_current_active_user)
```

### Authorization Levels
1. **Public**: No authentication required
2. **User**: Valid JWT token required
3. **Owner**: User must own the resource
4. **Admin**: Admin role required

### Example Authorization Patterns
```python
# User must own the file
@router.get("/files/{file_id}")
def get_file(
    file_id: int,
    current_user: User = Depends(get_current_active_user)
):
    # Check ownership in endpoint or service layer
    pass

# Admin only
@router.get("/admin/users")
def list_all_users(
    current_user: User = Depends(get_admin_user)  # Custom dependency
):
    pass
```

## 📝 Request/Response Patterns

### Standard Success Response
```json
{
  "id": 1,
  "name": "Resource name",
  "created_at": "2023-01-01T00:00:00Z",
  "updated_at": "2023-01-01T00:00:00Z"
}
```

### List Response with Metadata
```json
{
  "data": [...],
  "total": 100,
  "page": 1,
  "per_page": 20
}
```

### Error Response
```json
{
  "detail": "Resource not found",
  "status_code": 404,
  "error_type": "not_found"
}
```

### Validation Error Response
```json
{
  "detail": [
    {
      "loc": ["body", "email"],
      "msg": "field required",
      "type": "value_error.missing"
    }
  ],
  "status_code": 422
}
```

## 🔄 WebSocket Integration (`websockets.py`)

### Real-time Updates
WebSockets provide real-time notifications for:
- **Task progress**: Transcription, analysis, summarization progress updates
- **File status**: Processing status changes with detailed progress tracking
- **Upload progress**: Real-time upload status with concurrent file processing
- **Summarization progress**: AI processing status with section-by-section updates
- **System notifications**: Admin alerts and user-specific notifications
- **Recording status**: Live recording progress and session management

### WebSocket Pattern
```python
@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int):
    await websocket.accept()
    # Handle real-time communication
```

### Client Usage
```javascript
const ws = new WebSocket('ws://localhost:5174/ws/123');
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
```python
from app.utils.error_handlers import ErrorHandler

# Standardized error responses
raise ErrorHandler.not_found_error("File")
raise ErrorHandler.validation_error("Invalid email format")
raise ErrorHandler.unauthorized_error("Access denied")
```

## 📊 Query Parameters & Filtering

### Common Query Parameters
```python
# Pagination
page: int = Query(1, ge=1)
per_page: int = Query(20, ge=1, le=100)

# Filtering
search: Optional[str] = None
from_date: Optional[datetime] = None
to_date: Optional[datetime] = None

# Multiple values
tags: List[str] = Query(None)
status: List[str] = Query(None)
```

### Advanced Filtering Example
```python
@router.get("/files")
def list_files(
    search: Optional[str] = None,
    tag: Optional[List[str]] = Query(None),
    speaker: Optional[List[str]] = Query(None),
    from_date: Optional[datetime] = None,
    to_date: Optional[datetime] = None,
    min_duration: Optional[float] = None,
    max_duration: Optional[float] = None,
    file_type: Optional[List[str]] = Query(None),
    status: Optional[List[str]] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    """List files with advanced filtering options."""
    pass
```

## 🧪 Testing Endpoints

### Test Structure
```python
def test_create_resource(client, auth_headers):
    response = client.post(
        "/api/resources",
        json={"name": "Test Resource"},
        headers=auth_headers
    )
    assert response.status_code == 201
    assert response.json()["name"] == "Test Resource"

def test_unauthorized_access(client):
    response = client.get("/api/resources")
    assert response.status_code == 401
```

### Testing Utilities
- **Test client**: FastAPI TestClient
- **Auth headers**: JWT token for testing
- **Mock data**: Fixtures for test data
- **Database**: Isolated test database

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

1. **Create/Update Model** (`app/models/`)
   ```python
   class NewResource(Base):
       __tablename__ = "new_resource"
       id: Mapped[int] = mapped_column(Integer, primary_key=True)
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
   class NewResourceService:
       def create(self, db: Session, data: NewResourceCreate) -> NewResource:
           # Business logic here
           pass
   ```

4. **Create Endpoints** (`app/api/endpoints/`)
   ```python
   @router.post("/", response_model=NewResourceResponse)
   def create_resource(resource: NewResourceCreate, ...):
       return service.create(db, resource)
   ```

5. **Add to Router** (`app/api/router.py`)
   ```python
   from app.api.endpoints import new_resource
   api_router.include_router(new_resource.router, prefix="/new-resources")
   ```

6. **Add Tests** (`tests/api/endpoints/`)
   ```python
   def test_create_new_resource(client, auth_headers):
       # Test implementation
       pass
   ```

---

This API layer provides a robust, scalable foundation for the OpenTranscribe application with clear patterns, comprehensive documentation, and modern FastAPI practices.
