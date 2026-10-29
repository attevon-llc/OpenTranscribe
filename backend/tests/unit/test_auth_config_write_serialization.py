"""Two workers writing ``auth_config`` keys in opposite order must not deadlock.

Regression for the order-dependent ``POST /migrate`` 500 in
``test_admin_auth_config_routes.py``: under ``-n auto`` the handler's bulk insert collided
with another worker's test inserting an overlapping key set in a different order, and
Postgres aborted one with ``DeadlockDetected`` on ``auth_config_config_key_key``.
"""

from __future__ import annotations

import threading
import time
import uuid

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from tests.conftest import engine
from tests.db_locks import serialize_auth_config_writes

_INSERT = text("INSERT INTO auth_config (uuid, config_key, category) VALUES (:u, :k, 'ldap')")


def _insert(conn, key: str) -> None:
    conn.execute(_INSERT, {"u": uuid.uuid4(), "k": key})


def _wait_until_blocked(pid: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    with engine.connect() as probe:
        while time.monotonic() < deadline:
            wait = probe.execute(
                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :p"), {"p": pid}
            ).scalar()
            if wait == "Lock":
                return
            time.sleep(0.05)
    raise AssertionError("the second writer never reached a lock wait")


def test_opposite_order_writers_do_not_deadlock_once_serialized():
    """Catches the advisory lock being dropped: A holds k1 and wants k2 while B holds k2
    and wants k1 — a textbook cycle that Postgres resolves by killing one of them."""
    k1, k2 = f"zz_ser_{uuid.uuid4().hex[:8]}", f"zz_ser_{uuid.uuid4().hex[:8]}"
    b_pid: list[int] = []
    b_started = threading.Event()
    b_error: list[BaseException] = []
    a_error: list[BaseException] = []

    conn_a = engine.connect()
    trans_a = conn_a.begin()
    serialize_auth_config_writes(conn_a)
    conn_a.execute(text("SET LOCAL lock_timeout = '30s'"))

    def writer_b() -> None:
        conn_b = engine.connect()
        trans_b = conn_b.begin()
        try:
            serialize_auth_config_writes(conn_b)
            conn_b.execute(text("SET LOCAL lock_timeout = '30s'"))
            b_pid.append(conn_b.execute(text("SELECT pg_backend_pid()")).scalar_one())
            b_started.set()
            _insert(conn_b, k2)
            _insert(conn_b, k1)
        except DBAPIError as exc:
            b_error.append(exc)
        finally:
            trans_b.rollback()
            conn_b.close()

    try:
        _insert(conn_a, k1)
        thread = threading.Thread(target=writer_b)
        thread.start()
        assert b_started.wait(timeout=15), "writer B never started"
        _wait_until_blocked(b_pid[0])
        try:
            _insert(conn_a, k2)
        except DBAPIError as exc:
            a_error.append(exc)
    finally:
        trans_a.rollback()
        conn_a.close()
    thread.join(timeout=60)

    assert not thread.is_alive()
    assert not a_error, f"writer A failed: {a_error[0]}"
    assert not b_error, f"writer B failed: {b_error[0]}"
