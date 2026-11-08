from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.db_metrics import register_pool_collector
from app.db.migration_connection import install_idle_in_transaction_timeout
from app.db.migration_connection import install_statement_timeout
from app.db.request_deadline import install_request_deadline

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
    pool_timeout=settings.DB_POOL_TIMEOUT_SECONDS,
)

# Observability: attach per-statement timing + per-request query-count listeners
# (see app.core.db_metrics). Registered here, on the shared engine, so the same
# instrumentation applies to API requests, Celery tasks, and scripts.
from app.core.db_metrics import register_listeners  # noqa: E402

register_listeners(engine)
register_pool_collector(engine, settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW)

# Server-side backstop for sessions stuck idle in a transaction. A pool connect-event SET, not a
# libpq `options=-c` startup parameter (proxies reject those); startup also sets it on the role
# (app.db.roles.apply_idle_timeout_to_role) so non-pooled sessions get it too.
install_idle_in_transaction_timeout(engine, settings.DB_IDLE_IN_TRANSACTION_TIMEOUT_MS)

# Per-statement bound, set for the API process only: the coded default is 0 (off) and only
# the backend service's compose environment sets it (30 s), so Celery workers, the migrate
# step and scripts are unaffected. A cancelled statement is answered 503 (core/db_overload).
install_statement_timeout(engine, settings.DB_STATEMENT_TIMEOUT_MS)

# Per-request bound: a request past REQUEST_DEADLINE_SECONDS gets no more statements. A no-op
# wherever no request set a deadline (Celery workers, startup work, scripts).
install_request_deadline(engine)

# Create sessionmaker
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# Create base class for models (SQLAlchemy 2.0 typed declarative).
# NO naming_convention: adding one would rename existing constraints = schema change.
class Base(DeclarativeBase):
    pass


# Dependency for database session
def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
