<div align="center">
  <img src="../../../assets/logo-banner.png" alt="OpenTranscribe Logo" width="250">

  # Background Tasks Documentation
</div>

This directory contains Celery-based background tasks for CPU-intensive AI processing operations. Tasks are designed to run asynchronously to avoid blocking API requests.

## ⚡ Task System Overview

### Architecture
```
API Request → Task Dispatch → Celery Worker → AI Processing → Database Update → WebSocket Notification
     ↓              ↓              ↓               ↓                ↓                    ↓
  HTTP Layer    Queue System   Background     AI/ML Models    Data Storage      Real-time UI
```

### Core Technologies
- **Celery**: Distributed task queue system
- **Redis**: Message broker for task queuing
- **Flower**: Task monitoring and management UI
- **WhisperX**: Advanced speech recognition with alignment
- **PyAnnote**: Speaker diarization and voice analysis
- **Multi-Provider LLMs**: Intelligent summarization with context processing
- **FFmpeg**: Media processing and conversion

## Task Structure

```
tasks/
├── transcription/                    # 3-stage GPU transcription pipeline
│   ├── __init__.py                  # Task exports
│   ├── preprocess.py                # Stage 1: download from MinIO, extract audio
│   ├── core.py                      # Stage 2: WhisperX + PyAnnote (GPU)
│   ├── postprocess.py               # Stage 3: index, notify, dispatch enrichment
│   ├── dispatch.py                  # Task routing helpers
│   ├── audio_processor.py           # FFmpeg audio conversion/extraction
│   ├── metadata_extractor.py        # ExifTool media metadata
│   ├── speaker_processor.py         # Speaker diarization processing
│   ├── storage.py                   # Database storage utilities
│   ├── notifications.py             # WebSocket notifications
│   └── waveform_generator.py        # Waveform data generation
├── speaker_tasks.py                  # Thin re-export module (do not add logic here)
├── speaker_identification_task.py    # LLM-powered speaker name suggestions
├── speaker_update_task.py            # Background speaker record updates
├── speaker_embedding_task.py         # Embedding extraction and reassignment
├── reindex_task.py                   # Search reindexing with stop/cancel support
├── file_retention_task.py            # Auto-deletion based on admin retention policy
├── analytics.py                      # Analytics and insights processing
├── cleanup.py                        # Stuck file detection and recovery
├── summarization.py                  # Multi-provider LLM summarization
├── summary_retry.py                  # Summary retry logic
├── youtube_processing.py             # yt-dlp URL download processing
├── embedding_migration_v4.py         # Speaker embedding migration to v4 format
├── migration_pipeline.py             # General migration pipeline tasks
├── embedding_consistency_repair.py   # Embedding index consistency repair
├── search_maintenance_task.py        # OpenSearch index maintenance
├── search_indexing_task.py           # Search indexing tasks
├── thumbnail.py                      # Thumbnail generation
├── waveform.py                       # Waveform generation (standalone)
├── rediarize_task.py                 # Re-diarization tasks
├── topic_extraction.py               # LLM topic extraction
└── utility.py                        # Shared task utilities
```

## Transcription Pipeline (`transcription/`)

### 3-Stage Pipeline Overview

The transcription pipeline runs as three chained Celery tasks, separating concerns and allowing the GPU worker to be freed as early as possible:

```
preprocess_for_transcription  →  transcribe_gpu_task  →  finalize_transcription
     (download audio)            (WhisperX + PyAnnote)   (index + notify + dispatch)
          CPU                            GPU                      CPU
```

Celery task names: `transcription.preprocess` → `transcription.gpu_transcribe` →
`transcription.postprocess`.

Enrichment tasks (speaker embedding, LLM identification) are dispatched asynchronously from postprocess — they do not block the GPU worker.

### Stage 1: Preprocess (`preprocess.py`)
- Downloads file from MinIO
- Extracts audio with FFmpeg
- Extracts media metadata with ExifTool
- Dispatches `transcribe_gpu_task`

### Stage 2: GPU Transcription (`core.py`)
Main GPU-side orchestrator:

```python
@celery_app.task(bind=True, name="transcription.gpu_transcribe", queue=CeleryQueues.GPU)
def transcribe_gpu_task(self, preprocess_context: dict) -> dict:
    """
    Run WhisperX transcription + PyAnnote speaker diarization.

    Steps:
    1. Load audio from temp storage
    2. Run WhisperX with configured model (admin-pinned via SystemSettings)
    3. Run PyAnnote speaker diarization
    4. Process and store transcript segments
    5. Chain into finalize_transcription
    """
```

**Key Features:**
- **Admin-pinned model**: Reads model from `SystemSettings` key `asr.local_model`, falls back to `WHISPER_MODEL` env var
- **Model preloading**: Worker preloads model at startup via `worker_ready` signal (CUDA workers)
- **GPU memory management**: `torch.cuda.empty_cache()` after processing
- **Progress tracking**: Real-time updates via `progress_tracker.py` (EWMA ETA)

### Stage 3: Postprocess (`postprocess.py`)
- Indexes content in OpenSearch
- Sends WebSocket completion notification
- Dispatches async enrichment: speaker embedding, LLM identification (does not block GPU)

### Metadata Extraction (`metadata_extractor.py`)
Extracts comprehensive media metadata using ExifTool:

```python
def extract_media_metadata(file_path: str) -> Optional[Dict[str, Any]]:
    """Extract metadata from media file using ExifTool."""

def get_important_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Filter and normalize important metadata fields."""

def update_media_file_metadata(media_file, extracted_metadata: Dict[str, Any],
                              content_type: str, file_path: str) -> None:
    """Update MediaFile object with extracted metadata."""
```

**Extracted Information:**
- **File Properties**: Size, format, type, duration
- **Video Specs**: Resolution, frame rate, codec, aspect ratio
- **Audio Specs**: Channels, sample rate, bit depth
- **Creation Data**: Timestamps, device information
- **Content Info**: Title, author, description, GPS data

### Audio Processing (`audio_processor.py`)
Handles audio extraction and format conversion:

```python
def prepare_audio_for_transcription(temp_file_path: str, content_type: str,
                                  temp_dir: str) -> str:
    """Prepare audio file for transcription by extracting from video or converting format."""

def extract_audio_from_video(video_path: str, output_path: str) -> None:
    """Extract audio from video using FFmpeg."""

def convert_audio_format(input_path: str, output_path: str) -> None:
    """Convert audio to WAV format for optimal WhisperX processing."""
```

**Processing Features:**
- **Format Detection**: Automatic format detection from MIME types
- **Video Audio Extraction**: Extract audio track from video files
- **Audio Conversion**: Convert to optimal format (16kHz, mono, PCM)
- **Quality Optimization**: Prepare audio for best AI recognition results

### Speaker Processing (`speaker_processor.py`)
Manages speaker identification and database operations:

```python
def extract_unique_speakers(segments: List[Dict[str, Any]]) -> Set[str]:
    """Extract unique speaker IDs from transcription segments."""

def create_speaker_mapping(db: Session, user_id: int, unique_speakers: Set[str]) -> Dict[str, int]:
    """Create mapping of speaker labels to database IDs."""

def process_segments_with_speakers(segments: List[Dict[str, Any]],
                                 speaker_mapping: Dict[str, int]) -> List[Dict[str, Any]]:
    """Process transcription segments and add speaker database IDs."""
```

**Speaker Features:**
- **Consistent Labeling**: Standardized speaker ID format (SPEAKER_XX)
- **Cross-file Tracking**: UUID-based speaker identification
- **Database Integration**: Automatic speaker record creation
- **User Management**: Speaker verification and display names

### Database Storage (`storage.py`)
Handles all database operations for transcription results:

```python
def save_transcript_segments(db: Session, file_id: int, segments: List[Dict[str, Any]]) -> None:
    """Save transcript segments to database."""

def update_media_file_transcription_status(db: Session, file_id: int,
                                         segments: List[Dict[str, Any]], language: str = "en") -> None:
    """Update media file with transcription completion metadata."""
```

**Storage Features:**
- **Bulk Operations**: Efficient segment insertion
- **Transaction Management**: Atomic operations with rollback
- **Status Updates**: File status progression tracking
- **Relationship Management**: Speaker-segment associations

### Notifications (`notifications.py`)
Real-time WebSocket notifications for UI updates:

```python
def send_notification_with_retry(user_id: int, file_id: int, status: FileStatus,
                                message: str, progress: int = 0, max_retries: int = 3) -> bool:
    """Send notification with retry logic."""

def send_processing_notification(user_id: int, file_id: int) -> None:
    """Send processing started notification."""

def send_completion_notification(user_id: int, file_id: int) -> None:
    """Send transcription completed notification."""
```

**Notification Features:**
- **Real-time Updates**: Instant UI notifications via WebSocket
- **Progress Tracking**: Step-by-step progress updates
- **Error Notifications**: User-friendly error messages
- **Retry Logic**: Robust delivery with automatic retries

## Speaker Tasks (Split Architecture)

`speaker_tasks.py` is a thin re-export module only. Real implementations live in dedicated files:

### `speaker_identification_task.py`
LLM-powered speaker name suggestions based on conversation context. Suggestions are never auto-applied — they require manual user verification.

### `speaker_update_task.py`
Background updates to speaker records (display names, profile links, verification status).

### `speaker_embedding_task.py`
Extracts voice embeddings and handles speaker profile reassignment. Runs on the embedding worker (`celery-embedding-worker`), keeping GPU memory free.

## Analytics Tasks (`analytics.py`)

### Purpose
Generate insights and analytics from transcribed content:

```python
@celery_app.task(name="analyze_transcript")
def analyze_transcript_task(file_id: int):
    """
    Analyze a transcript for additional metadata and insights.

    Generates:
    - Word count statistics
    - Speaker distribution
    - Segment analysis
    - Duration metrics
    """
```

**Analytics Features:**
- **Content Metrics**: Word counts, segment counts, speaker distribution
- **Time Analysis**: Speaking time per speaker, segment duration patterns
- **Language Insights**: Vocabulary analysis, complexity metrics
- **Engagement Metrics**: Turn-taking patterns, interruption analysis

## 📝 AI Summarization Tasks (`summarization.py`)

### Purpose
Generate comprehensive BLUF-format summaries using multi-provider LLMs with intelligent context processing:

```python
@celery_app.task(name="summarize_transcript")
def summarize_transcript_task(file_id: int):
    """
    Generate AI-powered summary with intelligent section processing.

    Workflow:
    1. Retrieve transcript and speaker data from database
    2. Query LLM model for context length capabilities
    3. Automatically chunk long transcripts at natural boundaries
    4. Process each section individually for comprehensive analysis
    5. Stitch section summaries into final BLUF format
    6. Store results in both PostgreSQL and OpenSearch for search
    """
```

### Intelligent Context Processing

```python
# Example: Long transcript processing
context_length = await llm_service.get_model_context_length()  # 4096 for small Ollama model
transcript_chunks = chunk_transcript_intelligently(transcript, context_length)

if len(chunks) == 1:
    # Single-pass processing
    summary = await llm_service.generate_summary(transcript_chunks[0])
else:
    # Multi-section processing
    section_summaries = []
    for i, chunk in enumerate(transcript_chunks):
        section_summary = await llm_service.summarize_transcript_section(chunk, i+1, len(chunks))
        section_summaries.append(section_summary)

    # Stitch into comprehensive BLUF summary
    summary = await llm_service.stitch_section_summaries(section_summaries)
```

**AI Summarization Features:**
- **BLUF Format**: Bottom Line Up Front executive summaries
- **Multi-Provider Support**: vLLM, OpenAI, Ollama, Claude, OpenRouter
- **Intelligent Chunking**: Natural boundaries (speaker changes, topics, sentences)
- **Context-Aware Processing**: Automatic model capability detection
- **Universal Compatibility**: Works with 4K to 200K+ token models
- **No Content Loss**: Complete transcript analysis regardless of length
- **Structured Output**: Action items, decisions, speaker analysis, key points

## 🧹 File Cleanup Tasks (`cleanup.py`)

### Purpose
Automated background tasks for system maintenance, file recovery, and health monitoring:

```python
@shared_task(bind=True, name="cleanup.run_periodic_cleanup")
def run_periodic_cleanup(self):
    """
    Periodic task to clean up stuck files and maintain system health.

    This task should be run regularly (e.g., every 30 minutes) to:
    - Detect and recover stuck files
    - Mark orphaned files for cleanup
    - Generate system health reports
    """

@shared_task(bind=True, name="cleanup.deep_cleanup")
def run_deep_cleanup(self, dry_run: bool = False):
    """
    Deep cleanup task for removing orphaned files (admin-triggered).

    Args:
        dry_run: If True, only preview what would be cleaned up
    """

@shared_task(bind=True, name="cleanup.health_check")
def system_health_check(self):
    """
    Generate a system health report with file processing metrics.
    """

@shared_task(bind=True, name="cleanup.emergency_recovery")
def emergency_file_recovery(self, file_ids: list):
    """
    Emergency recovery task for specific files (admin-triggered).

    Args:
        file_ids: List of file IDs to attempt recovery on
    """
```

### Auto-Recovery Features
The cleanup tasks provide comprehensive file recovery capabilities:

**Stuck File Detection:**
- Files processing longer than threshold (default: 2 hours)
- Files pending longer than threshold without starting
- Files with failed tasks that can be retried
- Files marked as orphaned needing recovery

**Recovery Actions:**
- Cancel stuck/failed Celery tasks
- Reset file status and retry counters
- Clear partial processing artifacts
- Restart transcription with fresh task
- Update recovery attempt tracking

**System Health Monitoring:**
- Calculate error rates and processing statistics
- Generate actionable health recommendations
- Monitor worker capacity and queue health
- Track cleanup success/failure rates

### Periodic Cleanup Configuration
```python
# Celery Beat configuration for automatic scheduling
from celery.schedules import crontab

celery_app.conf.beat_schedule = {
    'run-periodic-cleanup': {
        'task': 'cleanup.run_periodic_cleanup',
        'schedule': crontab(minute='*/30'),  # Every 30 minutes
    },
    'system-health-check': {
        'task': 'cleanup.health_check',
        'schedule': crontab(minute=0, hour='*/6'),  # Every 6 hours
    },
}
```

### Integration with Enhanced File Safety
The cleanup tasks work with the enhanced file management system:

**Enhanced Status States:**
- `PENDING` → `PROCESSING` → `COMPLETED`/`ERROR`
- New states: `CANCELLING`, `CANCELLED`, `ORPHANED`
- Task tracking with `active_task_id` and timing metadata

**Safe Operations:**
- Pre-deletion safety checks for active processing
- Force deletion options for admin users
- Retry limits and intelligent recovery logic
- Transaction safety with proper rollback handling

**Real-time Monitoring:**
- WebSocket notifications for recovery operations
- Progress tracking for bulk operations
- User-friendly error messages and recommendations

## Task Naming & Queue Convention

### Naming Rules

All Celery tasks **MUST** use explicit `name=` in their decorator with dotted namespace format:

```
<domain>.<action>
```

**Domains:**
| Domain | Purpose | Example |
|--------|---------|---------|
| `transcription.*` | Core transcription pipeline | `transcription.process_file` |
| `ai.*` | LLM/AI enrichment | `ai.generate_summary` |
| `media.*` | Media processing (waveform, thumbnails) | `media.generate_waveform` |
| `download.*` | URL/playlist downloads | `download.media_url` |
| `speaker.*` | Speaker analysis (clustering, embeddings) | `speaker.cluster_for_file` |
| `search.*` | Search indexing and maintenance | (future tasks) |
| `analytics.*` | Transcript analytics | `analytics.analyze_transcript` |
| `system.*` | Health checks, recovery, GPU stats | `system.health_check` |
| `cleanup.*` | File cleanup and retention | `cleanup.run_periodic_cleanup` |
| `migration.*` | Data migration tasks | `migration.normalize_embeddings` |

**Legacy flat names** (e.g., `rediarize`, `generate_thumbnail`) are stable — do not rename, but do not create new flat names.

### Queue Assignment Rules

1. **Define queue routing in `task_routes` only** (`backend/app/core/celery.py`) — this is the single source of truth
2. **Do NOT put `queue=` in task decorators** — it creates a maintenance hazard (decorator is silently overridden by `task_routes`)
3. **Use `CeleryQueues.*` constants** from `app.core.constants` — never raw queue name strings
4. **Exception:** Dynamically-routed tasks (e.g., `transcription.gpu_transcribe`) that are intentionally NOT in `task_routes` may use `.set(queue=CeleryQueues.X)` at call time

### Queue Definitions

All valid queues are declared in `CeleryQueues` class (`backend/app/core/constants.py`).
`task_create_missing_queues=False` is set — any typo in a queue name raises an error at dispatch time.

| Queue | Worker | Concurrency | Purpose |
|-------|--------|-------------|---------|
| `gpu` | `celery-worker` | 1 | GPU-intensive AI tasks (WhisperX, PyAnnote, embeddings) |
| `download` | `celery-download-worker` | 3 | Network I/O for yt-dlp media downloads |
| `cpu` | `celery-cpu-worker` | 8 | CPU-intensive processing (waveform, thumbnails, reindex) |
| `nlp` | `celery-nlp-worker` | 4 | LLM API calls (summarization, topics, speaker ID) |
| `embedding` | `celery-embedding-worker` | 1 | OpenSearch neural search indexing |
| `utility` | `celery-cpu-worker` | 8 | Lightweight maintenance (health checks, cleanup) |
| `cloud-asr` | `celery-cpu-worker` | 8 | Cloud ASR provider HTTP calls (dynamic routing) |
| `cpu-transcribe` | `celery-cpu-worker` | 8 | Lightweight CPU transcription (dynamic routing) |
| `celery` | `celery-nlp-worker` | 4 | Default fallback queue |

### Startup Validation

On worker startup, `_validate_task_routes()` logs a WARNING for any registered task missing from `task_routes`. This catches accidentally unrouted tasks that would silently go to the default `celery` queue.

### Task Registration
Tasks are registered via explicit `include=` list in `celery_app` initialization (`backend/app/core/celery.py`). When adding a new task module, add it to this list.

## 📊 Task Monitoring

### Flower Dashboard
- **URL**: http://localhost:5175/flower
- **Features**: Task monitoring, worker status, performance metrics
- **Real-time**: Live task progress and completion rates

### Task Status Tracking
```python
# Task status in database
class Task(Base):
    id: str  # Celery task ID
    status: str  # pending, in_progress, completed, failed
    progress: float  # 0.0 to 1.0
    error_message: Optional[str]
    created_at: datetime
    completed_at: Optional[datetime]
```

### Progress Updates
```python
# Update task progress during processing
with session_scope() as db:
    update_task_status(db, task_id, "in_progress", progress=0.5)
```

## 🛡️ Error Handling

### Error Recovery Patterns
```python
@celery_app.task(bind=True, autoretry_for=(Exception,), retry_kwargs={'max_retries': 3})
def robust_task(self, file_id: int):
    """Task with automatic retry on failure."""
    try:
        # Task logic
        return process_file(file_id)
    except RecoverableError as e:
        # Retry for recoverable errors
        raise self.retry(countdown=60, exc=e)
    except FatalError as e:
        # Don't retry for fatal errors
        logger.error(f"Fatal error in task: {e}")
        raise
```

### Error Notification
```python
def handle_task_failure(task_id: str, file_id: int, user_id: int, error: Exception):
    """Handle task failure with proper cleanup and notification."""
    try:
        # Update database status
        with session_scope() as db:
            update_task_status(db, task_id, "failed", error_message=str(error))
            update_media_file_status(db, file_id, FileStatus.ERROR)

        # Notify user
        send_error_notification(user_id, file_id, str(error))

    except Exception as cleanup_error:
        logger.error(f"Error during failure cleanup: {cleanup_error}")
```

## 🚀 Performance Optimization

### GPU Utilization
```python
# Optimize for GPU processing
device = "cuda" if torch.cuda.is_available() else "cpu"
compute_type = "float16" if device == "cuda" else "float32"

# Memory management
if device == "cuda":
    gc.collect()
    torch.cuda.empty_cache()
```

### Batch Processing
```python
# Process multiple files efficiently
@celery_app.task
def batch_transcribe_task(file_ids: List[int]):
    """Process multiple files in a single task for efficiency."""
    for file_id in file_ids:
        try:
            process_single_file(file_id)
        except Exception as e:
            logger.error(f"Error processing file {file_id}: {e}")
            continue  # Continue with other files
```

### Resource Management
```python
# Temporary file cleanup
with tempfile.TemporaryDirectory() as temp_dir:
    # Processing within temporary directory
    # Automatic cleanup on exit
    pass

# Database session management
with session_scope() as db:
    # Database operations
    # Automatic commit/rollback
    pass
```

## 🧪 Testing Tasks

### Task Testing Patterns
```python
def test_preprocess_task_success(celery_app, db_session, sample_file):
    """Test successful preprocess task (stage 1)."""
    from app.tasks.transcription.preprocess import preprocess_for_transcription

    with patch('app.services.minio_service.download_file') as mock_download:
        mock_download.return_value = (sample_audio_data, 1024, "audio/wav")

        result = preprocess_for_transcription.apply(args=[sample_file.id])

        assert result.successful()

def test_gpu_transcription_task_failure(celery_app, db_session, invalid_file):
    """Test GPU transcription task failure handling (stage 2)."""
    from app.tasks.transcription.core import transcribe_gpu_task

    result = transcribe_gpu_task.apply(args=[invalid_file.id])
    assert result.failed()
    # Verify file status set to ERROR and user notified
```

### Integration Testing
```python
def test_full_transcription_pipeline(client, auth_headers, sample_video):
    """Test complete pipeline from upload to completion."""
    # 1. Upload file
    response = client.post("/api/files", files={"file": sample_video}, headers=auth_headers)
    file_id = response.json()["id"]

    # 2. Wait for processing (with timeout)
    # 3. Verify transcription results
    # 4. Check database state
    # 5. Verify search indexing
```

## 🔧 Adding New Tasks

### Task Creation Checklist
1. **Define Purpose**: Clear single responsibility
2. **Design Interface**: Input parameters and return values
3. **Error Handling**: Comprehensive error recovery
4. **Progress Tracking**: Regular progress updates
5. **Resource Management**: Proper cleanup
6. **Testing**: Unit and integration tests
7. **Documentation**: Clear docstrings and examples

### Task Template
```python
@celery_app.task(bind=True, name="new_task")
def new_task(self, input_data: Dict[str, Any]):
    """
    New task description.

    Args:
        input_data: Task input parameters

    Returns:
        Dict with task results
    """
    task_id = self.request.id

    try:
        # 1. Initialize and validate
        validate_input(input_data)

        # 2. Update progress
        with session_scope() as db:
            update_task_status(db, task_id, "in_progress", progress=0.1)

        # 3. Perform main processing
        result = perform_main_operation(input_data)

        # 4. Update progress
        with session_scope() as db:
            update_task_status(db, task_id, "in_progress", progress=0.8)

        # 5. Finalize and return
        with session_scope() as db:
            update_task_status(db, task_id, "completed", progress=1.0, completed=True)

        return {"status": "success", "result": result}

    except Exception as e:
        # Error handling
        with session_scope() as db:
            update_task_status(db, task_id, "failed", error_message=str(e), completed=True)

        logger.error(f"Task {task_id} failed: {e}")
        return {"status": "error", "message": str(e)}
```

---

The background task system provides a robust, scalable foundation for AI-powered media processing with comprehensive error handling, progress tracking, and real-time user feedback.
