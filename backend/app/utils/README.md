<div align="center">
  <img src="../../../assets/logo-banner.png" alt="OpenTranscribe Logo" width="200">

  # Utilities Documentation
</div>

This directory contains small, low-dependency helpers shared by endpoints, services and Celery tasks. Domain logic belongs in `app/services`; the utilities here are building blocks. These modules load in every process flavor (API, GPU worker, CPU worker, redaction worker), so heavy dependencies (torch, pyannote.metrics, ...) are imported lazily inside the functions that need them. Single-home rules for the trickier modules (speaker labels, Content-Disposition, media types, WebSocket notifications, GPU selection, NLTK) are in `app/utils/CLAUDE.md`.

## 🛠️ Utilities Overview

### Design Principles
- **Single home for a rule**: several modules exist to be the only implementation of something (UUID/permission lookup, canonical speaker label, MIME alias folding, Content-Disposition, cosine conversion). Do not write a second copy.
- **Single responsibility**: each module is focused on one concern
- **Type safety**: type hints on all new functions
- **Best-effort side paths** (cache invalidation, WebSocket publish, metrics) log and swallow errors; they must never break the write they accompany

## Utility Modules

```
utils/
├── Database, identity and access
│   ├── db_helpers.py           # Query builders, get_or_create, tenant scoping (apply_tenant_scope)
│   ├── uuid_helpers.py         # Hybrid-ID lookup + the permission chokepoint (get_file_by_uuid_with_permission)
│   ├── uuid7.py                # RFC 9562 UUIDv7 generator (the default= for every model uuid column)
│   ├── pagination.py           # paginate(): count + offset + limit in one call
│   ├── tenant_sharing.py       # Tenant gates for per-user items shared "with everyone"
│   ├── encryption.py           # AES-256-GCM encryption for stored secrets (legacy Fernet auto-detected)
│   ├── client_ip.py            # Single source of truth for "who is this request from?"
│   └── user_settings_helpers.py, summary_settings.py   # User/system settings access helpers
├── Errors
│   ├── error_handlers.py       # ErrorHandler HTTPException builders, handle_not_found
│   └── error_classification.py # Fine-grained failure categories (retry policy; not on the wire)
├── Tasks and workers
│   ├── task_utils.py           # Task records, status transitions, cancellation, stuck-file recovery
│   ├── task_lock.py            # Redis lock to stop periodic tasks overlapping (with_task_lock)
│   ├── boot_once.py            # Run a startup step once across replicas
│   ├── host_slots.py           # Non-blocking per-host concurrency slots (flock)
│   ├── cpu_budget.py           # CPU quota of this process (container quota, not host cores)
│   ├── scratch_volume.py       # Shared scratch volume for cross-worker artifact handoff
│   ├── temp_file_utils.py      # Temporary file handling for audio/media tasks
│   └── websocket_notify.py     # Publish WebSocket events over Redis (send_ws_event / send_ws_event_for_file)
├── Transcripts and speakers
│   ├── transcript_builders.py  # Shared transcript text building (masking-aware)
│   ├── transcript_comparison.py# Transcript comparison for pipeline benchmarking
│   ├── segment_dedup.py, segment_postprocess.py   # Segment dedup, resegmentation and merging
│   ├── speaker_labels.py       # The canonical speaker display label
│   ├── embedding_helpers.py, cosine_space.py      # Embedding math; cosine <-> OpenSearch score conversion
│   ├── pyannote_utils.py, diarization_metrics.py  # PyAnnote helpers; boundary accuracy metrics
│   ├── language.py, time_format.py, text_preprocessing.py   # Language parsing, timestamps, topic-extraction text prep
│   └── whisper_model_choice.py, lite_mode.py      # Per-file Whisper model rules; lite-deployment refusals
├── Files and media
│   ├── file_validation.py, file_hash.py, filename.py, media_types.py, content_disposition.py
│   ├── thumbnail.py, url_validation.py            # Thumbnails; SSRF-safe URL validation
│   └── csv_safe.py                                # Spreadsheet-formula neutralization for CSV exports
├── Hardware and models
│   ├── hardware_detection.py   # CUDA/MPS/CPU authority; resolve_cuda_device_index
│   ├── vram_profiler.py, nvml_monitor.py          # VRAM profiling (ENABLE_VRAM_PROFILING), NVML monitor
│   ├── hf_hub_offline.py, nltk_offline.py         # Bounded/offline model loading; NLTK corpus availability
│   └── benchmark_timing.py                        # Wall-clock timing (ENABLE_BENCHMARK_TIMING)
└── Misc
    ├── prompt_manager.py       # LLM prompt template management
    ├── llm_log_safety.py       # Log-safe descriptions of LLM text
    └── stats_helpers.py        # Consolidated database statistics helpers
```

## Transcript Builders (`transcript_builders.py`)

Shared transcript-building functions used across tasks and services, so formatting is not re-implemented in each:
- `mask_segment_text(segment, redaction_cfg)`: a segment's text after redaction masking
- `get_speaker_name(segment)`: the speaker display name for a segment
- `build_full_transcript(transcript_segments, redaction_cfg)`: the full speaker-attributed transcript string for LLM and search use

Import from here rather than reimplementing formatting. Export formats (SRT, VTT, TXT, JSON, CSV, Markdown) live in `services/transcript_export_*.py` and `services/captions/`.

## Authorization (`uuid_helpers.py`, `db_helpers.py`)

### Purpose
Resource-level authorization. There are **no authorization decorators**; the `auth_decorators.py` module that used to live here was deleted in issue #450 (unreferenced by any route, and silently bypassed by positional calls). Do not reintroduce that shape. Authorization is a FastAPI dependency or an explicit call.

```python
from app.utils.uuid_helpers import get_file_by_uuid_with_permission
from app.utils.uuid_helpers import require_resource_owner
from app.utils.db_helpers import apply_tenant_scope

@router.put("/files/{file_uuid}")
def update_file(
    file_uuid: UUID,
    updates: FileUpdateSchema,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    # The permission chokepoint. Order: platform bypass asked about the loaded row, takedown 404,
    # public, tenant gate, owner, shares.
    file_obj = get_file_by_uuid_with_permission(db, file_uuid, current_user.id)
    ...
```

`uuid_helpers.py` also provides `get_by_uuid`, `get_<entity>_by_uuid` for the common models, `get_collection_by_uuid_with_permission`, `require_speaker_access` and `require_profile_in_scope`. Privilege gates (`get_current_admin_user`, `get_current_active_superuser`) are FastAPI dependencies in `api/endpoints/auth/dependencies.py`.

## 🗄️ Database Helpers (`db_helpers.py`)

### Key Components
```python
def get_user_files_query(db: Session, user_id: int, *, organization_id=UNSCOPED) -> Query:
    """Standard query for a user's (or organization's) files."""

def get_user_files_query_for_context(db: Session, ctx: RequestContext) -> Query:
    """The same, scoped by the request's tenant context."""

def apply_tenant_scope(...):
    """SQL-plane default-deny tenant filter; mirrors api/deps_context.scope_to_context."""

def get_or_create[T](db, model, defaults=None, **kwargs) -> tuple[T, bool]
def safe_get_by_id[T](db, model, obj_id, user_id=None) -> T | None
def bulk_update[T](db, model, updates, id_field="id") -> bool

def get_file_with_transcript_count(...)
def get_user_speakers(db, user_id) -> list[Speaker]
def get_unique_speakers_for_file(db, file_id) -> list[Speaker]
def get_files_by_status(db, user_id, status) -> list[MediaFile]
def get_user_file_stats(db, user_id) -> dict
```
Tag creation and attachment live in `services/tag_service.py` and `services/tag_bulk.py`, not here.

### Usage
```python
query = get_user_files_query(db, user.id)
files = query.order_by(MediaFile.upload_time.desc()).all()

tag, created = get_or_create(db, Tag, name=tag_name)  # check models: Tag is tenant-scoped, prefer tag_service
```

## Error Handlers (`error_handlers.py`)

`ErrorHandler` builds `HTTPException` instances with consistent status codes and messages:
`database_error(operation, error)`, `validation_error(message)`, `not_found_error(resource)`, `unauthorized_error(message)`, `internal_error(message)` and `file_processing_error(operation, error)`. `handle_not_found(resource_name)` is a decorator factory that raises 404 when the wrapped function returns `None`.

`handle_database_errors` was removed (#914): it reclassified any `HTTPException` the wrapped function raised into an opaque 500.

```python
from app.utils.error_handlers import ErrorHandler

if not file_obj:
    raise ErrorHandler.not_found_error("File")
```

Remember that service-layer code raises the exceptions in `app/core/exceptions.py` instead; `main.py` maps them to HTTP statuses.

## ⚙️ Task Utilities (`task_utils.py`)

Task-row and file-status management for Celery tasks:

```python
create_task_record(db, celery_task_id, user_id, media_file_id, task_type)   # supersedes an older active row of the step
update_task_status(db, task_id, status, progress=None, error_message=None, completed=False)
update_task_status_with_duration(...)       # same, also returns elapsed time
update_media_file_status(db, file_id, status)   # FileStatus
update_media_file_from_task_status(db, file_id)
get_task_summary_for_media_file(db, file_id)
cancel_active_task(db, file_id)             # phase one of a two-phase stop (writes CANCELLING)
check_for_stuck_files(db, stuck_threshold_hours=2.0)
recover_stuck_file(db, file_id)
is_file_safe_to_delete(db, file_id)
reset_file_for_retry(db, file_id, reset_retry_count=False)
```

`cancel_active_task` arms the cancellation flag and writes `FileStatus.CANCELLING`; the worker writes `CANCELLED` when it has actually stopped (details in `app/tasks/CLAUDE.md`). `task_lock.with_task_lock(key, timeout)` prevents overlapping periodic tasks.

### Usage in a task
```python
@celery_app.task(bind=True, name="domain.action")
def long_running_task(self, file_uuid: str):
    task_id = self.request.id
    with session_scope() as db:
        update_task_status(db, task_id, "in_progress", progress=0.0)
    # ... slow work with no session open ...
    with session_scope() as db:
        update_task_status(db, task_id, "completed", progress=1.0, completed=True)
```

## 🔧 Common Patterns

### Session Management
`session_scope()` (in `app/db/session_utils.py`) is the transactional scope for background work; API endpoints use `Depends(get_db)`. Never hold a session across slow non-database work: read, release, work, then write in a short second session.

### Validation
Validate input and raise `ErrorHandler.validation_error(...)` (or the typed exceptions in `core/exceptions.py` from services). Upload validation helpers are in `file_validation.py`; user-supplied URLs go through `url_validation.py` (SSRF protection).

## 🧪 Testing Utilities

Utility tests live in `backend/tests/unit/` and run with `./scripts/run-backend-tests.sh tests/unit` (see `backend/README.md`). For example:

```python
def test_not_found_error():
    with pytest.raises(HTTPException) as exc_info:
        raise ErrorHandler.not_found_error("TestResource")

    assert exc_info.value.status_code == 404
    assert "TestResource not found" in str(exc_info.value.detail)
```

## 🚀 Performance Considerations

- Prefer `db.scalars(select(...))` / eager loading (`joinedload`, `selectinload`) over per-row lazy loads in loops
- Use `paginate()` instead of hand-rolled count + offset + limit
- Keep heavy imports lazy so CPU-only workers and fast unit tests can import these modules

## 🔧 Adding New Utilities

### Guidelines
1. **Check for an existing single-home module first** (see the list above and `app/utils/CLAUDE.md`)
2. **Single purpose** and full type hints
3. **No `app.api` imports** from here (use `TYPE_CHECKING` for types)
4. **Lazy heavy imports** (torch, pyannote.metrics, meeteval)
5. **Tests** in `backend/tests/unit/`
