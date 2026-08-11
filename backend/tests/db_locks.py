"""The advisory lock that isolates schema DDL from the other xdist workers.

Lives in its own module so ``conftest.py`` and the handful of tests that open their own
DB connection share **one** definition of the key. A second literal copy of the key would
silently stop protecting anything the moment either copy changed.

Background: ``ALTER TABLE`` / ``DROP TABLE`` take Postgres's ``ACCESS EXCLUSIVE`` lock, and
for a foreign key that extends to the *referenced* table. Under ``-n auto`` nearly every
other worker is writing ``user`` rows, so unisolated DDL deadlocks against unrelated tests
(issue #389). Ordinary tests hold this lock SHARED for their transaction; a DDL test holds
it EXCLUSIVE, which drains every other worker — so it is expensive and belongs only on
tests that really execute DDL (issue #431).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.engine import Connection

#: Postgres advisory-lock namespace reserved for test-suite DDL isolation (issue #389).
#: Uses the 2-key ``(classid, objid)`` form, which is a separate namespace from single-key
#: ``pg_advisory_lock(N)`` calls — ``app/db/migrations.py``'s startup guard uses ``42``.
DDL_ISOLATION_LOCK_KEY = (990389, 1)

#: How long to wait for the lock before failing. The lock only covers connections that opt
#: in, and ~20 app paths open their own ``SessionLocal()`` during a request, so a genuine
#: cross-connection cycle remains possible. Without a timeout it presents as a suite that
#: never finishes.
DDL_LOCK_TIMEOUT = "10s"


def acquire_ddl_lock_exclusive(connection: Connection) -> None:
    """Take the DDL isolation lock EXCLUSIVE on a SQLAlchemy connection.

    Blocks until every in-flight ordinary test finishes, and blocks new ones from starting,
    until this transaction ends. ``pg_advisory_xact_lock`` releases on commit *or* rollback,
    so a failing test cannot wedge the suite.
    """
    connection.execute(text(f"SET LOCAL lock_timeout = '{DDL_LOCK_TIMEOUT}'"))
    connection.execute(
        text("SELECT pg_advisory_xact_lock(:k1, :k2)"),
        {"k1": DDL_ISOLATION_LOCK_KEY[0], "k2": DDL_ISOLATION_LOCK_KEY[1]},
    )


def acquire_ddl_lock_shared(connection: Connection) -> None:
    """Take the DDL isolation lock SHARED — what every ordinary DB test does."""
    connection.execute(
        text("SELECT pg_advisory_xact_lock_shared(:k1, :k2)"),
        {"k1": DDL_ISOLATION_LOCK_KEY[0], "k2": DDL_ISOLATION_LOCK_KEY[1]},
    )


def acquire_ddl_lock_exclusive_raw(cursor) -> None:  # noqa: ANN001 - psycopg2 cursor
    """Same lock, for a test holding a raw DBAPI cursor instead of a SQLAlchemy connection.

    ``tests/unit/test_uuid7_migration_guard.py`` needs this: it exercises the v368 migration
    guard's raw ``DO $$`` block, which scans ``information_schema`` for ``public`` tables with
    a non-native ``uuid`` column and runs ``ALTER TABLE`` on each match. That loop is empty on
    a correctly migrated database, but "empty in practice" is a runtime accident, not
    isolation — a match would ALTER a real table with no coordination at all.
    """
    cursor.execute(f"SET LOCAL lock_timeout = '{DDL_LOCK_TIMEOUT}'")
    cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", DDL_ISOLATION_LOCK_KEY)
