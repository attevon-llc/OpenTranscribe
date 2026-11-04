"""Layer 2 of audit protection: the unprivileged application database role.

``reconcile_app_role`` runs as the table owner (the migration role) and makes the grants of
``POSTGRES_APP_USER`` match ``audit_protection``: DML everywhere except on the protected
tables, where it gets SELECT, INSERT and UPDATE of lifecycle columns only. It is idempotent and
runs on every startup, which also repairs a ``pg_dump --no-acl`` restore. Never ``DROP ROLE``:
turning the split off leaves a harmless unused role behind.

CLI for ``RUN_MIGRATIONS_ON_STARTUP=false`` deployments (run it after ``alembic upgrade``)::

    python -m app.db.roles reconcile
"""

import logging
import sys
from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.engine import make_url

from app.core.config import settings
from app.db.audit_protection import PRUNE_FUNCTION
from app.db.audit_protection import ProtectedTable
from app.db.audit_protection import all_protected_tables
from app.db.migration_connection import create_migration_engine

logger = logging.getLogger(__name__)

#: Tables the app role may read but never write.
_READ_ONLY_TABLES = ("alembic_version",)


def _quote(conn: Connection, name: str) -> str:
    return conn.dialect.identifier_preparer.quote(name)


def _existing(conn: Connection, tables: Iterable[ProtectedTable]) -> list[ProtectedTable]:
    return [
        t
        for t in tables
        if conn.execute(
            text("SELECT to_regclass(:n) IS NOT NULL"), {"n": f"public.{t.name}"}
        ).scalar()
    ]


def _scram_verifier(conn: Connection, role: str, password: str) -> str:
    """SCRAM-SHA-256 verifier, so the plaintext never reaches a server log."""
    from psycopg2.extensions import encrypt_password

    raw = conn.connection.driver_connection
    verifier: str = encrypt_password(password, role, raw, "scram-sha-256")
    return verifier


def reconcile_app_role(
    conn: Connection, role: str, password: str | None, *, provision: bool = True
) -> None:
    """Create/alter ``role`` and (re)apply its grants. Runs as the owner, caller commits.

    Args:
        conn: Connection of the owner role (the migration role).
        role: Application role name.
        password: Login password; ``None`` leaves it unchanged (or unset for a new role).
        provision: ``False`` skips CREATE/ALTER ROLE for roles created outside the app.
    """
    ident = _quote(conn, role)
    owner = _quote(conn, conn.execute(text("SELECT current_user")).scalar_one())
    db = _quote(conn, conn.execute(text("SELECT current_database()")).scalar_one())
    run = conn.exec_driver_sql

    if provision:
        exists = conn.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
        ).scalar()
        if not exists:
            run(f"CREATE ROLE {ident} LOGIN")
        run(
            f"ALTER ROLE {ident} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOBYPASSRLS NOREPLICATION"
        )
        if password:
            conn.execute(
                text(f"ALTER ROLE {ident} PASSWORD :verifier"),
                {"verifier": _scram_verifier(conn, role, password)},
            )

    run(f"GRANT CONNECT ON DATABASE {db} TO {ident}")
    run(f"GRANT USAGE ON SCHEMA public TO {ident}")
    run(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {ident}")
    run(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {ident}")
    run(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {ident}"
    )
    run(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA public "
        f"GRANT USAGE, SELECT ON SEQUENCES TO {ident}"
    )
    for name in _read_only_present(conn):
        run(f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON {_quote(conn, name)} FROM {ident}")

    for table in _existing(conn, all_protected_tables()):
        tbl = _quote(conn, table.name)
        run(f"REVOKE UPDATE, DELETE, TRUNCATE, TRIGGER, REFERENCES ON {tbl} FROM {ident}")
        if table.mutable_columns:
            cols = ", ".join(_quote(conn, c) for c in table.mutable_columns)
            run(f"GRANT UPDATE ({cols}) ON {tbl} TO {ident}")
        if table.app_delete:
            run(f"GRANT DELETE ON {tbl} TO {ident}")

    prune = conn.execute(
        text("SELECT to_regprocedure(:p) IS NOT NULL"),
        {"p": f"{PRUNE_FUNCTION}(regclass,integer)"},
    ).scalar()
    if prune:
        run(f"GRANT EXECUTE ON FUNCTION {PRUNE_FUNCTION}(regclass, integer) TO {ident}")


def _read_only_present(conn: Connection) -> list[str]:
    return [
        n
        for n in _READ_ONLY_TABLES
        if conn.execute(text("SELECT to_regclass(:n) IS NOT NULL"), {"n": f"public.{n}"}).scalar()
    ]


def reconcile_from_settings() -> None:
    """Reconcile the configured app role using the owner (migration) connection."""
    if not settings.db_role_split_requested:
        return
    role = settings.POSTGRES_APP_USER or make_url(settings.DATABASE_URL).username
    if not role:
        raise RuntimeError("database role split requested but no app role name is resolvable")
    provision = settings.POSTGRES_APP_ROLE_PROVISION and bool(settings.POSTGRES_APP_USER)
    engine = create_migration_engine()
    try:
        with engine.begin() as conn:
            reconcile_app_role(
                conn, role, settings.POSTGRES_APP_PASSWORD or None, provision=provision
            )
        logger.info("Application database role grants reconciled")
    finally:
        engine.dispose()


def main(argv: list[str]) -> int:
    """CLI entry: ``python -m app.db.roles reconcile``."""
    if argv != ["reconcile"]:
        sys.stderr.write("usage: python -m app.db.roles reconcile\n")
        return 2
    if not settings.db_role_split_requested:
        sys.stderr.write("database role split is not requested; nothing to do\n")
        return 0
    reconcile_from_settings()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
