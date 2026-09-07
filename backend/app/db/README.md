<div align="center">
  <img src="../../../assets/logo-banner.png" alt="OpenTranscribe Logo" width="200">

  # Database Layer Documentation
</div>

This directory contains core database configuration, session management, and the comprehensive database management approach for OpenTranscribe.

## 📁 Database Components

```
db/
├── base.py           # Core SQLAlchemy setup and configuration
├── migrations.py     # Startup Alembic runner + schema version detection
├── session_utils.py  # Session management utilities
└── README.md         # This documentation
```

## 🔧 Core Database Setup (`base.py`)

### Purpose
Provides the fundamental SQLAlchemy configuration and database connection setup.

### Key Components
```python
# Database engine with connection pooling
engine = create_engine(settings.DATABASE_URL)

# Session factory for database transactions
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for all ORM models
Base = declarative_base()

# FastAPI dependency for database sessions
def get_db():
    """Dependency injection for database sessions in API endpoints."""
```

### Usage in API Endpoints
```python
@router.get("/files")
def get_files(db: Session = Depends(get_db)):
    """Automatic session management via dependency injection."""
    return db.query(MediaFile).all()
```

## 🔄 Session Management (`session_utils.py`)

### Purpose
Provides advanced session management patterns for different use cases, particularly background tasks and complex transactions.

### Key Utilities

#### Context Manager for Transactions
```python
@contextmanager
def session_scope():
    """Provide a transactional scope around a series of operations."""
    # Automatic commit/rollback handling
    # Exception safety with proper cleanup
```

**Usage:**
```python
# Background tasks with automatic transaction management
with session_scope() as db:
    file_obj = db.query(MediaFile).get(file_id)
    file_obj.status = FileStatus.PROCESSING
    # Automatic commit on success, rollback on exception
```

#### Object Refresh Utilities
```python
def get_refreshed_object(db: Session, model_class, obj_id: int):
    """Get a fresh copy of an object from the database."""
    # Handles detached objects in Celery tasks
    # Provides fallback session creation
```

**Usage:**
```python
# Celery task with detached object handling
@celery_app.task
def process_file_task(file_id: int):
    with session_scope() as db:
        # Get fresh object instance
        file_obj = get_refreshed_object(db, MediaFile, file_id)
        # Process with active session
```

### Session Patterns

#### API Endpoints (Dependency Injection)
```python
def api_endpoint(db: Session = Depends(get_db)):
    """FastAPI automatically manages session lifecycle."""
    # Session created for request
    # Automatic cleanup after response
```

#### Background Tasks (Context Manager)
```python
def background_task():
    """Background tasks manage their own sessions."""
    with session_scope() as db:
        # Explicit transaction control
        # Exception-safe cleanup
```

#### Service Layer (Injected Session)
```python
class FileService:
    def __init__(self, db: Session):
        self.db = db

    def process_file(self, file_id: int):
        # Uses injected session from endpoint
        # Participates in request transaction
```

# Database Management Approach

## Overview

OpenTranscribe uses Alembic migrations as the sole authority for database schema management across all environments. The bootstrap migration creates the full schema from scratch, and subsequent migrations handle schema evolution.

## Development Approach

### Current Setup
- Database schema is defined by Alembic migrations in `backend/alembic/versions/`
- SQLAlchemy models in `app/models/` reflect the schema
- Pydantic schemas in `app/schemas/` handle API validation

### Enhanced Database Schema Features

The current database includes several new tables and enhancements for advanced functionality:

#### User Settings Tables
- **`user_settings`**: User-specific configuration storage with key-value pairs
  - Recording preferences (duration, quality, auto-stop settings)
  - LLM provider configurations with encrypted API key storage
  - Custom user interface preferences

#### AI Integration Tables
- **`user_llm_configurations`**: Per-user LLM provider settings
  - Support for multiple providers (OpenAI, Claude, vLLM, Ollama, etc.)
  - Encrypted API key storage with user-specific encryption
  - Connection testing and validation history

- **`summary_prompts`**: Custom AI prompts for different content types
  - User-created prompt templates for meetings, interviews, podcasts
  - Content type categorization and organization
  - Prompt versioning and management

- **`file_summaries`**: AI-generated summaries with metadata
  - BLUF format structured summaries
  - Processing status and error tracking
  - OpenSearch integration for searchable summaries

#### Enhanced Media Processing
- **Enhanced `media_files` table**: Additional fields for improved processing
  - YouTube URL processing metadata
  - Enhanced error tracking and recovery information
  - Improved file status management with granular progress tracking

- **Notification System Tables**: Enhanced WebSocket notification persistence
  - User-specific notification preferences
  - Progress tracking for long-running AI operations
  - Real-time status updates for uploads and processing

### Development Workflow
1. **Schema Changes**: Create a new Alembic migration in `backend/alembic/versions/`
2. **Model Updates**: Modify SQLAlchemy models in `app/models/`
3. **Schema Updates**: Update Pydantic schemas in `app/schemas/`
4. **Migration Detection**: Update `app/db/migrations.py` for the new version
5. **Reset Database**: Run `./opentr.sh reset dev` to test full migration chain

## Production Approach

### Alembic Setup
- Alembic is configured in `alembic.ini`
- Migration files in `backend/alembic/versions/` — v010 (baseline) through v355
- `migrations.py` runs automatically on backend startup: detects current version, stamps untracked databases, runs `alembic upgrade head`
- Migration chain covers ~35 versions; see `alembic/versions/` for the full list

### Production Deployment
1. **Create Migration**: Add a new migration in `backend/alembic/versions/`
2. **Review Migration**: Ensure migration is safe and correct
3. **Deploy**: Backend runs `alembic upgrade head` automatically on startup
4. **Verify**: Confirm schema changes applied correctly

## Directory Structure

```
backend/
├── alembic/                    # Alembic migration framework
│   ├── versions/              # Migration files (v010–v355)
│   ├── env.py                 # Alembic environment config
│   └── script.py.mako         # Migration template
├── alembic.ini                # Alembic configuration
└── app/
    ├── db/                   # Database layer (session management)
    │   ├── base.py           # Engine, SessionLocal, Base
    │   ├── migrations.py     # Startup runner + version detection
    │   └── session_utils.py  # session_scope(), get_refreshed_object()
    ├── models/               # SQLAlchemy ORM models (~20 files)
    └── schemas/              # Pydantic validation schemas
```

## Commands Reference

### Development Commands
```bash
# Reset development database
./opentr.sh reset dev

# Inspect current database
python scripts/db_inspect.py
```

### Migration Commands
```bash
# Apply all pending migrations (also runs automatically on startup)
alembic upgrade head

# Check current version
alembic current

# Rollback to previous version
alembic downgrade -1
```

## Best Practices

### During Development
- Always update all three: Alembic migration, SQLAlchemy models, and Pydantic schemas
- Test changes with `./opentr.sh reset dev`
- Document significant schema changes
- Keep SQLAlchemy models in sync with Alembic migrations

### Session Management
- Use `get_db()` dependency in API endpoints
- Use `session_scope()` context manager in background tasks
- Always handle detached objects in Celery tasks
- Prefer service layer injection for complex business logic

### For Production
- Review all migrations before deploying
- Test migrations on staging database
- Backup database before applying migrations
- Use descriptive migration messages
- Never edit existing migration files that have been deployed

## Troubleshooting

### Common Issues
- **Model/Schema Mismatch**: Check that SQLAlchemy models match Alembic migrations
- **Validation Errors**: Ensure Pydantic schemas match SQLAlchemy models
- **Database State**: Use `./opentr.sh reset dev` to start fresh
- **Session Errors**: Use `session_scope()` for background tasks
- **Detached Objects**: Use `get_refreshed_object()` in Celery tasks

### Debug Tools
- `scripts/db_inspect.py` - Inspect current database state
- `scripts/query_tags.py` - Debug tag-related tables
- Database logs via `./opentr.sh logs postgres`

## Performance Considerations

### Connection Pooling
```python
# Engine configuration for optimal performance
engine = create_engine(
    settings.DATABASE_URL,
    pool_size=20,        # Connection pool size
    max_overflow=30,     # Additional connections when pool is full
    pool_pre_ping=True,  # Validate connections before use
    pool_recycle=3600    # Recycle connections every hour
)
```

### Session Best Practices
- Keep sessions short-lived
- Avoid long-running transactions
- Use bulk operations for large datasets
- Implement proper error handling and rollback

### Query Optimization
- Use eager loading for related objects
- Implement pagination for large result sets
- Use indexes for frequently queried columns
- Monitor query performance with logging

## References

- [SQLAlchemy Session Documentation](https://docs.sqlalchemy.org/en/14/orm/session_basics.html)
- [FastAPI Database Tutorial](https://fastapi.tiangolo.com/tutorial/sql-databases/)
- [Alembic Documentation](https://alembic.sqlalchemy.org/)
- [SQLAlchemy Connection Pooling](https://docs.sqlalchemy.org/en/14/core/pooling.html)

---

The database layer provides a robust foundation for data persistence with proper session management, transaction handling, and development/production workflow support.
