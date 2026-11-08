"""TaskLockManager against a real Redis: leases, fail-closed, and body errors.

Each test names the defect it pins (concurrency audit M5):

* one failed ping latched ``_redis_init_failed`` and every later task ran unlocked;
* Redis unavailable ran destructive tasks with no lock at all (fail open);
* the body's exceptions passed through the generator's ``except`` clauses, so a body
  ``RedisError`` became ``RuntimeError("generator didn't stop after throw()")`` and any
  other error (including Celery's ``Retry``) became ``TaskLockError``;
* the lock TTL was fixed, so a run longer than its TTL lost the lock to a second copy and
  a crashed run blocked the next one for the whole TTL.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
import uuid

import pytest
import redis
from celery.exceptions import Retry

from app.core.config import settings
from app.utils import task_lock

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("local_model_redis")]


@pytest.fixture(autouse=True)
def fresh_manager(monkeypatch):
    """A manager per test, so no state one test leaves behind decides another's result."""
    monkeypatch.setattr(task_lock, "task_lock_manager", task_lock.TaskLockManager())


@pytest.fixture
def lock_redis(local_model_redis, monkeypatch):
    monkeypatch.setattr(task_lock, "get_request_redis", lambda: local_model_redis)
    if "TASK_LOCK_LEASE_SECONDS" in type(settings).model_fields:
        monkeypatch.setattr(settings, "TASK_LOCK_LEASE_SECONDS", 1)
    return local_model_redis


@pytest.fixture
def key() -> str:
    return f"test:tasklock:{uuid.uuid4().hex[:12]}"


class _Down:
    """A client whose every command fails the way an unreachable Redis does."""

    def __getattr__(self, name):
        def fail(*_args, **_kwargs):
            raise redis.exceptions.ConnectionError("down")

        return fail


def test_one_failed_ping_does_not_disable_locking_for_later_calls(lock_redis, key, monkeypatch):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        return _Down() if calls["n"] == 1 else lock_redis

    monkeypatch.setattr(task_lock, "get_request_redis", flaky)
    with task_lock.task_lock_manager.acquire_lock(key, timeout=30):
        pass
    with task_lock.task_lock_manager.acquire_lock(key, timeout=30) as acquired:
        assert acquired is True
        assert lock_redis.exists(key) == 1, "the second call ran without taking the lock"


def test_redis_down_refuses_a_destructive_task(lock_redis, key, monkeypatch):
    monkeypatch.setattr(task_lock, "get_request_redis", lambda: _Down())
    with task_lock.task_lock_manager.acquire_lock(
        "search_index_maintenance", timeout=30
    ) as acquired:
        assert acquired is False


def test_redis_down_still_runs_a_task_classified_fail_open(lock_redis, monkeypatch):
    monkeypatch.setattr(task_lock, "get_request_redis", lambda: _Down())
    with task_lock.task_lock_manager.acquire_lock(
        "watch_source:fs_dispatch:7", timeout=30
    ) as acquired:
        assert acquired is True


def test_with_task_lock_skips_the_body_when_redis_is_down(monkeypatch):
    monkeypatch.setattr(task_lock, "get_request_redis", lambda: _Down())
    ran = []

    @task_lock.with_task_lock("backup_run", timeout=30)
    def body():
        ran.append(1)
        return "ran"

    result = body()
    assert ran == []
    assert result["skipped"] is True


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("body failed"),
        redis.exceptions.ConnectionError("body redis call failed"),
        Retry("retry later"),
    ],
    ids=["value-error", "redis-error", "celery-retry"],
)
def test_a_body_exception_reaches_the_caller_unchanged(lock_redis, key, exc):
    with pytest.raises(type(exc)) as raised:
        with task_lock.task_lock_manager.acquire_lock(key, timeout=30):
            raise exc
    assert raised.value is exc
    assert lock_redis.exists(key) == 0, "the lock must be released after a body error"


def test_a_live_holder_keeps_its_lock_past_the_lease_ttl(lock_redis, key):
    with task_lock.task_lock_manager.acquire_lock(key, timeout=60) as acquired:
        assert acquired
        # Sample the key across 2.5 lease TTLs: only renewal keeps it alive that long.
        started = time.monotonic()
        while True:
            assert lock_redis.exists(key) == 1, "the live holder lost its lease"
            if time.monotonic() - started >= 2.5:
                break
            time.sleep(0.1)
        with task_lock.task_lock_manager.acquire_lock(key, timeout=60) as second:
            assert second is False
    assert lock_redis.exists(key) == 0


def test_a_dead_holders_lease_expires_within_one_ttl(lock_redis, key):
    """A process that dies holding the lock (no release, no heartbeat) frees it."""
    port = lock_redis.connection_pool.connection_kwargs["port"]
    child = textwrap.dedent(
        f"""
        import os, redis
        from app.core.config import settings
        from app.utils import task_lock
        if "TASK_LOCK_LEASE_SECONDS" in type(settings).model_fields:
            settings.TASK_LOCK_LEASE_SECONDS = 1
        client = redis.Redis(host="127.0.0.1", port={port})
        task_lock.get_request_redis = lambda: client
        with task_lock.task_lock_manager.acquire_lock({key!r}, timeout=3600) as ok:
            assert ok
            print("HELD", flush=True)
            os._exit(0)
        """
    )
    backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    out = subprocess.run(
        [sys.executable, "-c", child],
        cwd=backend_dir,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONPATH": backend_dir},
        check=False,
    )
    assert "HELD" in out.stdout, out.stderr[-2000:]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not lock_redis.exists(key):
            break
        time.sleep(0.1)
    assert lock_redis.exists(key) == 0, "a dead holder blocked the lock for its full timeout"


def test_racing_acquirers_get_exactly_one_lock(lock_redis, key):
    n = 8
    barrier = threading.Barrier(n)
    results: list[bool] = []
    # Every worker stays inside its block until all have tried, so a winner cannot release
    # early and let a later thread acquire the key legitimately.
    all_tried = threading.Barrier(n + 1)

    def worker():
        barrier.wait()
        with task_lock.task_lock_manager.acquire_lock(key, timeout=30) as acquired:
            results.append(acquired)
            all_tried.wait(10)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    all_tried.wait(10)
    for t in threads:
        t.join(10)
    assert sorted(results) == [False] * (n - 1) + [True]


def test_every_fail_open_entry_names_a_lock_the_code_takes():
    """A stale classification entry would silently exempt some future key."""
    from pathlib import Path

    app_dir = Path(task_lock.__file__).resolve().parents[1]
    source = "\n".join(p.read_text() for p in app_dir.rglob("*.py") if p.name != "task_lock.py")
    assert task_lock.FAIL_OPEN_LOCKS, "the classification table is empty"
    for pattern in task_lock.FAIL_OPEN_LOCKS:
        stem = pattern.rstrip("*").split("{")[0]
        assert stem in source, f"FAIL_OPEN_LOCKS entry {pattern!r} matches no lock key in app/"
