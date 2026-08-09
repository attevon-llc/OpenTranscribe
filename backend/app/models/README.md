<div align="center">
  <img src="../../../assets/logo-banner.png" alt="OpenTranscribe Logo" width="250">

  # Data Models Documentation
</div>

This directory contains SQLAlchemy ORM models that define the database schema and relationships for OpenTranscribe.

## 🗄️ Database Schema Overview

The OpenTranscribe database is designed around media files, users, and AI-powered transcription with the following core entities:

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│    User     │────▶│ MediaFile   │────▶│TranscriptSeg│
└─────────────┘     └─────────────┘     └─────────────┘
                           │                     │
                           ▼                     ▼
                    ┌─────────────┐     ┌─────────────┐
                    │   Comment   │     │   Speaker   │
                    └─────────────┘     └─────────────┘
                           │
                           ▼
                    ┌─────────────┐
                    │    Tag      │
                    └─────────────┘
```

## Model Files

### Core Models
- `user.py` — User authentication and authorization
- `media.py` — Media files, transcript segments, speakers, tasks, comments, tags

### Authentication & Security (v0.4.0)
- `user_mfa.py` — MFA/TOTP settings per user (RFC 6238)
- `password_history.py` — Password history tracking for reuse prevention
- `refresh_token.py` — Refresh token management (rotation)
- `password_reset.py` — Password reset token tracking
- `auth_config.py` — Authentication provider configuration (LDAP/OIDC/PKI settings)

### User Settings & Configuration
- `user_llm_settings.py` — Per-user LLM provider settings with encrypted API keys
- `user_asr_settings.py` — Per-user ASR provider settings
- `user_diarization_settings.py` — Per-user diarization preferences (min/max speakers)
- `user_media_source.py` — User-specific media source configurations
- `system_settings.py` — System-wide admin settings (key-value store)

### Collaboration & Sharing (v0.4.0)
- `group.py` — User groups
- `sharing.py` — Collection sharing with permissions (`collection_share` table)
- `prompt.py` — Shared prompt templates (shared_prompt, summary_prompt)

### Media & Processing
- `upload_batch.py` — Upload session/batch tracking
- `topic.py` — LLM-extracted topics per transcript
- `custom_vocabulary.py` — Per-user custom vocabulary for transcription

### Mixins & Utilities
- `mixins.py` — Shared model mixins (timestamps, soft-delete, etc.)

## 👤 User Models (`user.py`)

### User
The main user entity for authentication and authorization.

```python
class User(Base):
    __tablename__ = "user"

    # Primary Key
    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Authentication
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String, nullable=False)

    # Profile
    full_name: Mapped[Optional[str]] = mapped_column(String)

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False)
    role: Mapped[str] = mapped_column(String, default="user")

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())

    # Relationships
    media_files: Mapped[List["MediaFile"]] = relationship("MediaFile", back_populates="user")
    comments: Mapped[List["Comment"]] = relationship("Comment", back_populates="user")
    speakers: Mapped[List["Speaker"]] = relationship("Speaker", back_populates="user")
    tasks: Mapped[List["Task"]] = relationship("Task", back_populates="user")
```

**Key Features:**
- **JWT Authentication**: Email-based login with hashed passwords
- **Role-based Access**: User/admin roles with is_superuser flag
- **Soft Delete**: is_active flag for account deactivation
- **Audit Trail**: Created/updated timestamps
- **Relationships**: One-to-many with all user-owned resources

## 📁 Media Models (`media.py`)

### MediaFile
Central entity representing uploaded audio/video files.

```python
class MediaFile(Base):
    __tablename__ = "media_file"

    # Primary Key
    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # File Information
    filename: Mapped[str] = mapped_column(String, nullable=False)
    file_size: Mapped[int] = mapped_column(Integer)
    content_type: Mapped[str] = mapped_column(String)
    storage_path: Mapped[str] = mapped_column(String)

    # Processing Status
    status: Mapped[FileStatus] = mapped_column(Enum(FileStatus), default=FileStatus.PENDING)

    # Media Metadata
    duration: Mapped[Optional[float]] = mapped_column(Float)
    language: Mapped[Optional[str]] = mapped_column(String)

    # AI Processing Results
    summary: Mapped[Optional[str]] = mapped_column(Text)
    translated_text: Mapped[Optional[str]] = mapped_column(Text)

    # Rich Metadata (JSON fields)
    metadata: Mapped[Optional[dict]] = mapped_column(JSON)  # Full ExifTool output
    metadata_important: Mapped[Optional[dict]] = mapped_column(JSON)  # Curated fields

    # Video-specific
    resolution_width: Mapped[Optional[int]] = mapped_column(Integer)
    resolution_height: Mapped[Optional[int]] = mapped_column(Integer)
    frame_rate: Mapped[Optional[float]] = mapped_column(Float)
    codec: Mapped[Optional[str]] = mapped_column(String)

    # Audio-specific
    audio_channels: Mapped[Optional[int]] = mapped_column(Integer)
    audio_sample_rate: Mapped[Optional[int]] = mapped_column(Integer)
    audio_bit_depth: Mapped[Optional[int]] = mapped_column(Integer)

    # Ownership & Timestamps
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("user.id"), nullable=False)
    upload_time: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="media_files")
    transcript_segments: Mapped[List["TranscriptSegment"]] = relationship("TranscriptSegment", back_populates="media_file")
    comments: Mapped[List["Comment"]] = relationship("Comment", back_populates="media_file")
    file_tags: Mapped[List["FileTag"]] = relationship("FileTag", back_populates="media_file")
    tasks: Mapped[List["Task"]] = relationship("Task", back_populates="media_file")
    analytics: Mapped[Optional["Analytics"]] = relationship("Analytics", back_populates="media_file", uselist=False)
```

**Key Features:**
- **File Management**: Storage path, size, content type tracking
- **Processing Pipeline**: Status tracking (PENDING, PROCESSING, COMPLETED, ERROR, CANCELLING, CANCELLED, ORPHANED) — `FileStatus` enum centralized in `app/core/enums.py`
- **Rich Metadata**: JSON fields for flexible metadata storage
- **Media Specifications**: Video/audio technical details
- **AI Integration**: Summary and translation storage
- **v0.4.0 Columns**: `diarization_disabled`, `ai_summary_disabled`, `requested_whisper_model`, `diarization_source`
- **Audit Trail**: Upload time and completion tracking

### TranscriptSegment
Individual segments of transcribed text with timing and speaker information.

```python
class TranscriptSegment(Base):
    __tablename__ = "transcript_segment"

    # Primary Key
    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Foreign Keys
    media_file_id: Mapped[int] = mapped_column(Integer, ForeignKey("media_file.id"), nullable=False)
    speaker_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("speaker.id"))

    # Timing Information
    start_time: Mapped[float] = mapped_column(Float, nullable=False)
    end_time: Mapped[float] = mapped_column(Float, nullable=False)

    # Content
    text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[Optional[float]] = mapped_column(Float)

    # Word-level Data (JSON)
    words: Mapped[Optional[dict]] = mapped_column(JSON)  # Word-level timing from WhisperX

    # Ordering
    segment_index: Mapped[Optional[int]] = mapped_column(Integer)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())

    # Relationships
    media_file: Mapped["MediaFile"] = relationship("MediaFile", back_populates="transcript_segments")
    speaker: Mapped[Optional["Speaker"]] = relationship("Speaker", back_populates="transcript_segments")
```

**Key Features:**
- **Precise Timing**: Start/end times for video synchronization
- **Speaker Attribution**: Links to speaker identification
- **Word-level Data**: Detailed timing for each word (from WhisperX)
- **Confidence Scores**: AI transcription confidence metrics
- **Searchable Text**: Full-text search capability

### Speaker
Speaker identification and management from AI diarization.

```python
class Speaker(Base):
    __tablename__ = "speaker"

    # Primary Key
    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Speaker Identity
    name: Mapped[str] = mapped_column(String, nullable=False)  # AI-generated (SPEAKER_01)
    display_name: Mapped[Optional[str]] = mapped_column(String)  # User-assigned
    uuid: Mapped[str] = mapped_column(String, nullable=False)  # Cross-file speaker tracking

    # Voice Characteristics (for future ML features)
    voice_embedding: Mapped[Optional[bytes]] = mapped_column(LargeBinary)
    characteristics: Mapped[Optional[dict]] = mapped_column(JSON)

    # User Management
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("user.id"), nullable=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="speakers")
    transcript_segments: Mapped[List["TranscriptSegment"]] = relationship("TranscriptSegment", back_populates="speaker")
```

**Key Features:**
- **AI Integration**: Generated from PyAnnote diarization
- **Cross-file Tracking**: UUID for speaker consistency across files
- **User Customization**: Display names and verification
- **Voice Biometrics**: Storage for voice embeddings (future ML features)
- **Segment Linking**: Connection to all speaker's spoken segments

### Task
Background task tracking for long-running AI operations.

```python
class Task(Base):
    __tablename__ = "task"

    # Primary Key (matches Celery task ID)
    id: Mapped[str] = mapped_column(String, primary_key=True)

    # Task Information
    task_type: Mapped[str] = mapped_column(String, nullable=False)  # transcription, analysis, etc.
    status: Mapped[str] = mapped_column(String, default="pending")
    progress: Mapped[float] = mapped_column(Float, default=0.0)

    # Error Handling
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    # Foreign Keys
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("user.id"), nullable=False)
    media_file_id: Mapped[int] = mapped_column(Integer, ForeignKey("media_file.id"), nullable=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="tasks")
    media_file: Mapped["MediaFile"] = relationship("MediaFile", back_populates="tasks")
```

**Key Features:**
- **Celery Integration**: ID matches Celery task ID for tracking
- **Progress Monitoring**: Real-time progress updates
- **Error Handling**: Comprehensive error message storage
- **Task Types**: Support for multiple background operations
- **Performance Metrics**: Start/completion time tracking

### Comment
User annotations and comments on media files.

```python
class Comment(Base):
    __tablename__ = "comment"

    # Primary Key
    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Content
    text: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp: Mapped[Optional[float]] = mapped_column(Float)  # Position in media file

    # Foreign Keys
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("user.id"), nullable=False)
    media_file_id: Mapped[int] = mapped_column(Integer, ForeignKey("media_file.id"), nullable=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), onupdate=func.now())

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="comments")
    media_file: Mapped["MediaFile"] = relationship("MediaFile", back_populates="comments")
```

### Tag & FileTag
Flexible tagging system for file organization.

```python
class Tag(Base):
    __tablename__ = "tag"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    color: Mapped[Optional[str]] = mapped_column(String)

    # Relationships
    file_tags: Mapped[List["FileTag"]] = relationship("FileTag", back_populates="tag")

class FileTag(Base):
    __tablename__ = "file_tag"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_file_id: Mapped[int] = mapped_column(Integer, ForeignKey("media_file.id"))
    tag_id: Mapped[int] = mapped_column(Integer, ForeignKey("tag.id"))

    # Relationships
    media_file: Mapped["MediaFile"] = relationship("MediaFile", back_populates="file_tags")
    tag: Mapped["Tag"] = relationship("Tag", back_populates="file_tags")
```

### Analytics
Processing analytics and insights for media files.

```python
class Analytics(Base):
    __tablename__ = "analytics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_file_id: Mapped[int] = mapped_column(Integer, ForeignKey("media_file.id"))

    # Transcript Analytics
    word_count: Mapped[Optional[int]] = mapped_column(Integer)
    speaker_count: Mapped[Optional[int]] = mapped_column(Integer)
    segment_count: Mapped[Optional[int]] = mapped_column(Integer)

    # Processing Metrics
    processing_time: Mapped[Optional[float]] = mapped_column(Float)

    # AI Insights (JSON for flexibility)
    insights: Mapped[Optional[dict]] = mapped_column(JSON)

    # Relationships
    media_file: Mapped["MediaFile"] = relationship("MediaFile", back_populates="analytics")
```

## 🔧 Model Patterns & Best Practices

### Primary Keys
- **Auto-incrementing integers** for most entities
- **String IDs** for Task (matches Celery task IDs)
- **UUIDs** for cross-system tracking (Speaker.uuid)

### Foreign Keys & Relationships
- **Explicit foreign key columns** with proper constraints
- **SQLAlchemy relationships** for ORM navigation
- **Cascade deletes** where appropriate (user → media files)

### Timestamps
- **created_at**: Automatic on creation
- **updated_at**: Automatic on update
- **Domain-specific**: upload_time, completed_at, etc.

### JSON Fields
- **Flexible metadata storage** for varying data structures
- **AI processing results** that don't fit rigid schemas
- **Configuration data** that may evolve

### Enums
```python
class FileStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    ERROR = "error"
```

### Database Constraints
- **NOT NULL** for required fields
- **UNIQUE** constraints for business rules
- **Foreign key constraints** for referential integrity
- **Check constraints** for data validation

## 🔍 Query Patterns

### Common Queries
```python
# Get user's files with transcription status
files_with_status = session.query(MediaFile)\
    .filter(MediaFile.user_id == user_id)\
    .filter(MediaFile.status == FileStatus.COMPLETED)\
    .all()

# Get transcript segments with speaker info
segments = session.query(TranscriptSegment)\
    .join(Speaker)\
    .filter(TranscriptSegment.media_file_id == file_id)\
    .order_by(TranscriptSegment.start_time)\
    .all()

# Full-text search in transcripts
matching_segments = session.query(TranscriptSegment)\
    .join(MediaFile)\
    .filter(MediaFile.user_id == user_id)\
    .filter(TranscriptSegment.text.ilike(f"%{search_term}%"))\
    .all()
```

### Performance Optimization
- **Eager loading**: Use `joinedload()` for related data
- **Lazy loading**: Default for optional relationships
- **Indexing**: Database indexes on frequently queried columns
- **Pagination**: Limit results for large datasets

## 🧪 Testing Models

### Model Testing Patterns
```python
def test_user_creation():
    user = User(
        email="test@example.com",
        hashed_password="hashed_password",
        full_name="Test User"
    )
    assert user.email == "test@example.com"
    assert user.is_active is True  # Default value

def test_media_file_relationships():
    user = User(email="test@example.com", hashed_password="hash")
    media_file = MediaFile(
        filename="test.mp4",
        user_id=user.id,
        storage_path="/path/to/file"
    )
    # Test relationships work correctly
```

## Migration Considerations

### Schema Authority
Alembic migrations (`backend/alembic/versions/`) are the sole authority for schema in all environments. There is no `init_db.sql` — the bootstrap migration (`v010_baseline.py`) creates the full schema from scratch.

### Schema Changes
1. Create Alembic migration in `backend/alembic/versions/` (use idempotent SQL with `IF NOT EXISTS`)
2. Update SQLAlchemy model definitions in `app/models/`
3. Update Pydantic schemas in `app/schemas/`
4. Update `app/db/migrations.py` version detection
5. Test with `./opentr.sh reset dev`

### Data Migration
- **Additive changes**: Generally safe
- **Destructive changes**: Require careful planning
- **Data transformation**: May need custom migration scripts
- **Backups**: Always backup before major migrations

---

This data model provides a robust foundation for OpenTranscribe's media processing and AI transcription capabilities while maintaining flexibility for future enhancements.
