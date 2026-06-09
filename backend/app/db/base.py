from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import sessionmaker

from app.core.config import settings

# Build connect_args with PostgreSQL SSL mode when configured
connect_args: dict = {}
if settings.POSTGRES_SSLMODE and settings.POSTGRES_SSLMODE != "disable":
    connect_args["sslmode"] = settings.POSTGRES_SSLMODE

# Create SQLAlchemy engine with connection pool settings
# Backend (FastAPI) handles concurrent API requests — needs larger pool.
# Celery workers each fork their own process with a separate engine, so
# pool_size here mainly affects the backend web server.
engine = create_engine(
    settings.DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,  # Verify connections before using them
    pool_recycle=3600,  # Recycle connections after 1 hour
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
)

# Observability: attach per-statement timing + per-request query-count listeners
# (see app.core.db_metrics). Registered here, on the shared engine, so the same
# instrumentation applies to API requests, Celery tasks, and scripts.
from app.core.db_metrics import register_listeners  # noqa: E402

register_listeners(engine)

# Create sessionmaker
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# Create base class for models (SQLAlchemy 2.0 typed declarative).
# NO naming_convention: adding one would rename existing constraints = schema change.
class Base(DeclarativeBase):
    pass


# Dependency for database session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
