"""Tests for ``app/services/migration_progress_service.py`` (issue #474).

``MigrationProgressService`` backs the Redis-tracked progress panel for four
migration orchestrators (v3->v4 embeddings, speaker attributes, combined
speaker analysis, imohash recompute) and had zero test coverage. Real Redis is
required — this module's actual job is a Lua script's atomic semantics and a
JSON round-trip through Redis, neither of which a mock proves.

Rather than borrow the live dev-stack Redis (localhost:5177, password-protected,
and shared with whatever else is running against it), these tests bring up a
throwaway, unauthenticated ``redis:7-alpine`` container on a dynamically chosen
port — the same "isolated real service, never the dev stack" pattern
documented in ``backend/tests/CLAUDE.md`` for Postgres. It never touches the
project's Redis credentials or the dev stack's data.

FIX (real bug, confirmed against real Redis before writing this file): lua-cjson
cannot distinguish an empty JSON array from an empty JSON object once decoded to
an empty Lua table, so ``_INCREMENT_LUA``'s round-trip re-encode of the *whole*
status blob silently turned ``"failed_files": []`` into ``"failed_files": {}``
the moment ``increment_processed()`` ran with zero failures recorded so far —
which is the common case for most of a migration's run. That broke the
``MigrationStatus.failed_files: list[str]`` contract for every consumer,
including the JSON wire response the admin UI reads. It self-heals the instant
a real failure is appended (a non-empty Lua table round-trips as a JSON array
correctly), which is exactly why nobody had noticed it.
``test_failed_files_survives_a_success_only_increment_as_a_list`` and
``test_failed_files_self_heals_once_a_real_failure_is_appended`` pin both
halves. The fix lives in ``get_status()``: normalize a dict-shaped
``failed_files`` back to ``[]`` at the one chokepoint every reader goes
through.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
import redis

from app.services.migration_progress_service import MigrationProgressService

# All tests in this file share ONE module-scoped throwaway Redis container
# (see redis_container below). Without xdist_group, pytest-xdist's default
# "load" scheduling can spread individual test items across many worker
# processes, each paying its own ~1-3s `docker run` + readiness-poll cost —
# observed inflating this file's wall time from ~6s to ~55s. Grouping keeps
# every test — and therefore the one container — on a single worker.
pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("migration_progress_service_redis")]


# =============================================================================
# Throwaway Redis container — isolated from the dev stack, never touches it.
# =============================================================================
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def redis_container():
    """A throwaway, unauthenticated Redis on a private port for this module only."""
    if shutil.which("docker") is None:
        pytest.skip("docker is not on PATH — cannot start a throwaway Redis for this suite")

    port = _free_port()
    name = f"ot-test-redis-mps-{uuid.uuid4().hex[:10]}"
    # No try/except here: the precondition check above is the intended skip path
    # (docker missing entirely). If the docker CLI IS present but this invocation
    # fails for some other reason, that is a real environment problem worth
    # failing loudly on, not masking as a skip.
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-p",
            f"127.0.0.1:{port}:6379",
            "redis:7-alpine",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )

    client = redis.Redis(host="127.0.0.1", port=port, db=0)
    try:
        deadline = time.time() + 15
        ready = False
        while time.time() < deadline:
            try:
                if client.ping():
                    ready = True
                    break
            except redis.exceptions.ConnectionError:
                time.sleep(0.1)
        if not ready:
            pytest.fail("throwaway redis container did not become ready in time")
        yield client
    finally:
        client.close()
        subprocess.run(["docker", "stop", name], capture_output=True, timeout=30)


@pytest.fixture
def svc(redis_container):
    """A fresh MigrationProgressService, wired directly to the throwaway Redis.

    Setting ``_redis_client`` bypasses the module's lazy ``get_redis()`` (the
    process-wide singleton pointed at the real, password-protected dev-stack
    Redis) entirely — the same technique ``test_speaker_attribute_migration_
    task.py`` uses for its FakeRedis. A UUID-suffixed key prefix keeps this
    test's keys from colliding with any other test sharing the container.
    """
    instance = MigrationProgressService(key_prefix=f"test_mps_{uuid.uuid4().hex[:10]}")
    instance._redis_client = redis_container
    yield instance
    redis_container.delete(instance._get_key("status"), instance._get_key("completed"))


# =============================================================================
# get_status() defaults / no data
# =============================================================================
def test_get_status_with_nothing_started_returns_defaults(svc):
    status = svc.get_status()
    assert status == {
        "running": False,
        "total_files": 0,
        "processed_files": 0,
        "failed_files": [],
        "started_at": None,
        "completed_at": None,
        "orchestrator_task_id": None,
        "last_updated": None,
    }
    assert svc.is_running() is False


def test_get_status_handles_corrupt_json_without_raising(svc, redis_container):
    redis_container.set(svc._get_key("status"), b"not valid json{{{")
    status = svc.get_status()
    assert status["running"] is False
    assert status["failed_files"] == []


# =============================================================================
# start_migration()
# =============================================================================
def test_start_migration_writes_full_status_and_clears_completion_flag(svc, redis_container):
    redis_container.set(svc._get_key("completed"), "1")  # stale flag from a prior run

    ok = svc.start_migration(total_files=7, task_id="task-abc")

    assert ok is True
    status = svc.get_status()
    assert status["running"] is True
    assert status["total_files"] == 7
    assert status["processed_files"] == 0
    assert status["failed_files"] == []
    assert status["orchestrator_task_id"] == "task-abc"
    assert status["started_at"] is not None
    assert status["completed_at"] is None
    # The stale completion flag from an earlier run must not leak forward.
    assert redis_container.get(svc._get_key("completed")) is None
    # 24h safety-net TTL is actually set on the status key.
    ttl = redis_container.ttl(svc._get_key("status"))
    assert 0 < ttl <= 86400


# =============================================================================
# increment_processed() — atomic Lua increment, and the cjson type bug
# =============================================================================
def test_increment_processed_success_increments_count(svc):
    svc.start_migration(total_files=3)

    assert svc.increment_processed(success=True) is True
    assert svc.get_status()["processed_files"] == 1

    svc.increment_processed(success=True)
    assert svc.get_status()["processed_files"] == 2


def test_increment_processed_with_no_status_key_returns_true_but_records_nothing(svc):
    """The Lua script no-ops (returns 0) when GET finds nothing — start_migration
    was never called. The Python wrapper still reports True because eval() did
    not raise; there is genuinely nothing to observe as failed here, but the
    absence of a crash (and of any status materializing) is the behavior."""
    assert svc.increment_processed(success=True) is True
    assert svc.get_status() == {
        "running": False,
        "total_files": 0,
        "processed_files": 0,
        "failed_files": [],
        "started_at": None,
        "completed_at": None,
        "orchestrator_task_id": None,
        "last_updated": None,
    }


def test_increment_processed_failure_appends_the_uuid(svc):
    svc.start_migration(total_files=3)

    svc.increment_processed(success=False, file_uuid="file-1")

    status = svc.get_status()
    assert status["processed_files"] == 1
    assert status["failed_files"] == ["file-1"]


def test_increment_processed_failure_does_not_duplicate_the_same_uuid(svc):
    svc.start_migration(total_files=3)

    svc.increment_processed(success=False, file_uuid="file-1")
    svc.increment_processed(success=False, file_uuid="file-1")

    status = svc.get_status()
    assert status["processed_files"] == 2  # both increments count
    assert status["failed_files"] == ["file-1"]  # but the uuid is recorded once


def test_increment_processed_failure_appends_distinct_uuids_in_order(svc):
    svc.start_migration(total_files=3)

    svc.increment_processed(success=False, file_uuid="file-1")
    svc.increment_processed(success=False, file_uuid="file-2")

    assert svc.get_status()["failed_files"] == ["file-1", "file-2"]


def test_failed_files_survives_a_success_only_increment_as_a_list(svc):
    """FIX regression: pins the cjson empty-array-vs-object bug described in
    this module's docstring. Before the fix, ``failed_files`` came back as
    ``{}`` (a dict) after the very first success-only increment; any consumer
    treating it as a list (append, JSON array on the wire, ``in`` membership
    against a specific uuid) silently got the wrong type."""
    svc.start_migration(total_files=5)

    svc.increment_processed(success=True)
    svc.increment_processed(success=True)
    svc.increment_processed(success=True)

    status = svc.get_status()
    assert status["processed_files"] == 3
    assert isinstance(status["failed_files"], list), (
        f"failed_files must stay a list with zero failures, got {type(status['failed_files'])}"
    )
    assert status["failed_files"] == []


def test_failed_files_self_heals_once_a_real_failure_is_appended(svc):
    """The corrupted (dict-shaped) Redis value self-heals to a real JSON array
    the moment a non-empty failed_files list is written — this is WHY the bug
    went unnoticed. Confirms the fix does not depend on that self-heal (the
    prior test proves the zero-failure window is also correct)."""
    svc.start_migration(total_files=5)
    svc.increment_processed(success=True)  # corrupts the raw Redis value pre-fix
    svc.increment_processed(success=False, file_uuid="file-9")

    status = svc.get_status()
    assert status["failed_files"] == ["file-9"]
    assert status["processed_files"] == 2


def test_increment_processed_is_atomic_under_concurrent_callers(svc):
    """The docstring's claim: 'safe to call concurrently from multiple Celery
    workers — the Lua script executes atomically within Redis.' Twenty threads
    each increment once; a non-atomic read-modify-write would lose updates."""
    svc.start_migration(total_files=20)

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda _: svc.increment_processed(success=True), range(20)))

    assert all(results)
    assert svc.get_status()["processed_files"] == 20


# =============================================================================
# complete_migration() — SETNX guard, single-winner semantics
# =============================================================================
def test_complete_migration_marks_status_not_running(svc):
    svc.start_migration(total_files=2)
    svc.increment_processed(success=True)

    ok = svc.complete_migration(success=True)

    assert ok is True
    status = svc.get_status()
    assert status["running"] is False
    assert status["completed_at"] is not None
    assert status["processed_files"] == 1  # unrelated fields untouched


def test_complete_migration_only_the_first_caller_wins(svc):
    svc.start_migration(total_files=2)

    first = svc.complete_migration(success=True)
    second = svc.complete_migration(success=True)

    assert first is True
    assert second is False, "a second concurrent batch task must not redo completion work"


def test_complete_migration_shortens_the_status_ttl(svc, redis_container):
    svc.start_migration(total_files=2)
    svc.complete_migration(success=True)

    ttl = redis_container.ttl(svc._get_key("status"))
    assert 0 < ttl <= 3600, "completed status is kept only 1h, not the running 24h TTL"


# =============================================================================
# force_stop()
# =============================================================================
def test_force_stop_while_running_marks_not_running(svc):
    svc.start_migration(total_files=4)

    ok = svc.force_stop()

    assert ok is True
    status = svc.get_status()
    assert status["running"] is False
    assert status["completed_at"] is not None


def test_force_stop_when_nothing_is_running_returns_false_and_changes_nothing(svc):
    ok = svc.force_stop()

    assert ok is False
    assert svc.get_status()["running"] is False


# =============================================================================
# clear_status()
# =============================================================================
def test_clear_status_removes_both_keys(svc, redis_container):
    svc.start_migration(total_files=4)
    svc.complete_migration(success=True)
    assert redis_container.get(svc._get_key("completed")) is not None

    ok = svc.clear_status()

    assert ok is True
    assert svc.get_status() == {
        "running": False,
        "total_files": 0,
        "processed_files": 0,
        "failed_files": [],
        "started_at": None,
        "completed_at": None,
        "orchestrator_task_id": None,
        "last_updated": None,
    }
    assert redis_container.get(svc._get_key("completed")) is None


# =============================================================================
# Redis unavailable — every method degrades to a safe no-op, never raises
# =============================================================================
@pytest.fixture
def unreachable_svc(monkeypatch):
    def _boom():
        raise redis.exceptions.ConnectionError("no redis for this test")

    monkeypatch.setattr("app.services.migration_progress_service.get_redis", _boom)
    return MigrationProgressService(key_prefix=f"test_mps_unreachable_{uuid.uuid4().hex[:8]}")


def test_all_writes_return_false_when_redis_is_unreachable(unreachable_svc):
    assert unreachable_svc.start_migration(total_files=5) is False
    assert unreachable_svc.increment_processed(success=True) is False
    assert unreachable_svc.complete_migration(success=True) is False
    assert unreachable_svc.clear_status() is False
    assert unreachable_svc.force_stop() is False


def test_get_status_returns_defaults_when_redis_is_unreachable(unreachable_svc):
    status = unreachable_svc.get_status()
    assert status["running"] is False
    assert status["failed_files"] == []
    assert unreachable_svc.is_running() is False


# =============================================================================
# key_prefix — the property that makes 4 orchestrators share one Redis safely
# =============================================================================
def test_two_instances_with_different_prefixes_do_not_see_each_others_status(redis_container):
    a = MigrationProgressService(key_prefix="test_mps_a")
    b = MigrationProgressService(key_prefix="test_mps_b")
    a._redis_client = redis_container
    b._redis_client = redis_container
    try:
        a.start_migration(total_files=1, task_id="a-task")

        assert a.is_running() is True
        assert b.is_running() is False
        assert b.get_status()["orchestrator_task_id"] is None
    finally:
        redis_container.delete(a._get_key("status"), a._get_key("completed"))
        redis_container.delete(b._get_key("status"), b._get_key("completed"))
