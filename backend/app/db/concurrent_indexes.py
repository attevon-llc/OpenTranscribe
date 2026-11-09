"""Build performance and integrity indexes without ever blocking or failing startup.

``CREATE INDEX CONCURRENTLY`` cannot run in a transaction and waits for every transaction that
could still see the table (PostgreSQL ``CREATE INDEX`` docs, "Building Indexes Concurrently"), so
one idle-in-transaction session or a long celery transaction would hold an upgrade indefinitely.
This module is the one place that handles that, shared by the v456 object-key indexes and the
v458 speaker index:

* the caller passes an AUTOCOMMIT connection;
* every statement runs under ``SET lock_timeout`` (reset afterwards, the connection is pooled);
* an INVALID index left by an earlier failed build is dropped first, because it enforces and
  serves nothing and ``IF NOT EXISTS`` would accept it;
* nothing raises: an index that could not be built is reported ``failed`` and the runner retries
  it on the next start.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

logger = logging.getLogger(__name__)

#: Per-statement wait for other sessions' open transactions.
BUILD_LOCK_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class IndexSpec:
    """A (possibly partial, possibly unique) index. Names are code constants, never input.

    ``method`` is the access method (``gin`` for JSONB containment); a column entry may carry
    its operator class (``"citations jsonb_path_ops"``). Default is btree.
    """

    name: str
    table: str
    columns: tuple[str, ...]
    where: str | None = None
    unique: bool = False
    method: str | None = None

    def create_sql(self) -> str:
        unique = "UNIQUE " if self.unique else ""
        where = f" WHERE {self.where}" if self.where else ""
        using = f" USING {self.method}" if self.method else ""
        return (
            f"CREATE {unique}INDEX CONCURRENTLY {self.name} "
            f"ON {self.table}{using} ({', '.join(self.columns)}){where}"
        )


_STATE_SQL = text(
    "SELECT c.relname FROM pg_index i "
    "JOIN pg_class c ON c.oid = i.indexrelid "
    "JOIN pg_class t ON t.oid = i.indrelid "
    "JOIN pg_namespace n ON n.oid = t.relnamespace "
    "WHERE n.nspname = current_schema() AND t.relname = :t AND c.relname = ANY(:n) "
    "AND i.indisvalid = :valid"
)


def index_names(conn: Connection, table: str, names: Sequence[str], *, valid: bool) -> set[str]:
    """Names among ``names`` on ``table`` that are valid (or, with ``valid=False``, INVALID)."""
    rows = conn.execute(_STATE_SQL, {"t": table, "n": list(names), "valid": valid})
    return {str(r[0]) for r in rows}


def ensure_concurrently(
    conn: Connection,
    specs: Sequence[IndexSpec],
    *,
    lock_timeout_seconds: int | None = None,
    skip: Callable[[Connection, IndexSpec], bool] | None = None,
) -> dict[str, str]:
    """Build every missing index in ``specs``; never raise on a database problem.

    Args:
        conn: An AUTOCOMMIT connection.
        specs: The indexes that should exist.
        lock_timeout_seconds: Per-statement wait for other transactions (default: the module
            constant, read at call time).
        skip: Optional veto run before each build (``True`` reports ``skipped``).

    Returns:
        ``{index_name: "exists" | "built" | "skipped" | "failed"}``.

    Raises:
        ValueError: ``conn`` is not in AUTOCOMMIT mode.
    """
    if conn.get_execution_options().get("isolation_level") != "AUTOCOMMIT":
        raise ValueError("ensure_concurrently needs an AUTOCOMMIT connection")
    if lock_timeout_seconds is None:
        lock_timeout_seconds = BUILD_LOCK_TIMEOUT_SECONDS
    outcome: dict[str, str] = {}
    pending: list[IndexSpec] = []
    broken: set[str] = set()
    try:
        for table in {s.table for s in specs}:
            names = [s.name for s in specs if s.table == table]
            present = index_names(conn, table, names, valid=True)
            broken |= index_names(conn, table, names, valid=False)
            for s in specs:
                if s.table == table:
                    if s.name in present:
                        outcome[s.name] = "exists"
                    else:
                        pending.append(s)
        if not pending:
            return outcome
        conn.execute(text(f"SET lock_timeout = '{int(lock_timeout_seconds)}s'"))
    except DBAPIError:
        logger.error("Could not inspect indexes; startup continues", exc_info=True)
        return {**{s.name: "failed" for s in specs}, **outcome}
    try:
        for s in pending:
            outcome[s.name] = _build(conn, s, s.name in broken, skip)
    finally:
        try:
            conn.execute(text("RESET lock_timeout"))
        except DBAPIError:
            logger.error("Could not reset lock_timeout", exc_info=True)
    return outcome


def _build(
    conn: Connection,
    spec: IndexSpec,
    broken: bool,
    skip: Callable[[Connection, IndexSpec], bool] | None,
) -> str:
    try:
        if broken:
            conn.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {spec.name}"))
        if skip is not None and skip(conn, spec):
            return "skipped"
        conn.execute(text(spec.create_sql()))
        return "built"
    except DBAPIError:
        logger.error("Could not build %s; startup continues", spec.name, exc_info=True)
        _drop_invalid_leftover(conn, spec)
        return "failed"


def _drop_invalid_leftover(conn: Connection, spec: IndexSpec) -> None:
    try:
        if spec.name in index_names(conn, spec.table, [spec.name], valid=False):
            conn.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {spec.name}"))
    except DBAPIError:
        logger.error("Could not drop the invalid index %s", spec.name, exc_info=True)
