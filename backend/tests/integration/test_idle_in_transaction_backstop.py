"""The idle-in-transaction backstop, proven against a live Postgres (issue #440).

Configuration-only assertions live in ``tests/unit/test_idle_in_transaction_
backstop.py``; they prove the GUC is *set*. These prove it *works* — that
Postgres really terminates a transaction left idle, that it does NOT interrupt a
slow query, and that the one documented ``SET LOCAL`` exemption does not leak to
the next transaction on a pooled connection.

Each test carries its own control. "The connection broke" is also what a
restarting database looks like, and a version of the termination test that ran
only the with-GUC case passed for that reason during development.
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import OperationalError

from app.core.config import settings
from app.db.base import build_libpq_options

#: Well under any plausible scheduling delay in CI, but long enough that the
#: transaction is genuinely idle rather than racing its own BEGIN.
_TEST_TIMEOUT_MS = 500


def _outcome_of_idling(options: str | None) -> str:
    """Idle inside a transaction past the timeout; report ``survived`` or ``killed``.

    Args:
        options: libpq ``options`` string, or None to connect without one.

    Returns:
        ``"survived"`` if a statement still works afterwards, else ``"killed"``.
    """
    engine = create_engine(
        settings.DATABASE_URL,
        connect_args={"options": options} if options else {},
    )
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))  # open a real transaction
            time.sleep(_TEST_TIMEOUT_MS / 1000 * 4)
            try:
                conn.execute(text("SELECT 1"))
            except (OperationalError, DBAPIError):
                return "killed"
            return "survived"
    except (OperationalError, DBAPIError):
        # The rollback on block exit can also surface the dead socket.
        return "killed"
    finally:
        engine.dispose()


@pytest.mark.integration
def test_postgres_terminates_a_transaction_left_idle() -> None:
    """The control works against a real server, not just in ``connect_args``.

    Carries its own control: the SAME idle transaction, same duration, differing
    only by whether the GUC is set. Without the pair this asserts nothing —
    "the connection broke" is also what a restarting database looks like, and a
    version of this test that only ran the WITH case passed for that reason
    during development.

    The error text is deliberately not asserted. Postgres does send
    ``FATAL: terminating connection due to idle-in-transaction timeout``, but
    psycopg2 usually finds the socket already closed and reports the generic
    "server closed the connection unexpectedly" instead. Asserting the specific
    string made this test fail against a backstop that was working correctly.
    """
    with_guc = _outcome_of_idling(build_libpq_options(_TEST_TIMEOUT_MS))
    without_guc = _outcome_of_idling(None)

    assert without_guc == "survived", (
        "The control idled for the same duration with no timeout configured and "
        f"still got {without_guc!r} — something other than the backstop is "
        "closing connections, so this test proves nothing about the GUC."
    )
    assert with_guc == "killed", (
        "An idle transaction survived past the configured "
        f"idle_in_transaction_session_timeout={_TEST_TIMEOUT_MS}ms. The backstop "
        "is not in effect on this server."
    )


@pytest.mark.integration
def test_set_local_exempts_one_transaction_and_does_not_leak_to_the_next() -> None:
    """The watch-source ingest exemption is scoped to its own transaction.

    ``ingest_prepared_file`` issues ``SET LOCAL
    idle_in_transaction_session_timeout = 0`` before its MinIO upload — the one
    documented place where holding a transaction idle for minutes is expected
    (a 15 GB import over a slow link exceeds the 5-minute default, and killing
    that connection would abort a legitimate ingest).

    ``SET LOCAL`` is only correct if it reverts at commit. Connections are
    pooled, so a setting that persisted would silently disable the backstop for
    every later transaction that reused the connection — turning a narrow,
    documented exemption into a global hole. Both halves are asserted here:
    exempt inside the transaction, back to the configured value after it.
    """
    engine = create_engine(
        settings.DATABASE_URL,
        connect_args={"options": build_libpq_options(_TEST_TIMEOUT_MS)},
    )
    try:
        with engine.connect() as conn:
            with conn.begin():
                conn.execute(text("SET LOCAL idle_in_transaction_session_timeout = 0"))
                time.sleep(_TEST_TIMEOUT_MS / 1000 * 4)
                assert conn.execute(text("SELECT 1")).scalar() == 1, (
                    "SET LOCAL did not exempt this transaction — a large "
                    "watch-source import would be terminated mid-upload."
                )

            after = conn.execute(text("SHOW idle_in_transaction_session_timeout")).scalar_one()
            assert after == f"{_TEST_TIMEOUT_MS}ms", (
                f"After commit the timeout is {after!r}, not the connection's "
                f"configured {_TEST_TIMEOUT_MS}ms. SET LOCAL leaked, so the "
                "backstop is disabled for every later user of this pooled "
                "connection."
            )
    finally:
        engine.dispose()


@pytest.mark.integration
def test_a_slow_query_is_not_killed_by_the_backstop() -> None:
    """The control must not interrupt legitimate long work — the reason it is safe.

    ``idle_in_transaction_session_timeout`` acts only on a transaction running
    NO query. This is the control for the test above: same engine, same timeout,
    but the time is spent inside a statement instead of between statements, and
    the connection survives. Without this, a passing suite would be equally
    consistent with a GUC that kills any long transaction — which would be a
    production outage, not a backstop.
    """
    engine = create_engine(
        settings.DATABASE_URL,
        connect_args={"options": build_libpq_options(_TEST_TIMEOUT_MS)},
        poolclass=None,
    )
    try:
        with engine.connect() as conn:
            sleep_seconds = _TEST_TIMEOUT_MS / 1000 * 4
            conn.execute(text("SELECT pg_sleep(:s)"), {"s": sleep_seconds})
            assert conn.execute(text("SELECT 1")).scalar() == 1
    finally:
        engine.dispose()
