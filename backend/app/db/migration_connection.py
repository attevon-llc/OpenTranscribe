"""How the migration runner and Alembic connect: owner URL, SSL mode, maintenance switch."""

from typing import Any

from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.engine import make_url

from app.core.config import Settings

#: Custom GUC the audit immutability trigger honours, for the table owner only. Migration
#: connections set it so a data migration may repair an audit row; runtime engines never do.
AUDIT_MAINTENANCE_GUC = "opentranscribe.audit_maintenance"


def migration_database_url() -> str:
    """Owner connection URL with ``POSTGRES_SSLMODE`` applied (a URL's own sslmode wins).

    Resolved from the process environment at CALL time, not from the import-time ``settings``
    singleton: an explicit ``MIGRATION_DATABASE_URL`` (or ``DATABASE_URL``) still wins, else the
    URL is derived from the ``POSTGRES_*`` variables as they are now. Otherwise a caller that
    points ``POSTGRES_*`` at another database before running Alembic would migrate whichever
    database this process happened to import with.
    """
    current = Settings()
    url = make_url(current.MIGRATION_DATABASE_URL)
    if (
        current.POSTGRES_SSLMODE
        and current.POSTGRES_SSLMODE != "disable"
        and "sslmode" not in url.query
    ):
        url = url.update_query_dict({"sslmode": current.POSTGRES_SSLMODE})
    return url.render_as_string(hide_password=False)


class SessionSet:
    """``PoolEvents.connect`` listener running one session-level ``SET`` statement."""

    def __init__(self, statement: str) -> None:
        self.statement = statement

    def __call__(self, dbapi_connection: Any, _connection_record: Any) -> None:
        previous = dbapi_connection.autocommit
        dbapi_connection.autocommit = True
        try:
            with dbapi_connection.cursor() as cursor:
                cursor.execute(self.statement)
        finally:
            dbapi_connection.autocommit = previous


set_audit_maintenance_on_connect = SessionSet(f"SET {AUDIT_MAINTENANCE_GUC} = 'on'")
"""``PoolEvents.connect`` listener: session-level ``SET`` of the maintenance switch."""


def install_idle_in_transaction_timeout(engine: Engine, timeout_ms: int) -> Engine:
    """Apply ``idle_in_transaction_session_timeout`` to every connection ``engine`` opens.

    Same pool ``connect`` ``SET`` pattern as the audit switch below, for the same reason: a
    libpq ``options=-c`` startup parameter is rejected by connection proxies (RDS Proxy,
    PgBouncer). ``timeout_ms`` is coerced to ``int`` so no config text reaches the SQL. 0 or
    less installs nothing (the server/role default applies).
    """
    if int(timeout_ms) > 0:
        event.listen(
            engine,
            "connect",
            SessionSet(f"SET idle_in_transaction_session_timeout = {int(timeout_ms)}"),
        )
    return engine


def install_statement_timeout(engine: Engine, timeout_ms: int) -> Engine:
    """Apply ``statement_timeout`` to every connection ``engine`` opens.

    The per-session bound PostgreSQL recommends over a server-wide one ("Setting
    statement_timeout in postgresql.conf is not recommended because it would affect all
    sessions", runtime-config-client.html#GUC-STATEMENT-TIMEOUT). Installed with the same
    pool ``connect`` ``SET`` as :func:`install_idle_in_transaction_timeout`, for the same
    reason (proxies reject libpq ``options``). A statement past it is cancelled with
    SQLSTATE 57014 ``query_canceled``, which the API answers 503
    (``app.core.db_overload``). ``timeout_ms`` is coerced to ``int``; 0 or less installs
    nothing, which is the coded default, so Celery workers, the migrate step and scripts
    keep the server default (off). A route that legitimately needs longer uses
    ``SET LOCAL statement_timeout`` inside its own transaction and says why.
    """
    if int(timeout_ms) > 0:
        event.listen(
            engine,
            "connect",
            SessionSet(f"SET statement_timeout = {int(timeout_ms)}"),
        )
    return engine


def install_audit_maintenance_switch(engine: Engine) -> Engine:
    """Turn the audit maintenance switch on for every DBAPI connection ``engine`` opens.

    A session-level ``SET`` in a pool ``connect`` event, not a libpq ``options`` startup
    parameter: connection proxies and poolers (e.g. AWS RDS Proxy, PgBouncer in transaction
    mode) reject or drop startup options, so a ``-c`` switch would either fail every migration
    connection or silently not apply. This is SQLAlchemy's documented on-connect pattern
    ("Setting a Default Schema for New Connections", PoolEvents.connect). Autocommit is enabled
    for the ``SET`` so it is not rolled back with the driver's implicit transaction (psycopg2
    opens one on first execute) and so it persists for the whole session.
    """

    event.listen(engine, "connect", set_audit_maintenance_on_connect)
    return engine


def create_migration_engine(url: str | None = None, **kwargs: Any) -> Engine:
    """Owner engine for migrations and role reconciliation, with the maintenance switch on."""
    return install_audit_maintenance_switch(
        create_engine(url or migration_database_url(), **kwargs)
    )
