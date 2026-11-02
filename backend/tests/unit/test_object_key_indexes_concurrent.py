"""Runtime build of the v456 object-key indexes: concurrent, self-healing, bounded.

``CREATE UNIQUE INDEX CONCURRENTLY`` cannot run inside a transaction, so these tests use their own
autocommit connection, on a throwaway database that is dropped in a ``finally`` (see
``_exclusive_autocommit``): they never drop or invalidate an index of the shared database.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.pool import NullPool

from app.db import migrations
from app.db import object_key_indexes as oki
from app.db.base import engine

PLAYBACK = oki.PLAYBACK_PATH


@contextmanager
def _exclusive_autocommit() -> Iterator[Connection]:
    """An AUTOCOMMIT connection to a THROWAWAY database holding a minimal ``media_file``.

    These tests drop, invalidate and rebuild the key indexes. Doing that on the shared database
    contended with the live stack (``CREATE INDEX CONCURRENTLY`` waits for every older
    transaction in the database) and, when a build timed out, left the shared schema without
    the index every other suite asserts on. A database of its own has no such neighbours and
    needs no DDL advisory lock.
    """
    name = f"ot_oki_{uuid.uuid4().hex[:12]}_test"
    admin = create_engine(
        engine.url.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    scratch = None
    try:
        with admin.connect() as admin_conn:
            admin_conn.execute(text(f'CREATE DATABASE "{name}"'))
        scratch = create_engine(engine.url.set(database=name), poolclass=NullPool)
        with scratch.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(
                text(
                    "CREATE TABLE media_file (id serial PRIMARY KEY, storage_path text, "
                    "thumbnail_path text, playback_path text)"
                )
            )
            built = oki.ensure_indexes_concurrently(conn)
            assert set(built.values()) == {"built"}, built
            yield conn
    finally:
        if scratch is not None:
            scratch.dispose()
        with admin.connect() as admin_conn:
            admin_conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _mark_invalid(conn: Connection, name: str) -> None:
    conn.execute(
        text(
            "UPDATE pg_index SET indisvalid = false WHERE indexrelid = "
            "(SELECT oid FROM pg_class WHERE relname = :n)"
        ),
        {"n": name},
    )


def _drop_index(conn: Connection, name: str) -> None:
    conn.execute(text(f"DROP INDEX {name}"))


def test_the_destructive_tests_run_on_a_database_of_their_own():
    with engine.connect() as shared:
        shared_db = shared.execute(text("SELECT current_database()")).scalar_one()
    with _exclusive_autocommit() as conn:
        scratch_db = conn.execute(text("SELECT current_database()")).scalar_one()
        assert scratch_db != shared_db
        assert scratch_db.startswith("ot_oki_")
        _drop_index(conn, PLAYBACK.name)
        with engine.connect() as shared:
            # Dropping in the scratch database leaves the shared schema alone (when it has one).
            if oki.TABLE in inspect(shared).get_table_names():
                assert PLAYBACK.name in oki.existing_indexes(shared)
    with engine.connect() as admin:
        left = admin.execute(
            text("SELECT count(*) FROM pg_database WHERE datname = :n"), {"n": scratch_db}
        ).scalar_one()
    assert left == 0, "the scratch database was not dropped"


def test_an_invalid_index_is_dropped_and_rebuilt_concurrently():
    with _exclusive_autocommit() as conn:
        _mark_invalid(conn, PLAYBACK.name)
        assert PLAYBACK.name not in oki.existing_indexes(conn)
        assert oki.invalid_indexes(conn) == {PLAYBACK.name}

        outcome = oki.ensure_indexes_concurrently(conn)

        assert outcome[PLAYBACK.name] == "built"
        assert oki.invalid_indexes(conn) == set()
        assert PLAYBACK.name in oki.existing_indexes(conn)


def test_a_missing_index_is_built_and_valid_ones_are_left_alone():
    with _exclusive_autocommit() as conn:
        oid_before = conn.execute(
            text("SELECT oid FROM pg_class WHERE relname = :n"), {"n": oki.STORAGE_PATH.name}
        ).scalar_one()
        _drop_index(conn, PLAYBACK.name)
        statements: list[str] = []

        def record(_c, _cur, statement, *_rest):
            statements.append(statement)

        event.listen(conn, "before_cursor_execute", record)
        try:
            outcome = oki.ensure_indexes_concurrently(conn)
        finally:
            event.remove(conn, "before_cursor_execute", record)

        assert outcome[PLAYBACK.name] == "built"
        creates = [s for s in statements if s.lstrip().upper().startswith("CREATE")]
        assert len(creates) == 1 and "CONCURRENTLY" in creates[0].upper()
        assert outcome[oki.STORAGE_PATH.name] == "exists"
        assert (
            conn.execute(
                text("SELECT oid FROM pg_class WHERE relname = :n"), {"n": oki.STORAGE_PATH.name}
            ).scalar_one()
            == oid_before
        )


def test_a_build_blocked_by_an_open_transaction_gives_up_instead_of_waiting_forever():
    """CREATE INDEX CONCURRENTLY waits for every older transaction in the database; one idle
    session must cost a bounded wait and a ``failed`` outcome, never an unbounded startup."""
    import time

    with _exclusive_autocommit() as conn:
        _drop_index(conn, PLAYBACK.name)
        with conn.engine.connect().execution_options(isolation_level="REPEATABLE READ") as blocker:
            # An open transaction holding a snapshot is exactly what the build has to wait out.
            blocker.execute(text("SELECT count(*) FROM media_file"))
            started = time.monotonic()
            outcome = oki.ensure_indexes_concurrently(conn, lock_timeout_seconds=1)
            waited = time.monotonic() - started
            blocker.rollback()

        assert outcome[PLAYBACK.name] == "failed"
        assert waited < 30, f"the build waited {waited:.0f}s despite a 1s lock timeout"
        # The session setting must not leak onto the (pooled) connection.
        assert conn.execute(text("SHOW lock_timeout")).scalar_one() == "0"
        # The failed build may leave an INVALID index (its cleanup waits on the same blocker); with
        # the blocker gone the next call drops it and builds again.
        assert oki.ensure_indexes_concurrently(conn)[PLAYBACK.name] == "built"


def test_the_concurrent_build_is_refused_inside_a_transaction(db_session):
    with pytest.raises(ValueError, match="AUTOCOMMIT"):
        oki.ensure_indexes_concurrently(db_session.connection())


def test_the_plain_build_still_reports_an_invalid_index_as_not_present(db_session):
    conn = db_session.connection()
    nested = db_session.begin_nested()
    try:
        _mark_invalid(conn, PLAYBACK.name)
        assert PLAYBACK.name not in oki.existing_indexes(conn)
        assert oki.status(conn)["healthy"] is False
    finally:
        nested.rollback()


@pytest.fixture
def fresh_retry_state(monkeypatch):
    monkeypatch.setattr(migrations, "_object_key_retry_done", False)


def test_the_startup_retry_runs_once_per_process_and_builds_concurrently(
    fresh_retry_state, monkeypatch
):
    calls: list[str] = []

    def fake_ensure(conn: Connection) -> dict[str, str]:
        calls.append(str(conn.get_execution_options().get("isolation_level")))
        return {}

    monkeypatch.setattr(oki, "ensure_indexes_concurrently", fake_ensure)

    migrations._retry_object_key_indexes()
    migrations._retry_object_key_indexes()

    assert calls == ["AUTOCOMMIT"]
