"""Measure what the CURRENT database role can really do to the protected tables.

Everything is read from the server (``has_table_privilege`` and friends), never from the
configuration, so a status page cannot claim protection that a superuser connection defeats.
"""

import logging
from dataclasses import dataclass
from dataclasses import field

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.engine import Engine

from app.core.config import settings
from app.db.audit_protection import ProtectedTable
from app.db.audit_protection import all_protected_tables
from app.db.audit_protection import guard_trigger_name

logger = logging.getLogger(__name__)


@dataclass
class TableState:
    """Measured privileges of the current role on one protected table."""

    name: str
    insert: bool
    update_columns: list[str]
    table_update: bool
    delete: bool
    truncate: bool
    guard_trigger_present: bool
    ok: bool = False


@dataclass
class RoleState:
    """Measured state of the current role."""

    split_requested: bool
    is_superuser: bool
    bypasses_rls: bool
    owns_tables: bool
    tables: list[TableState] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def enforced(self) -> bool:
        """True only when the split was requested and the role really cannot rewrite audit rows."""
        return self.split_requested and not self.problems


def _scalar(conn: Connection, sql: str, **params: str) -> object:
    return conn.execute(text(sql), params).scalar()


def _table_state(conn: Connection, table: ProtectedTable) -> TableState:
    rel = f"public.{table.name}"
    cols = [
        r[0]
        for r in conn.execute(
            text(
                "SELECT a.attname FROM pg_attribute a WHERE a.attrelid = to_regclass(:rel) "
                "AND a.attnum > 0 AND NOT a.attisdropped "
                "AND has_column_privilege(current_user, a.attrelid, a.attnum, 'UPDATE') "
                "ORDER BY a.attnum"
            ),
            {"rel": rel},
        )
    ]
    trigger = table.own_guard or guard_trigger_name(table.name)
    state = TableState(
        name=table.name,
        insert=bool(
            _scalar(
                conn, "SELECT has_table_privilege(current_user, to_regclass(:r), 'INSERT')", r=rel
            )
        ),
        update_columns=cols,
        table_update=bool(
            _scalar(
                conn, "SELECT has_table_privilege(current_user, to_regclass(:r), 'UPDATE')", r=rel
            )
        ),
        delete=bool(
            _scalar(
                conn, "SELECT has_table_privilege(current_user, to_regclass(:r), 'DELETE')", r=rel
            )
        ),
        truncate=bool(
            _scalar(
                conn, "SELECT has_table_privilege(current_user, to_regclass(:r), 'TRUNCATE')", r=rel
            )
        ),
        guard_trigger_present=bool(
            _scalar(
                conn,
                "SELECT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid = to_regclass(:r) "
                "AND tgname = :t AND tgenabled = 'O')",
                r=rel,
                t=trigger,
            )
        ),
    )
    state.ok = (
        not state.table_update
        and (state.delete == table.app_delete)
        and not state.truncate
        and set(state.update_columns) <= set(table.mutable_columns)
        and state.guard_trigger_present
    )
    return state


def measure_role(conn: Connection) -> RoleState:
    """Measure the current role of ``conn`` against the registry."""
    row = conn.execute(
        text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
    ).one()
    owns = bool(
        conn.execute(
            text(
                "SELECT EXISTS(SELECT 1 FROM pg_tables WHERE schemaname = 'public' "
                "AND pg_has_role(current_user, tableowner, 'MEMBER'))"
            )
        ).scalar()
    )
    state = RoleState(
        split_requested=settings.db_role_split_requested,
        is_superuser=bool(row[0]),
        bypasses_rls=bool(row[1]),
        owns_tables=owns,
    )
    if state.is_superuser:
        state.problems.append("runtime role is a superuser")
    if state.bypasses_rls:
        state.problems.append("runtime role bypasses row-level security")
    if state.owns_tables:
        state.problems.append("runtime role owns tables in the public schema")
    for table in all_protected_tables():
        exists = conn.execute(
            text("SELECT to_regclass(:r) IS NOT NULL"), {"r": f"public.{table.name}"}
        ).scalar()
        if not exists:
            continue
        ts = _table_state(conn, table)
        state.tables.append(ts)
        if not ts.ok:
            state.problems.append(f"{table.name}: privileges or guard trigger do not match")
    return state


def enforce_runtime_role(engine: Engine) -> None:
    """Refuse to start when the role split was requested but is not effective.

    No-op when the split is not requested. Raises ``RuntimeError`` listing what is wrong.
    """
    if not settings.db_role_split_requested:
        return
    with engine.connect() as conn:
        state = measure_role(conn)
    if not state.enforced:
        raise RuntimeError(
            "database role split was requested (POSTGRES_APP_USER / MIGRATION_DATABASE_URL) but "
            "is not effective: " + "; ".join(state.problems)
        )
    logger.info("Audit record protection is enforced (runtime role verified)")
