"""A one-connection engine reserved for ``/health/ready``.

The readiness probe must not queue behind user traffic for a pooled connection: under load that
turns a busy replica into a "not ready" one. This engine is separate from ``app.db.base.engine``,
holds at most one connection, and bounds both connect and statement time.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.core.config import settings
from app.db.migration_connection import SessionSet

PROBE_APPLICATION_NAME = "opentranscribe-api-probe"
PROBE_CONNECT_TIMEOUT_SECONDS = 2
PROBE_STATEMENT_TIMEOUT_MS = 2000

_lock = threading.Lock()
_engine: Engine | None = None


def get_probe_engine() -> Engine:
    """The process-wide probe engine, created on first use."""
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                connect_args: dict = {}
                if settings.POSTGRES_SSLMODE and settings.POSTGRES_SSLMODE != "disable":
                    connect_args["sslmode"] = settings.POSTGRES_SSLMODE
                connect_args["connect_timeout"] = PROBE_CONNECT_TIMEOUT_SECONDS
                connect_args["application_name"] = PROBE_APPLICATION_NAME
                engine = create_engine(
                    settings.DATABASE_URL,
                    connect_args=connect_args,
                    pool_size=1,
                    max_overflow=0,
                    pool_timeout=1,
                    pool_pre_ping=True,
                )
                event.listen(
                    engine,
                    "connect",
                    SessionSet(f"SET statement_timeout = {PROBE_STATEMENT_TIMEOUT_MS}"),
                )
                _engine = engine
    return _engine


# "ok at head" is fixed for the process once seen; keyed by the head it was proven against.
_schema_ok_at: str | None = None


def reset_schema_cache() -> None:
    """Forget the cached schema result (tests only)."""
    global _schema_ok_at
    _schema_ok_at = None


def _check_schema(conn: Any, checks: dict[str, str]) -> str:
    """Compare the DB revision to the shipped head; "ok" is remembered per head."""
    global _schema_ok_at
    from alembic.migration import MigrationContext

    from app.db.migrations import get_alembic_head

    head = get_alembic_head()
    if _schema_ok_at is not None and _schema_ok_at == head:
        current: str | None = head
    else:
        current = MigrationContext.configure(conn).get_current_revision()
    checks["schema_revision"] = current or "none"
    checks["schema_head"] = head or "none"
    if current == head:
        _schema_ok_at = head
        return "ok"
    return f"stale: {current} != head {head}"


def check_postgres_and_schema(
    checks: dict[str, str], run_probe: Callable[[dict[str, str], str, Callable[[], str]], None]
) -> str:
    """``SELECT 1`` then the schema check on the one probe connection; raises if it cannot connect.

    The schema result is recorded through ``run_probe`` so a failure there does not mask the
    fact that Postgres itself answered.
    """
    with get_probe_engine().connect() as conn:
        conn.execute(text("SELECT 1"))
        run_probe(checks, "schema", lambda: _check_schema(conn, checks))
    return "ok"
