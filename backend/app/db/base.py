from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import sessionmaker

from app.core.config import settings

# Build connect_args with PostgreSQL SSL mode when configured
connect_args: dict = {}
if settings.POSTGRES_SSLMODE and settings.POSTGRES_SSLMODE != "disable":
    connect_args["sslmode"] = settings.POSTGRES_SSLMODE


def build_libpq_options(idle_in_transaction_timeout_ms: int) -> str | None:
    """Build the libpq ``options`` string for a connection, or None if empty.

    Factored out so the value is testable without opening a connection, and so
    a second server-side GUC can be added here rather than by string-appending
    at the call site.

    Args:
        idle_in_transaction_timeout_ms: Milliseconds; 0 or less disables.

    Returns:
        A libpq ``options`` string, or None when nothing needs setting.
    """
    gucs: list[str] = []
    if idle_in_transaction_timeout_ms > 0:
        gucs.append(f"-c idle_in_transaction_session_timeout={idle_in_transaction_timeout_ms}")
    return " ".join(gucs) if gucs else None


_libpq_options = build_libpq_options(settings.DB_IDLE_IN_TRANSACTION_TIMEOUT_MS)
if _libpq_options is not None:
    connect_args["options"] = _libpq_options

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
