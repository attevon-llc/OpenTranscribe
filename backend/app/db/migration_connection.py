"""How the migration runner and Alembic connect: owner URL, SSL mode, maintenance switch."""

from typing import Any

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


def migration_connect_args() -> dict[str, Any]:
    """``connect_args`` for a migration engine: sets the audit maintenance switch."""
    return {"options": f"-c {AUDIT_MAINTENANCE_GUC}=on"}
