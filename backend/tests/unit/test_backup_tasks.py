"""Tests for ``app/tasks/backup_tasks.py`` (issue #474).

Four Celery tasks, all thin wrappers whose own logic — not
``backup_service``/``media_mirror_service``'s, already covered by
``test_backup_service.py``/``test_media_mirror_service.py`` — is the DB-driven
due-check + window-claim, and the overlap-guarding Redis lock:

* ``check_backup_schedule`` / ``check_mirror_schedule`` — disabled -> no dispatch;
  not due -> no dispatch, no stamp; due -> stamps ``*.last_run_at`` BEFORE
  dispatching (real DB state, read back through ``get_settings``) and dispatches
  the run task on the documented queue/priority.
* ``run_backup`` / ``run_media_mirror`` — skip without running the real work when
  the overlap lock is already held; run it and pass the result through unchanged
  otherwise.

``run_backup``'s lock exists specifically because of issue #284 A1.18: a double
beat tick — or a manual "Run Now" landing on a scheduled window — started two
concurrent ``pg_dump`` processes against the same database. Most of these tests
fake ``task_lock_manager.acquire_lock`` the way ``test_watch_fs_event_watchdog.
py``/``test_task_session_lifetime.py`` already do for this exact seam, but
``test_run_backup_lock_actually_prevents_a_concurrent_run`` proves the fix with
a REAL Redis distributed lock — a throwaway, unauthenticated ``redis:7-alpine``
container private to this module (never the password-protected dev-stack Redis,
same pattern ``test_migration_progress_service.py`` uses) — so the guarantee is
demonstrated, not just asserted.

``xdist_group("backup_system_settings")``: this file writes both ``backup.*``
(via ``backup_service``) and ``backup.mirror_*`` (via ``media_mirror_service``)
``SystemSettings`` rows, which share the ``backup.%`` key namespace with
``test_backup_{service,metrics,alerts}.py`` and
``test_admin_backup_mirror_routes.py`` (issue #389 — two xdist workers
inserting overlapping keys in different orders deadlock on
``system_settings_key_key``).
"""

from __future__ import annotations

import contextlib
import shutil
import socket
import subprocess
import time
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest import mock

import pytest
import redis

from app.core.constants import CeleryQueues
from app.core.constants import CPUPriority  # noqa: F401 - referenced for parity/documentation
from app.core.constants import DownloadPriority
from app.core.constants import UtilityPriority
from app.services import backup_service as bs
from app.services import media_mirror_service as mm
from app.tasks import backup_tasks
from app.utils.task_lock import TaskLockManager

pytestmark = pytest.mark.xdist_group("backup_system_settings")


# =============================================================================
# Fixtures
# =============================================================================
@contextlib.contextmanager
def _scope_yielding(db_session):
    yield db_session


@pytest.fixture
def use_test_session(monkeypatch, db_session):
    """Point the tasks' ``session_scope()`` at the savepoint-backed test session."""
    monkeypatch.setattr(backup_tasks, "session_scope", lambda: _scope_yielding(db_session))
    return db_session


@contextlib.contextmanager
def _always_acquire(_key, timeout=0, blocking_timeout=0):
    yield True


@contextlib.contextmanager
def _never_acquire(_key, timeout=0, blocking_timeout=0):
    yield False


# =============================================================================
# check_backup_schedule
# =============================================================================
def test_check_backup_schedule_disabled_dispatches_nothing(use_test_session):
    bs.update_settings(use_test_session, enabled=False)

    with mock.patch.object(backup_tasks.run_backup, "apply_async") as dispatch:
        result = backup_tasks.check_backup_schedule()

    assert result == {"status": "disabled"}
    dispatch.assert_not_called()


def test_check_backup_schedule_not_due_dispatches_nothing_and_does_not_stamp(use_test_session):
    bs.update_settings(use_test_session, enabled=True, schedule="0 3 * * *")
    # "Does not stamp" means the value is UNCHANGED, not that it is None. `backup.last_run_at`
    # is a SystemSettings row in the shared dev DB that the running celery-beat commits
    # whenever a scheduled backup fires, so asserting `is None` only held on a database where
    # beat had never run — it went red the first time the dev stack claimed a backup window.
    before = bs.get_settings(use_test_session)["last_run_at"]

    with (
        mock.patch.object(bs, "is_due", return_value=False),
        mock.patch.object(backup_tasks.run_backup, "apply_async") as dispatch,
    ):
        result = backup_tasks.check_backup_schedule()

    assert result == {"status": "not_due", "schedule": "0 3 * * *"}
    dispatch.assert_not_called()
    assert bs.get_settings(use_test_session)["last_run_at"] == before


def test_check_backup_schedule_due_stamps_last_run_and_dispatches(use_test_session):
    bs.update_settings(use_test_session, enabled=True, schedule="0 3 * * *")

    with (
        mock.patch.object(bs, "is_due", return_value=True),
        mock.patch.object(backup_tasks.run_backup, "apply_async") as dispatch,
    ):
        result = backup_tasks.check_backup_schedule()

    assert result == {"status": "dispatched", "schedule": "0 3 * * *"}
    dispatch.assert_called_once_with(queue="utility", priority=UtilityPriority.ROUTINE)
    # Real DB state: the window was claimed BEFORE dispatch, not merely logged.
    cfg = bs.get_settings(use_test_session)
    assert cfg["last_run_at"] is not None
    stamped = datetime.fromisoformat(cfg["last_run_at"])
    assert datetime.now(UTC) - stamped < timedelta(seconds=10)


def test_check_backup_schedule_claims_the_window_even_though_dispatch_is_outside_the_session(
    use_test_session,
):
    """The stamp-then-dispatch ordering is the actual overlap guard: it must be
    visible to the NEXT beat tick's read even though ``apply_async`` runs after
    the session that wrote it has already closed."""
    bs.update_settings(use_test_session, enabled=True, schedule="* * * * *")

    with mock.patch.object(backup_tasks.run_backup, "apply_async"):
        backup_tasks.check_backup_schedule()
        second_tick = backup_tasks.check_backup_schedule()

    # A second tick in the SAME minute must not re-fire (is_due sees the fresh stamp).
    assert second_tick["status"] == "not_due"


# =============================================================================
# run_backup
# =============================================================================
def test_run_backup_runs_perform_backup_and_returns_its_result_when_lock_acquired(monkeypatch):
    monkeypatch.setattr(
        backup_tasks.task_lock_manager, "acquire_lock", _always_acquire, raising=False
    )
    canned = {"ok": True, "status": "success", "filename": "opentranscribe-x.dump"}

    with mock.patch.object(bs, "perform_backup", return_value=canned) as perform:
        result = backup_tasks.run_backup()

    assert result == canned
    perform.assert_called_once_with()


def test_run_backup_skips_perform_backup_when_lock_not_acquired(monkeypatch):
    monkeypatch.setattr(
        backup_tasks.task_lock_manager, "acquire_lock", _never_acquire, raising=False
    )

    with mock.patch.object(bs, "perform_backup") as perform:
        result = backup_tasks.run_backup()

    assert result == {"status": "skipped", "reason": "backup already running"}
    perform.assert_not_called()


# --- real-Redis proof of the overlap guard ----------------------------------
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def real_lock_manager(monkeypatch):
    """A ``TaskLockManager`` wired to a throwaway, unauthenticated Redis.

    Isolated container (never the password-protected dev-stack Redis on
    5177) so this test proves the REAL ``redis.Redis.lock()`` mutual-exclusion
    semantics ``run_backup`` depends on, not a Python-level stand-in.
    """
    if shutil.which("docker") is None:
        pytest.skip("docker is not on PATH — cannot start a throwaway Redis for this test")

    port = _free_port()
    name = f"ot-test-redis-backup-tasks-{uuid.uuid4().hex[:10]}"
    # No try/except here: the precondition check above is the intended skip
    # path (docker missing entirely, e.g. some CI runners). If the docker CLI
    # IS present but this invocation fails for some other reason, that is a
    # real environment problem worth failing loudly on, not masking as a skip.
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

        mgr = TaskLockManager()
        mgr._redis_client = client
        monkeypatch.setattr(backup_tasks, "task_lock_manager", mgr)
        yield mgr
    finally:
        client.close()
        subprocess.run(["docker", "stop", name], capture_output=True, timeout=30)


def test_run_backup_lock_actually_prevents_a_concurrent_run(real_lock_manager):
    """Issue #284 A1.18: without a lock, a double tick starts two ``pg_dump``
    processes racing on the same retention set. Hold the real Redis lock
    ourselves (standing in for a first, still-running invocation) and prove a
    second ``run_backup()`` call is refused and never touches ``perform_backup``
    — then release and prove a subsequent call proceeds normally."""
    with real_lock_manager.acquire_lock(
        backup_tasks.BACKUP_LOCK_KEY, timeout=backup_tasks.BACKUP_LOCK_TIMEOUT
    ) as acquired:
        assert acquired is True
        with mock.patch.object(bs, "perform_backup") as perform:
            blocked_result = backup_tasks.run_backup()
        perform.assert_not_called()

    assert blocked_result == {"status": "skipped", "reason": "backup already running"}

    # The lock was released on exit — a fresh call now proceeds for real.
    with mock.patch.object(bs, "perform_backup", return_value={"ok": True, "status": "success"}):
        free_result = backup_tasks.run_backup()
    assert free_result == {"ok": True, "status": "success"}


# =============================================================================
# check_mirror_schedule
# =============================================================================
def test_check_mirror_schedule_disabled_dispatches_nothing(use_test_session):
    mm.update_settings(use_test_session, enabled=False)

    with mock.patch.object(backup_tasks.run_media_mirror, "apply_async") as dispatch:
        result = backup_tasks.check_mirror_schedule()

    assert result == {"status": "disabled"}
    dispatch.assert_not_called()


def test_check_mirror_schedule_not_due_dispatches_nothing(use_test_session):
    mm.update_settings(use_test_session, enabled=True, schedule="0 4 * * *")

    with (
        mock.patch.object(bs, "is_due", return_value=False),
        mock.patch.object(backup_tasks.run_media_mirror, "apply_async") as dispatch,
    ):
        result = backup_tasks.check_mirror_schedule()

    assert result == {"status": "not_due", "schedule": "0 4 * * *"}
    dispatch.assert_not_called()
    assert mm.get_settings(use_test_session)["last_run_at"] is None


def test_check_mirror_schedule_due_stamps_and_dispatches_on_download_queue(use_test_session):
    mm.update_settings(use_test_session, enabled=True, schedule="0 4 * * *")

    with (
        mock.patch.object(bs, "is_due", return_value=True),
        mock.patch.object(backup_tasks.run_media_mirror, "apply_async") as dispatch,
    ):
        result = backup_tasks.check_mirror_schedule()

    assert result == {"status": "dispatched", "schedule": "0 4 * * *"}
    # Bulk object I/O belongs on the download queue, never gpu/cpu.
    dispatch.assert_called_once_with(
        queue=CeleryQueues.DOWNLOAD, priority=DownloadPriority.PLAYLIST
    )
    cfg = mm.get_settings(use_test_session)
    assert cfg["last_run_at"] is not None
    stamped = datetime.fromisoformat(cfg["last_run_at"])
    assert datetime.now(UTC) - stamped < timedelta(seconds=10)


def test_mirror_schedule_is_a_separate_namespace_from_backup_schedule(use_test_session):
    """Regression guard: the two due-checks must never share state — enabling
    one must not make the other appear due, and vice versa."""
    bs.update_settings(use_test_session, enabled=True, schedule="0 3 * * *")
    mm.update_settings(use_test_session, enabled=False)

    with mock.patch.object(backup_tasks.run_media_mirror, "apply_async") as dispatch:
        result = backup_tasks.check_mirror_schedule()

    assert result == {"status": "disabled"}
    dispatch.assert_not_called()


# =============================================================================
# run_media_mirror
# =============================================================================
def test_run_media_mirror_runs_and_passes_max_objects_through_when_lock_acquired(monkeypatch):
    monkeypatch.setattr(
        backup_tasks.task_lock_manager, "acquire_lock", _always_acquire, raising=False
    )
    canned = {"ok": True, "status": "success", "mirrored": 3}

    with mock.patch(
        "app.services.media_mirror_engine.perform_mirror", return_value=canned
    ) as perform:
        result = backup_tasks.run_media_mirror(max_objects=50)

    assert result == canned
    perform.assert_called_once_with(max_objects=50)


def test_run_media_mirror_skips_when_lock_not_acquired(monkeypatch):
    monkeypatch.setattr(
        backup_tasks.task_lock_manager, "acquire_lock", _never_acquire, raising=False
    )

    with mock.patch("app.services.media_mirror_engine.perform_mirror") as perform:
        result = backup_tasks.run_media_mirror()

    assert result == {"status": "skipped", "reason": "mirror already running"}
    perform.assert_not_called()


def test_run_media_mirror_defaults_max_objects_to_none_for_the_beat_dispatched_case(monkeypatch):
    """The beat schedule always calls with no argument (unbounded run); only
    manual "Run Now" and tests bound it."""
    monkeypatch.setattr(
        backup_tasks.task_lock_manager, "acquire_lock", _always_acquire, raising=False
    )

    with mock.patch(
        "app.services.media_mirror_engine.perform_mirror", return_value={"ok": True}
    ) as perform:
        result = backup_tasks.run_media_mirror()

    perform.assert_called_once_with(max_objects=None)
    # The task must pass the engine's result through unmodified, not just call it.
    assert result == {"ok": True}
