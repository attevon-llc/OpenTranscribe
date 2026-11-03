"""Unit tests for the DB-query metric listeners and per-request counter.

These exercise the ContextVar mutable-dict propagation pattern that lets a
``BaseHTTPMiddleware`` parent see statement counts incremented inside the child
task / threadpool, plus the None-context no-op for worker/script contexts.
"""

import asyncio

from sqlalchemy import create_engine
from starlette.concurrency import run_in_threadpool

from app.core import db_metrics

# The listeners never touch the connection or cursor, but they are typed with the
# real SQLAlchemy objects, so hand them real ones (an in-memory engine, never queried).
_CONN = create_engine("sqlite://").connect()
_CURSOR = _CONN.connection.cursor()


def _make_context():
    """A stand-in object the listeners can ``setattr`` a start time onto."""

    class _Ctx:
        pass

    return _Ctx()


def _run_listeners(statement, parameters, ctx):
    db_metrics._before_cursor_execute(_CONN, _CURSOR, statement, parameters, ctx, False)
    db_metrics._after_cursor_execute(_CONN, _CURSOR, statement, parameters, ctx, False)


def test_counter_increments_per_statement():
    token = db_metrics.start_request_counter()
    try:
        assert db_metrics.get_request_query_count() == 0
        for _ in range(3):
            ctx = _make_context()
            _run_listeners("SELECT 1", None, ctx)
        assert db_metrics.get_request_query_count() == 3
    finally:
        db_metrics.reset(token)


def test_none_context_is_noop():
    # Outside a request (Celery worker / script) the ContextVar is None.
    assert db_metrics.get_request_query_count() == 0
    ctx = _make_context()
    # Must not raise and must not crash even though there's no counter dict.
    _run_listeners("SELECT 1", None, ctx)
    assert db_metrics.get_request_query_count() == 0


def test_mutable_dict_propagates_through_threadpool():
    """A statement counted inside run_in_threadpool is visible to the parent.

    This is the exact pattern the middleware relies on: the parent sets the
    ContextVar once, the threadpool copies the context (same dict object), and
    increments there are visible after the await.
    """

    async def scenario() -> int:
        token = db_metrics.start_request_counter()
        try:

            def do_query():
                ctx = _make_context()
                _run_listeners("SELECT 1", None, ctx)

            await run_in_threadpool(do_query)
            return db_metrics.get_request_query_count()
        finally:
            db_metrics.reset(token)

    assert asyncio.run(scenario()) == 1


def test_duration_histogram_observed():
    """Each statement feeds db_query_duration_seconds regardless of context."""
    before = db_metrics.db_query_duration_seconds._sum.get()
    ctx = _make_context()
    _run_listeners("SELECT 1", None, ctx)
    after = db_metrics.db_query_duration_seconds._sum.get()
    assert after >= before


def test_slow_query_logs_warning_without_params(caplog):
    """Slow statements warn with truncated SQL and NEVER the bound parameters.

    Bound parameters are where secrets/PII actually live in parameterized SQL
    (``WHERE token = %(token)s`` with the value supplied separately). The
    listener logs only the statement text, never the ``parameters`` tuple.
    """
    import logging

    from app.core.config import settings

    original = settings.SLOW_QUERY_MS
    settings.SLOW_QUERY_MS = 0  # force every statement to count as "slow"
    try:
        with caplog.at_level(logging.WARNING, logger="app.core.db_metrics"):
            ctx = _make_context()
            statement = "SELECT * FROM users WHERE token = %(token)s"
            params = {"token": "supersecret-bound-value"}
            _run_listeners(statement, params, ctx)
        assert any("Slow query" in rec.message for rec in caplog.records)
        # The bound parameter value must never appear in the log output.
        assert "supersecret-bound-value" not in caplog.text
    finally:
        settings.SLOW_QUERY_MS = original


def test_slow_query_truncates_statement_to_120_chars(caplog):
    """Only the first 120 chars of the statement are logged."""
    import logging

    from app.core.config import settings

    original = settings.SLOW_QUERY_MS
    settings.SLOW_QUERY_MS = 0
    try:
        with caplog.at_level(logging.WARNING, logger="app.core.db_metrics"):
            ctx = _make_context()
            tail_marker = "UNIQUE_TAIL_MARKER_PAST_120_CHARS"
            statement = "SELECT " + ("col, " * 40) + tail_marker
            assert len(statement) > 120
            _run_listeners(statement, None, ctx)
        assert tail_marker not in caplog.text
    finally:
        settings.SLOW_QUERY_MS = original
