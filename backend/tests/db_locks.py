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

import re
from typing import TYPE_CHECKING

from sqlalchemy import event
from sqlalchemy import text

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.engine import Connection

#: Postgres advisory-lock namespace reserved for test-suite DDL isolation (issue #389).
#: Uses the 2-key ``(classid, objid)`` form, which is a separate namespace from single-key
#: ``pg_advisory_lock(N)`` calls — ``app/db/migrations.py``'s startup guard uses ``42``.
DDL_ISOLATION_LOCK_KEY = (990389, 1)

#: How long to wait for the lock before failing. The lock only covers connections that opt in,
#: and ~20 app paths open their own ``SessionLocal()`` during a request, so a genuine
#: cross-connection cycle remains possible. Without a timeout it presents as a suite that
#: never finishes.
#:
#: Sized against the *drain*, not the DDL. An EXCLUSIVE acquisition waits for every in-flight
#: SHARED holder to finish, and each ordinary test holds the shared lock for its whole
#: lifetime — so the wait is bounded by the slowest test running concurrently, not by the DDL
#: statement. Measured: 10 s failed 7 of the 14 ``ddl_exclusive`` tests with
#: ``LockNotAvailable`` on a suite whose p99 test is ~20 s and whose slowest setup was 20.9 s.
#: 120 s clears that with headroom while staying well inside the 300 s pytest timeout, so a
#: real deadlock still surfaces as a clear Postgres error rather than a hung run.
DDL_LOCK_TIMEOUT = "120s"


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
    serialize_auth_config_writes(connection)


def acquire_ddl_lock_shared(connection: Connection) -> None:
    """Take the DDL isolation lock SHARED — what every ordinary DB test does.

    Also arms ``serialize_auth_config_writes``: this is the one call every DB-backed test
    makes on its connection before anything else, so it is the place that covers them all.
    """
    serialize_auth_config_writes(connection)
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


#: Advisory-lock key serialising **writers of the ``auth_config`` table** across xdist workers.
#: Separate from ``DDL_ISOLATION_LOCK_KEY`` (that one is shared by every test; this one is
#: exclusive between the few that write ``auth_config``).
AUTH_CONFIG_WRITE_LOCK_KEY = (990389, 2)

_AUTH_CONFIG_WRITE = re.compile(
    r"^\s*(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+\"?auth_config\"?(?![\w])", re.I
)


def serialize_auth_config_writes(connection: Connection) -> None:
    """Make this test's first ``auth_config`` write take a cross-worker advisory lock.

    ``db_session`` isolates a test by never committing its outer transaction, so the unique
    index entries it inserts into ``auth_config`` stay as uncommitted row locks until the test
    ends. ``POST /migrate`` inserts every missing key in one request; another worker's test
    inserting an overlapping set in a different order waits on the first, which waits on it
    back — ``DeadlockDetected`` on ``auth_config_config_key_key``, surfacing as a 500 from the
    handler's broad ``except``. Order-dependent and rare, which is why it passed alone.

    Taking one transaction-scoped advisory lock *before* the first write (and holding it to
    rollback, exactly as long as the row locks) removes the cycle: a second writer waits at
    the lock holding no ``auth_config`` row lock. It hooks the SQL rather than a module list,
    so a new test that writes the table is covered without anyone remembering a marker.
    """
    held = False

    @event.listens_for(connection, "before_cursor_execute")
    def _lock_before_first_write(conn, cursor, statement, parameters, context, executemany):
        nonlocal held
        if held or not _AUTH_CONFIG_WRITE.match(statement):
            return
        held = True
        conn.exec_driver_sql("SELECT pg_advisory_xact_lock(%s, %s)", AUTH_CONFIG_WRITE_LOCK_KEY)
