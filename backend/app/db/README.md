<div align="center">
  <img src="../../../assets/logo-banner.png" alt="OpenTranscribe Logo" width="200">

  # Database Layer Documentation
</div>

This directory contains core database configuration, session management, and the comprehensive database management approach for OpenTranscribe.

## 📁 Database Components

```
db/
├── base.py           # Engine, SessionLocal, Base, get_db(), build_libpq_options()
├── migrations.py     # Startup Alembic runner + schema version detection
├── session_utils.py  # session_scope(), get_refreshed_object()
├── CLAUDE.md         # Agent/contributor rules (schema-change checklist, gotchas)
└── README.md         # This documentation
```

## 🔧 Core Database Setup (`base.py`)

### Purpose
Provides the fundamental SQLAlchemy configuration and database connection setup.

### Key Components
```python
# Database engine with connection pooling (simplified; see base.py for the full call)
engine = create_engine(
    settings.DATABASE_URL,
    connect_args=connect_args,          # sslmode + idle_in_transaction_session_timeout
    pool_pre_ping=True,
    pool_recycle=3600,
    pool_size=settings.DB_POOL_SIZE,    # default 20
    max_overflow=settings.DB_MAX_OVERFLOW,  # default 40
)

# Session factory for database transactions
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for all ORM models (SQLAlchemy 2.0 typed declarative; deliberately no naming_convention)
class Base(DeclarativeBase):
    pass

# FastAPI dependency for database sessions
def get_db():
    """Yield a session and close it after the request."""
```

The pool sizes and `DB_IDLE_IN_TRANSACTION_TIMEOUT_MS` (server-side timeout for sessions left idle inside a transaction, default 5 minutes) are settings in `.env.example`. Celery workers fork their own engines, so the pool size mainly matters for the API process.

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
    file_obj = get_refreshed_object(db, MediaFile, file_id)
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
def process_file(db: Session, file_id: int):
    # Uses the session injected from the endpoint
    # Participates in the request transaction
```

# Database Management Approach

## Overview

OpenTranscribe uses Alembic migrations as the sole authority for database schema management across all environments. The bootstrap migration creates the full schema from scratch, and subsequent migrations handle schema evolution.

## Development Approach

### Current Setup
- Database schema is defined by Alembic migrations in `backend/alembic/versions/`
- SQLAlchemy models in `app/models/` reflect the schema
- Pydantic schemas in `app/schemas/` handle API validation

### Where the tables live
Table definitions are grouped by domain in `app/models/`; `app/models/README.md` has the inventory. Core media tables (`media_file`, `transcript_segment`, `speaker`, `speaker_profile`, `tag`, `collection`, `task`, ...) are in `models/media.py`. Per-user configuration lives in `user_setting` (key/value), `user_llm_settings` (encrypted provider keys), `summary_prompt`, and the `user_asr_settings` / `user_diarization_settings` tables; deployment-wide configuration is in `system_settings`. AI summaries are stored as JSON in `media_file.summary_data`.

### Development Workflow
1. **Schema Changes**: Create a new Alembic migration in `backend/alembic/versions/`
2. **Model Updates**: Modify SQLAlchemy models in `app/models/`
3. **Schema Updates**: Update Pydantic schemas in `app/schemas/`
4. **Migration Detection**: Add a detection guard for the new revision in `_detect_schema_version()` in `app/db/migrations.py`
5. **Reset Database**: Run `./opentr.sh reset dev` to test the full migration chain (this deletes all data), and also restart over an existing database to test the upgrade path

## Production Approach

### Alembic Setup
- Alembic is configured in `backend/alembic.ini` (`script_location = alembic`, relative to `backend/`)
- Migration files live in `backend/alembic/versions/`, named `v<NNN>_<description>.py`, from `v010_baseline` to the current head. Derive the head with `python3 scripts/release-tests/lib/alembic-head.py backend` from the repo root.
- `run_migrations()` in `migrations.py` is called from the FastAPI lifespan on every backend startup (skipped only when `RUN_MIGRATIONS_ON_STARTUP=false`): it takes a PostgreSQL advisory lock, detects the current version of an untracked database, stamps it, and runs `alembic upgrade head`. A migration failure aborts startup.
- Only the backend runs migrations; Celery workers do not.

### Production Deployment
1. **Create Migration**: Add a new migration in `backend/alembic/versions/`
2. **Review Migration**: Ensure migration is safe and correct
3. **Deploy**: The backend migrates automatically on startup
4. **Verify**: Confirm schema changes applied correctly (`alembic current` inside the backend container)

## Directory Structure

```
backend/
├── alembic/                    # Alembic migration framework
│   ├── versions/              # Migration files (v<NNN>_<description>.py, v010 baseline onward)
│   ├── env.py                 # Alembic environment config
│   └── script.py.mako         # Migration template
├── alembic.ini                # Alembic configuration
└── app/
    ├── db/                   # Database layer (session management)
    │   ├── base.py           # Engine, SessionLocal, Base
    │   ├── migrations.py     # Startup runner + version detection
    │   └── session_utils.py  # session_scope(), get_refreshed_object()
    ├── models/               # SQLAlchemy ORM models (one module per domain)
    └── schemas/              # Pydantic validation schemas
```

## Commands Reference

### Development Commands
```bash
# Reset development database
./opentr.sh reset dev

# Debug dump of the tag tables (run inside the container: ./opentr.sh shell backend)
python scripts/db_inspect.py
```

### Migration Commands
Run inside the backend container (`./opentr.sh shell backend`) from the `backend/` directory (`/app`).
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
- `scripts/db_inspect.py` - Dump the tag tables
- `scripts/query_tags.py` - Debug tag-related tables
- Database logs via `./opentr.sh logs postgres`

## Performance Considerations

### Connection Pooling
The shared engine in `base.py` uses `pool_pre_ping=True`, `pool_recycle=3600`, and `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` (defaults 20 / 40). Size them against Postgres `max_connections` when adding worker processes.

### Session Best Practices
- Keep sessions short-lived: do not hold a transaction open across HTTP calls, model inference or file I/O (`DB_IDLE_IN_TRANSACTION_TIMEOUT_MS` terminates idle-in-transaction sessions as a backstop)
- Avoid long-running transactions
- Use bulk operations for large datasets
- Implement proper error handling and rollback

### Query Optimization
- Use eager loading for related objects
- Implement pagination for large result sets
- Use indexes for frequently queried columns
- Monitor query performance with logging

## References

- [SQLAlchemy Session Documentation](https://docs.sqlalchemy.org/en/20/orm/session_basics.html)
- [FastAPI Database Tutorial](https://fastapi.tiangolo.com/tutorial/sql-databases/)
- [Alembic Documentation](https://alembic.sqlalchemy.org/)
- [SQLAlchemy Connection Pooling](https://docs.sqlalchemy.org/en/20/core/pooling.html)

---

The database layer provides a robust foundation for data persistence with proper session management, transaction handling, and development/production workflow support.
