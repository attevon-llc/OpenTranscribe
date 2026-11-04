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


def set_audit_maintenance_on_connect(dbapi_connection: Any, _connection_record: Any) -> None:
    """``PoolEvents.connect`` listener: session-level ``SET`` of the maintenance switch."""
    previous = dbapi_connection.autocommit
    dbapi_connection.autocommit = True
    try:
        with dbapi_connection.cursor() as cursor:
            cursor.execute(f"SET {AUDIT_MAINTENANCE_GUC} = 'on'")
    finally:
        dbapi_connection.autocommit = previous


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
