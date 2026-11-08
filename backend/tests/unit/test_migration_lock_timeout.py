"""The migration advisory-lock wait is bounded, against a real Postgres session holding it.

``run_migrations()`` used to call ``pg_advisory_lock(42)``, which waits forever: one wedged
holder hung every API replica (and every migrate step) behind it. These tests hold the lock
from a second session and check the runner gives up at ``MIGRATION_LOCK_TIMEOUT_S``, says who
holds it, never reaches Alembic, and still acquires normally once the holder lets go.

Advisory locks are per database and take no table lock, so this uses the suite database
without DDL, each test under a private key (the runner reads ``MIGRATION_LOCK_KEY`` at call
time). Run with ``./scripts/run-backend-tests.sh --throwaway-db`` or the dev stack.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy import text

from app.db import migrations
from app.db.migration_connection import migration_database_url


@pytest.fixture(autouse=True)
def private_lock_key(monkeypatch):
    """A key of this test's own: xdist workers share the database, and advisory locks are
    per database, so every test here holding the real key 42 would block the others."""
    monkeypatch.setattr(migrations, "MIGRATION_LOCK_KEY", 4_200_000 + uuid.uuid4().int % 99_999)


@pytest.fixture
def holder(private_lock_key):
    """A second session that holds the migration lock until the test ends."""
    engine = create_engine(migration_database_url())
    conn = engine.connect()
    got = conn.execute(
        text("SELECT pg_try_advisory_lock(:k)"), {"k": migrations.MIGRATION_LOCK_KEY}
    ).scalar()
    conn.commit()
    if not got:
        conn.close()
        engine.dispose()
        pytest.fail("the migration lock is already held on the test database; cannot set up")
    pid = conn.execute(text("SELECT pg_backend_pid()")).scalar()
    conn.commit()
    try:
        yield conn, pid
    finally:
        conn.close()  # session end releases the lock
        engine.dispose()


@pytest.fixture
def fast_poll(monkeypatch):
    monkeypatch.setattr(migrations, "_LOCK_POLL_S", 0.1)


def _session():
    engine = create_engine(migration_database_url())
    return engine, engine.connect()


def test_waiting_for_a_held_lock_gives_up_at_the_bound_and_names_the_holder(
    holder, fast_poll, caplog
):
    _, holder_pid = holder
    engine, conn = _session()
    try:
        caplog.set_level(logging.INFO, logger=migrations.__name__)
        started = time.monotonic()
        with pytest.raises(migrations.MigrationLockTimeoutError) as err:
            migrations._acquire_migration_lock(conn, timeout_s=1.0)
        elapsed = time.monotonic() - started
    finally:
        conn.close()
        engine.dispose()

    assert 1.0 <= elapsed < 10, f"gave up after {elapsed:.1f}s, bound was 1s"
    assert f"pid={holder_pid}" in str(err.value)
    assert any(
        r.levelno == logging.CRITICAL and f"pid={holder_pid}" in r.getMessage()
        for r in caplog.records
    ), "the give-up must be logged with the holder"


def test_the_bound_comes_from_the_environment(holder, fast_poll, monkeypatch):
    monkeypatch.setenv("MIGRATION_LOCK_TIMEOUT_S", "0.5")
    engine, conn = _session()
    try:
        started = time.monotonic()
        with pytest.raises(migrations.MigrationLockTimeoutError):
            migrations._acquire_migration_lock(conn)
        assert time.monotonic() - started < 10
    finally:
        conn.close()
        engine.dispose()


def test_run_migrations_fails_on_the_timeout_without_touching_alembic(
    holder, fast_poll, monkeypatch
):
    monkeypatch.setenv("MIGRATION_LOCK_TIMEOUT_S", "1")
    monkeypatch.setattr(migrations, "_DB_STARTUP_WAIT_S", 5.0)
    touched: list[str] = []
    monkeypatch.setattr(migrations, "get_alembic_config", lambda: touched.append("alembic"))
    monkeypatch.setattr(migrations, "_detect_schema_version", lambda *a: touched.append("detect"))

    with pytest.raises(migrations.MigrationLockTimeoutError):
        migrations.run_migrations()
    assert touched == []


def test_the_lock_is_taken_once_the_holder_releases(holder, fast_poll):
    holder_conn, _ = holder
    engine, conn = _session()
    try:
        release = threading.Timer(
            0.5,
            lambda: (
                holder_conn.execute(
                    text("SELECT pg_advisory_unlock(:k)"), {"k": migrations.MIGRATION_LOCK_KEY}
                ),
                holder_conn.commit(),
            ),
        )
        release.start()
        migrations._acquire_migration_lock(conn, timeout_s=10.0)
        release.join()
        mine = conn.execute(
            text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
                "AND objid = :k AND pid = pg_backend_pid()"
            ),
            {"k": migrations.MIGRATION_LOCK_KEY},
        ).scalar()
        conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": migrations.MIGRATION_LOCK_KEY})
        conn.commit()
    finally:
        conn.close()
        engine.dispose()
    assert mine == 1, "the waiting session must end up holding the lock"


@pytest.mark.parametrize("raw,expected", [("", 900.0), ("12", 12.0), ("0", 0.0), ("x", 900.0)])
def test_timeout_parsing(monkeypatch, raw, expected):
    monkeypatch.setitem(os.environ, "MIGRATION_LOCK_TIMEOUT_S", raw)
    assert migrations._migration_lock_timeout_s() == expected
