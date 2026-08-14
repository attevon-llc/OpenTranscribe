<div align="center">
  <img src="../../../assets/logo-banner.png" alt="OpenTranscribe Logo" width="250">

  # Services Layer Documentation
</div>

The services layer provides a clean abstraction for business logic, separating it from API endpoints and database operations. This layer orchestrates complex operations, manages transactions, and provides reusable business functionality.

## 🎯 Service Layer Principles

### Design Goals
- **Business Logic Encapsulation**: Keep complex operations out of endpoints
- **Reusability**: Share common operations across multiple endpoints
- **Transaction Management**: Handle database transactions consistently
- **External Service Integration**: Manage third-party API interactions
- **Error Handling**: Provide consistent error handling patterns
- **Testing**: Enable easier unit testing of business logic

### Architecture Pattern
```
API Endpoints → Service Layer → Data Layer (Models/External APIs)
     ↓              ↓                    ↓
  HTTP Logic   Business Logic      Data Operations
```

## 📁 Service Structure

```
services/
├── interfaces.py                      # Protocol interfaces (Storage, Search, Cache, Notification)
├── notification_service.py            # Unified send_task_notification() wrapper
├── progress_tracker.py                # EWMA ETA progress tracking (preferred)
├── migration_progress_service.py      # Atomic Lua increments for concurrent batch migrations
├── file_service.py                    # File management operations
├── file_cleanup_service.py            # File recovery and cleanup operations
├── file_retention_service.py          # Admin-configurable auto-deletion policies
├── llm_service.py                     # Multi-provider LLM (vLLM, OpenAI, Ollama, Claude, OpenRouter)
├── opensearch_service.py              # Full-text + neural search, ML Commons, alias-based speaker indices
├── hybrid_search_service.py           # Hybrid BM25+vector search (OS 3.4 crash fix applied)
├── minio_service.py                   # Object storage operations
├── analytics_service.py               # Server-side analytics computation
├── error_categorization_service.py    # Error classification and user guidance
├── formatting_service.py              # Data formatting and display
├── profile_embedding_service.py       # Profile centroid management (averaging bug fixed)
├── speaker_matching_service.py        # Speaker matching with cosine score conversion
├── smart_speaker_suggestion_service.py # Intelligent speaker suggestion consolidation
├── speaker_status_service.py          # Speaker verification status management
├── similarity_service.py              # Speaker similarity with corrected cosine conversion
├── task_filtering_service.py          # Task and data filtering optimization
├── asr/                               # ASR service + 8 cloud provider clients
│   ├── asr_service.py                 # ASR provider dispatch
│   ├── deepgram_client.py
│   ├── assemblyai_client.py
│   ├── openai_asr_client.py
│   ├── google_asr_client.py
│   ├── azure_asr_client.py
│   ├── aws_asr_client.py
│   ├── speechmatics_client.py
│   └── gladia_client.py
└── search/                            # Search sub-services
```

## 🔧 Service Design Patterns

### Base Service Pattern
```python
class BaseService:
    """Base service class with common patterns."""

    def __init__(self, db: Session):
        self.db = db

    def _validate_user_access(self, resource, user: User) -> None:
        """Common access validation pattern."""
        if resource.user_id != user.id and not user.is_superuser:
            raise ErrorHandler.unauthorized_error("Access denied")

    def _handle_database_error(self, operation: str, error: Exception):
        """Common database error handling."""
        logger.error(f"Database error in {operation}: {error}")
        self.db.rollback()
        raise ErrorHandler.database_error(operation, error)
```

### Service Method Pattern
```python
def service_method(self, data: InputSchema, user: User) -> OutputModel:
    """Standard service method pattern."""
    try:
        # 1. Validation
        self._validate_input(data)

        # 2. Authorization
        self._check_permissions(user)

        # 3. Business Logic
        result = self._perform_operation(data)

        # 4. Database Transaction
        self.db.commit()

        # 5. Return Result
        return result

    except Exception as e:
        self.db.rollback()
        raise self._handle_error(e)
```

## 📂 File Service (`file_service.py`)

### Purpose
Manages all file-related operations including upload, metadata management, and file lifecycle.

### Key Operations
```python
class FileService:
    async def upload_file(self, file: UploadFile, user: User) -> MediaFile:
        """Complete file upload pipeline."""
        # Validation, storage, database record creation

    def get_user_files(self, user: User, filters: dict) -> List[MediaFile]:
        """Retrieve files with advanced filtering."""
        # Query building, permission checking, filtering

    def update_file_metadata(self, file_id: int, updates: MediaFileUpdate, user: User) -> MediaFile:
        """Update file metadata with validation."""
        # Authorization, validation, database update

    def delete_file(self, file_id: int, user: User) -> None:
        """Complete file deletion (storage + database)."""
        # Authorization, storage cleanup, database deletion
```

### Usage Example
```python
# In API endpoint
@router.post("/files", response_model=MediaFileSchema)
async def upload_file(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user)
):
    file_service = FileService(db)
    return await file_service.upload_file(file, current_user)
```

### Features
- **Complete Upload Pipeline**: Validation → Storage → Database → Task Dispatch
- **Advanced Filtering**: Complex query building with multiple filter criteria
- **Permission Management**: User ownership validation
- **Storage Integration**: MinIO operations with error handling
- **Tag Management**: File tagging operations
- **Statistics**: User file statistics and analytics

## 🤖 LLM Service (`llm_service.py`)

### Purpose
Provides unified interface for multiple LLM providers with intelligent context processing for transcript summarization and speaker identification.

### Key Features
```python
async def generate_summary(transcript: str, speaker_data: Dict[str, Any]) -> Dict[str, Any]:
    """Generate BLUF format summary with intelligent section processing."""

    # Automatic context detection and chunking
    context_length = await self.get_model_context_length()
    chunks = self._chunk_transcript_intelligently(transcript, context_length)

    if len(chunks) == 1:
        # Single-pass processing for short transcripts
        return await self._process_single_section(chunks[0])
    else:
        # Multi-section processing for long transcripts
        section_summaries = []
        for chunk in chunks:
            section_summary = await self._summarize_transcript_section(chunk)
            section_summaries.append(section_summary)

        # Stitch sections into comprehensive final summary
        return await self._stitch_section_summaries(section_summaries)
```

### Supported Providers
- **vLLM**: Local high-performance inference
- **OpenAI**: GPT-4, GPT-3.5-turbo
- **Ollama**: Local consumer GPU inference
- **Anthropic**: Claude models
- **OpenRouter**: Multi-provider gateway

### Intelligent Context Processing
- **Automatic Detection**: Queries model endpoints for actual context limits
- **Smart Chunking**: Splits at natural boundaries (speakers, topics, sentences)
- **Section Analysis**: Each chunk processed with full context awareness
- **Summary Stitching**: Combines sections into comprehensive BLUF format
- **Universal Compatibility**: Works with 4K to 200K+ token models

## 🗄️ MinIO Service (`minio_service.py`)

### Purpose
Handles all object storage operations including file upload, download, streaming, and management.

### Key Operations
```python
def upload_file(file_content: IO, file_size: int, object_name: str, content_type: str) -> None:
    """Upload file to MinIO storage."""

def download_file(object_name: str) -> Tuple[IO, int, str]:
    """Download file from MinIO storage."""

def upload_bytes(self, bucket_name: str, object_name: str, data: bytes, content_type: str) -> None:
    """Upload an in-memory bytes payload (e.g. a generated ZIP) to MinIO."""

def delete_file(object_name: str) -> None:
    """Delete file from MinIO storage."""

def get_file_url(object_name: str, expires: int = 3600) -> str:
    """Generate presigned URL for inline file access (playback/thumbnails)."""

def get_presigned_download_url(object_name: str, *, download_filename: str, content_type: str) -> str:
    """Generate presigned URL that forces an attachment download."""
```

### Streaming Features
```python
# Media playback and downloads use presigned MinIO URLs, NOT API byte-proxying.
# The browser streams directly from object storage via the presigned /s3 URL, which
# supports HTTP range requests (206) natively for video seeking. Clients obtain URLs
# from GET /files/{uuid}/stream-url (playback) or POST /files/{uuid}/prepare-download.
```

### Features
- **Efficient Upload/Download**: Chunked file operations
- **Presigned Streaming**: Range-capable playback served directly from MinIO (no API proxy)
- **Presigned URLs**: Secure temporary file access
- **Error Handling**: Comprehensive MinIO error handling
- **Metadata Management**: File metadata and content-type handling

## 🧹 File Cleanup Service (`file_cleanup_service.py`)

### Purpose
Provides automated file recovery, cleanup operations, and system health monitoring for files stuck in processing or error states.

### Key Operations
```python
class FileCleanupService:
    def run_cleanup_cycle(self) -> Dict[str, Any]:
        """Run a complete cleanup cycle detecting and recovering stuck files."""

    def force_cleanup_orphaned_files(self, db: Session, dry_run: bool = False) -> Dict[str, Any]:
        """Force cleanup of orphaned files (admin operation)."""

    def get_cleanup_statistics(self, db: Session) -> Dict[str, Any]:
        """Get current cleanup statistics and system health metrics."""
```

### Auto-Recovery Features
```python
def _attempt_file_recovery(self, db: Session, file_id: int) -> bool:
    """Attempt to recover a single stuck file with intelligent retry logic."""
    # Check recovery attempt limits
    if media_file.recovery_attempts >= self.max_recovery_attempts:
        # Mark as permanently orphaned and eligible for force deletion
        media_file.status = FileStatus.ORPHANED
        media_file.force_delete_eligible = True
        return False

    # Use enhanced recovery logic from task_utils
    return recover_stuck_file(db, file_id)
```

### Health Monitoring
```python
def _generate_health_recommendations(self, db: Session) -> List[str]:
    """Generate system health recommendations based on file states."""
    recommendations = []

    # Calculate error rates and health metrics
    error_rate = status_counts.get('error', 0) / max(sum(status_counts.values()), 1)
    if error_rate > 0.1:  # More than 10% error rate
        recommendations.append(
            f"High error rate detected: {error_rate:.1%} of files are in error state. "
            "Consider investigating processing pipeline health."
        )

    return recommendations
```

### Features
- **Automatic Stuck File Detection**: Identifies files stuck in processing or pending states
- **Intelligent Recovery**: Multi-step recovery process with retry limits
- **Health Monitoring**: System health metrics and recommendations
- **Force Cleanup**: Admin tools for removing orphaned files
- **Statistics Tracking**: Comprehensive cleanup statistics and performance metrics
- **Scheduled Operations**: Designed for periodic background execution

### Integration with Task System
The cleanup service works closely with the new cleanup tasks and enhanced task utilities:
- Uses `check_for_stuck_files()` from `task_utils.py` for detection
- Calls `recover_stuck_file()` for automated recovery
- Integrates with `cancel_active_task()` for safe task cancellation
- Provides statistics for monitoring and alerting systems

## 📊 Analytics Service (`analytics_service.py`)

### Purpose
Provides comprehensive server-side analytics computation for media files including speaker analytics, conversation flow analysis, and meeting efficiency metrics.

### Key Operations
```python
class AnalyticsService:
    @staticmethod
    def compute_analytics(db: Session, media_file_id: int) -> Optional[Dict[str, Any]]:
        """Compute comprehensive analytics for a media file."""
        # Speaker talk time analysis, interruption detection, turn-taking patterns

    @staticmethod
    def save_analytics(db: Session, media_file_id: int, analytics_data: Dict[str, Any]) -> bool:
        """Save computed analytics to the database."""
        # Store analytics with user association

    @staticmethod
    def refresh_analytics(db: Session, media_file_id: int) -> bool:
        """Refresh analytics by recomputing them."""
        # Recompute and update existing analytics
```

### Analytics Features
- **Speaker Talk Time Analysis**: Percentage breakdowns and total speaking time
- **Interruption Detection**: Frequency tracking and pattern analysis
- **Turn-Taking Patterns**: Conversation flow and speaker interaction analysis
- **Question Frequency**: Track question asking by speaker
- **Speaking Pace**: Words per minute calculations
- **Silence Ratio**: Meeting efficiency analysis
- **Word Count Statistics**: Comprehensive word usage across speakers

## 🎨 Formatting Service (`formatting_service.py`)

### Purpose
Centralizes all data formatting logic for consistent display across the application, handling dates, durations, file sizes, and speaker information.

### Key Operations
```python
class FormattingService:
    @staticmethod
    def format_duration(seconds: float) -> str:
        """Format seconds to MM:SS format."""

    @staticmethod
    def format_bytes_detailed(size_bytes: int) -> str:
        """Format file size with appropriate units (B, KB, MB, GB)."""

    @staticmethod
    def format_speaker_name(speaker: Speaker) -> str:
        """Format speaker name with verification status indicators."""

    @staticmethod
    def get_status_badge_class(status: str) -> str:
        """Generate CSS class for status badges."""
```

### Formatting Features
- **Duration Formatting**: Seconds to MM:SS and detailed time formats
- **File Size Formatting**: Intelligent unit selection with precision
- **Date and Time**: Timezone-aware formatting with relative time
- **Speaker Name Resolution**: Fallback handling for unknown speakers
- **Status Badge Classes**: UI consistency for status indicators
- **File Age Calculation**: Human-readable relative time display

## 🤖 Smart Speaker Suggestion Service (`smart_speaker_suggestion_service.py`)

### Purpose
Implements intelligent speaker identification with consolidated suggestions, priority-based filtering, and automated profile matching.

### Key Operations
```python
class SmartSpeakerSuggestionService:
    @staticmethod
    def get_consolidated_suggestions(db: Session, speaker_id: int, user_id: int) -> List[ConsolidatedSuggestion]:
        """Get consolidated speaker suggestions with intelligent filtering."""
        # Profile-based suggestions prioritized over individual matches

    @staticmethod
    def filter_and_prioritize_suggestions(suggestions: List[Dict[str, Any]]) -> List[ConsolidatedSuggestion]:
        """Filter out unlabeled speakers and consolidate by name."""
        # Remove SPEAKER_XX format names and consolidate duplicates
```

### Smart Features
- **Profile Prioritization**: Verified profiles prioritized over individual matches
- **Intelligent Consolidation**: Duplicate speaker names merged into single suggestions
- **Unlabeled Filtering**: Automatic removal of SPEAKER_XX format names
- **Confidence Scoring**: Advanced confidence calculation with multiple factors
- **Auto-Accept Logic**: High-confidence suggestions marked for automatic acceptance

## 🔗 Profile Embedding Service (`profile_embedding_service.py`)

### Purpose
Manages voice embeddings for speaker profiles, enabling cross-video speaker recognition through vector similarity matching.

### Key Operations
```python
class ProfileEmbeddingService:
    @staticmethod
    def add_speaker_to_profile_embedding(db: Session, speaker_id: int, profile_id: int) -> bool:
        """Add speaker's voice embedding to profile's consolidated embedding."""

    @staticmethod
    def calculate_profile_similarity(db: Session, speaker_embedding: List[float], user_id: int) -> List[Dict[str, Any]]:
        """Calculate similarity between speaker and all user profiles."""
```

### Embedding Features
- **Voice Fingerprinting**: Advanced voice similarity using vector embeddings
- **Cross-Video Matching**: Identify same speakers across different recordings
- **Consolidated Embeddings**: Profile-level voice signatures from multiple recordings
- **Similarity Scoring**: Cosine similarity with confidence thresholds
- **Automatic Profile Updates**: Real-time embedding consolidation

## 🏷️ Speaker Status Service (`speaker_status_service.py`)

### Purpose
Manages comprehensive speaker verification status with computed fields for optimized UI display.

### Key Operations
```python
class SpeakerStatusService:
    @staticmethod
    def add_computed_status(speaker: Speaker) -> None:
        """Add computed status fields to speaker object."""
        # Verification status, suggestion availability, confidence levels

    @staticmethod
    def get_verification_status(speaker: Speaker) -> str:
        """Get human-readable verification status."""
```

### Status Features
- **Verification Tracking**: Comprehensive verification status management
- **Computed Fields**: UI-optimized fields added to speaker objects
- **Status Indicators**: Human-readable status text for display
- **Suggestion Availability**: Track whether suggestions are available

## 🔧 Error Categorization Service (`error_categorization_service.py`)

### Purpose
Provides intelligent error classification with user-friendly suggestions and retry guidance for improved error handling.

### Key Operations
```python
class ErrorCategorizationService:
    @staticmethod
    def get_error_info(error_message: str) -> Dict[str, Any]:
        """Categorize error and provide user-friendly information."""
        # Error category, suggestions, retry guidance

    @staticmethod
    def categorize_error(error_message: str) -> str:
        """Categorize error into predefined categories."""
```

### Error Features
- **Intelligent Classification**: Pattern-based error categorization
- **User-Friendly Suggestions**: Actionable guidance for error resolution
- **Retry Guidance**: Determine if errors are retryable
- **Category-Specific Advice**: Tailored suggestions based on error type

## 📋 Task Filtering Service (`task_filtering_service.py`)

### Purpose
Optimizes data retrieval and filtering for improved performance and user experience.

### Key Operations
```python
class TaskFilteringService:
    @staticmethod
    def filter_tasks_by_criteria(db: Session, user_id: int, criteria: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Filter tasks based on various criteria."""

    @staticmethod
    def optimize_query_performance(query: Any) -> Any:
        """Optimize database queries for better performance."""
```

### Filtering Features
- **Efficient Data Retrieval**: Optimized database queries
- **Multi-Criteria Filtering**: Complex filtering logic
- **Performance Optimization**: Query optimization for large datasets
- **User-Specific Filtering**: Context-aware filtering based on user permissions

## Protocol Interfaces (`interfaces.py`)

Defines `Protocol` interfaces for Storage, Search, Cache, and Notification abstractions. Use these for type hints when writing code that should be testable against mocks.

## Notification Service (`notification_service.py`)

Unified `send_task_notification()` wrapper used by all tasks and services. Import this rather than calling WebSocket send directly.

## Progress Tracker (`progress_tracker.py`)

Preferred progress tracking implementation using EWMA (Exponentially Weighted Moving Average) for ETA estimation. Use this in new tasks rather than manual progress calculations.

## Hybrid Search Service (`hybrid_search_service.py`)

Implements hybrid BM25+neural search. Contains a critical fix for OpenSearch 3.4: using `aggs` (cardinality aggregations) with `hybrid` query + `collapse` + RRF `search_pipeline` causes an `ArrayIndexOutOfBoundsException` in OS 3.4. The fix removes `aggs` from hybrid query bodies; `total_files` is computed from result length instead.

## OpenSearch Service (`opensearch_service.py`)

### Cosine Score Conversion — Critical

OpenSearch `cosinesimil` space type returns `(1 + cosine) / 2`, not raw cosine. A raw cosine of 0.50 appears as 0.75 in OpenSearch scores. All kNN score reads must convert:

```python
raw_cosine = 2.0 * hit["_score"] - 1.0
```

This conversion is applied at 8 locations across `opensearch_service.py`, `profile_embedding_service.py`, `speaker_matching_service.py`, `smart_speaker_suggestion_service.py`, and `similarity_service.py`.

### Speaker Index Alias Architecture

Speaker indices use an alias-based architecture:
- `speakers_v3`: concrete index, 512-dim (pyannote/embedding)
- `speakers_v4`: concrete index, 256-dim (WeSpeaker)
- `speakers`: OpenSearch ALIAS pointing to the active versioned index

Use `get_write_index()` and `get_active_versioned_index()` — never reference `speakers_v3`/`speakers_v4` directly outside `opensearch_service.py`.

## OpenSearch Service (`opensearch_service.py`)

### Purpose
Manages full-text search, indexing, and search analytics for transcripts and files.

### Key Operations
```python
def index_transcript(file_id: int, user_id: int, full_transcript: str, speaker_names: List[str], file_title: str) -> None:
    """Index transcript for full-text search."""

def search_transcripts(user_id: int, query: str, filters: dict = None) -> List[Dict]:
    """Search across user's transcripts."""

def add_speaker_embedding(speaker_id: int, embedding_vector: List[float]) -> None:
    """Store speaker voice embedding for similarity search."""

def search_similar_speakers(embedding_vector: List[float], user_id: int) -> List[Dict]:
    """Find similar speakers using vector search."""
```

### Search Features
```python
def build_search_query(query: str, filters: dict) -> Dict:
    """Build complex OpenSearch query."""
    search_body = {
        "query": {
            "bool": {
                "must": [
                    {"match": {"transcript": query}}
                ],
                "filter": []
            }
        },
        "highlight": {
            "fields": {"transcript": {}}
        }
    }

    # Add filters (date range, speakers, file types, etc.)
    if filters.get('from_date'):
        search_body["query"]["bool"]["filter"].append({
            "range": {"created_at": {"gte": filters['from_date']}}
        })

    return search_body
```

### Features
- **Full-text Search**: Advanced search across transcripts
- **Faceted Search**: Filter by speakers, dates, file types
- **Highlighting**: Search term highlighting in results
- **Vector Search**: Speaker similarity using voice embeddings
- **Analytics**: Search performance and usage analytics

## 🔄 Service Integration Patterns

### Cross-Service Operations
```python
class FileService:
    def delete_file(self, file_id: int, user: User) -> None:
        """Complete file deletion across all services."""
        try:
            # 1. Get file information
            file_obj = self._get_file_with_permission_check(file_id, user)

            # 2. Delete from object storage
            minio_service.delete_file(file_obj.storage_path)

            # 3. Remove from search index
            opensearch_service.delete_document(file_id)

            # 4. Cancel any running tasks
            transcription_service.cancel_file_tasks(file_id)

            # 5. Delete from database (cascades to related records)
            self.db.delete(file_obj)
            self.db.commit()

        except Exception as e:
            self.db.rollback()
            raise self._handle_deletion_error(e)
```

### Service Dependencies
```python
# Services can call other services when needed
class TranscriptionService:
    def __init__(self, db: Session):
        self.db = db
        self.file_service = FileService(db)  # Dependency injection

    def start_transcription(self, file_id: int, user: User):
        # Use file service for validation
        file_obj = self.file_service.get_file_by_id(file_id, user)
        # Continue with transcription logic...
```

## 🧪 Testing Services

### Service Testing Patterns
```python
class TestFileService:
    def test_upload_file_success(self, db_session, mock_user, mock_upload_file):
        """Test successful file upload."""
        file_service = FileService(db_session)

        # Mock external dependencies
        with patch('app.services.minio_service.upload_file'):
            result = await file_service.upload_file(mock_upload_file, mock_user)

        assert result.filename == mock_upload_file.filename
        assert result.user_id == mock_user.id

    def test_delete_file_unauthorized(self, db_session, mock_user, other_user_file):
        """Test unauthorized file deletion."""
        file_service = FileService(db_session)

        with pytest.raises(HTTPException) as exc_info:
            file_service.delete_file(other_user_file.id, mock_user)

        assert exc_info.value.status_code == 403
```

### Mocking External Services
```python
# Mock external service calls in tests
@patch('app.services.minio_service.upload_file')
@patch('app.services.opensearch_service.index_transcript')
def test_complete_transcription_workflow(mock_opensearch, mock_minio, db_session):
    """Test full transcription workflow with mocked external services."""
    # Test logic without actual external API calls
```

## 📊 Error Handling in Services

### Standardized Error Responses
```python
from app.utils.error_handlers import ErrorHandler

class FileService:
    def get_file_by_id(self, file_id: int, user: User) -> MediaFile:
        """Get file with proper error handling."""
        file_obj = self.db.query(MediaFile).filter(
            MediaFile.id == file_id,
            MediaFile.user_id == user.id
        ).first()

        if not file_obj:
            raise ErrorHandler.not_found_error("File")

        return file_obj

    def _handle_upload_error(self, error: Exception) -> None:
        """Handle upload-specific errors."""
        if isinstance(error, MinIOError):
            raise ErrorHandler.file_processing_error("storage upload", error)
        elif isinstance(error, ValidationError):
            raise ErrorHandler.validation_error(str(error))
        else:
            raise ErrorHandler.database_error("file creation", error)
```

## 🚀 Performance Considerations

### Database Optimization
```python
class FileService:
    def get_user_files_with_stats(self, user: User) -> List[Dict]:
        """Optimized query with eager loading."""
        return self.db.query(MediaFile)\
            .options(
                joinedload(MediaFile.transcript_segments),
                joinedload(MediaFile.comments),
                selectinload(MediaFile.file_tags).joinedload(FileTag.tag)
            )\
            .filter(MediaFile.user_id == user.id)\
            .all()
```

### Caching Strategies
```python
from functools import lru_cache

class TranscriptionService:
    @lru_cache(maxsize=100)
    def get_user_speakers_cached(self, user_id: int) -> List[Speaker]:
        """Cache frequently accessed speaker data."""
        return self.db.query(Speaker)\
            .filter(Speaker.user_id == user_id)\
            .all()
```

## 🔧 Adding New Services

### Service Creation Checklist
1. **Define Purpose**: Clear single responsibility
2. **Design Interface**: Public methods and their signatures
3. **Error Handling**: Use standardized error patterns
4. **Dependencies**: Inject required services/utilities
5. **Testing**: Comprehensive unit tests with mocks
6. **Documentation**: Clear docstrings and examples

### Service Template
```python
from typing import List, Optional
from sqlalchemy.orm import Session

from app.models.user import User
from app.utils.error_handlers import ErrorHandler
from app.utils.auth_decorators import AuthorizationHelper

class NewService:
    """Service for handling [specific domain] operations."""

    def __init__(self, db: Session):
        self.db = db

    def create_resource(self, data: CreateSchema, user: User) -> Resource:
        """Create a new resource with validation."""
        try:
            # Validation
            self._validate_create_data(data)

            # Business logic
            resource = self._perform_create_operation(data, user)

            # Database transaction
            self.db.add(resource)
            self.db.commit()
            self.db.refresh(resource)

            return resource

        except Exception as e:
            self.db.rollback()
            raise self._handle_create_error(e)

    def _validate_create_data(self, data: CreateSchema) -> None:
        """Private validation method."""
        # Validation logic
        pass

    def _handle_create_error(self, error: Exception) -> None:
        """Private error handling method."""
        # Error handling logic
        pass
```

---

The services layer provides a clean, testable, and maintainable way to implement business logic while keeping API endpoints focused on HTTP concerns and database models focused on data representation.
