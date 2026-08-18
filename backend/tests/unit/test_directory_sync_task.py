"""Tests for ``app/tasks/directory_sync_task.py`` (issue #474).

Two Celery tasks. ``services/directory_sync_service.py`` (own tests in
``test_directory_sync.py``) owns the actual policy — fail closed on ambiguity
not error, never touch ``super_admin``/``local`` accounts, disable-not-delete,
bounded by a per-run cap, revoke sessions on disable. This file's job is
narrower and specific to the TASK layer: does the Celery wrapper preserve
every one of those guarantees when it is the thing calling the service, not
just the service in isolation?

Two ways the wrapper specifically could break a guarantee without the
service's own tests ever catching it:

1. **The overlap lock could let two sweeps run concurrently** — the service
   has no lock of its own, and its docstring is explicit that revoking
   sessions (not just flipping ``is_active``) is what actually closes the
   window a stale refresh token would otherwise keep open. If the lock in
   ``run_directory_sync`` were wired wrong, a double dispatch could run two
   passes over the same accounts. ``test_run_directory_sync_skips_the_sweep_
   entirely_when_the_lock_is_held`` proves the locked-out call never even
   reaches the directory (candidate_users/probe_users untouched), not just
   that it "returns skipped".
2. **The ``dry_run=None`` passthrough could silently flip the safe default.**
   The beat always calls with ``dry_run=None`` so the *configured* value
   wins; a wrapper bug that coerced ``None`` to ``False`` would turn every
   scheduled tick into a live run regardless of the admin's setting.

The remaining tests drive ``run_directory_sync()`` end to end (through the
real ``run_scheduled_sweep`` -> ``sweep_ldap``) with the directory probe and
candidate list faked at the same seam ``test_directory_sync.py`` fakes them,
proving each service-level guarantee still holds when reached through the
task rather than called directly.

``check_directory_sync_schedule`` tests use the ``session_scope`` monkeypatch
pattern from ``test_speaker_attribute_migration_task.py`` to run the real
DB-driven due-check against the savepoint-backed test session.

``xdist_group("directory_sync_task_system_settings")``: ``tests/api/endpoints/
test_directory_sync_settings.py`` (issue #484) also writes ``directory_sync.*``
``SystemSettings`` keys now, so both files share this group to avoid the
``system_settings_key_key`` deadlock two xdist workers writing overlapping
keys in different orders would otherwise hit (issue #389).
"""

from __future__ import annotations

import contextlib
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest import mock

import pytest

from app.auth.ldap_auth import DIRECTORY_ABSENT
from app.auth.ldap_auth import DIRECTORY_PRESENT
from app.auth.ldap_auth import LdapDirectoryUnavailableError
from app.auth.ldap_auth import LdapProbe
from app.core.constants import CeleryQueues
from app.core.constants import CPUPriority
from app.services import directory_sync_service as svc
from app.tasks import directory_sync_task as task_mod

pytestmark = pytest.mark.xdist_group("directory_sync_task_system_settings")


# =============================================================================
# check_directory_sync_schedule — DB-driven due-check (real savepoint session)
# =============================================================================
@contextlib.contextmanager
def _scope_yielding(db_session):
    yield db_session


@pytest.fixture
def use_test_session(monkeypatch, db_session):
    monkeypatch.setattr(task_mod, "session_scope", lambda: _scope_yielding(db_session))
    return db_session


def test_check_schedule_disabled_dispatches_nothing(use_test_session):
    from app.services import system_settings_service as sss

    sss.set_setting(use_test_session, svc.KEY_ENABLED, "false", "disabled for this test")

    with mock.patch.object(task_mod.run_directory_sync, "apply_async") as dispatch:
        result = task_mod.check_directory_sync_schedule()

    assert result == {"status": "disabled"}
    dispatch.assert_not_called()


def test_check_schedule_not_due_dispatches_nothing_and_does_not_stamp(use_test_session):
    from app.services import system_settings_service as sss

    sss.set_setting(use_test_session, svc.KEY_ENABLED, "true", "enabled for this test")
    sss.set_setting(use_test_session, svc.KEY_SCHEDULE, "0 3 * * *", "cron for this test")

    from app.services import backup_service as bs

    # "Does not stamp" means UNCHANGED, not None: this is a SystemSettings row in the shared
    # dev DB that the running celery-beat commits whenever a sync window is claimed. The
    # backup sibling of this assertion went red exactly that way (see test_backup_tasks.py).
    before = svc.get_settings(use_test_session)["last_run_at"]

    with (
        mock.patch.object(bs, "is_due", return_value=False),
        mock.patch.object(task_mod.run_directory_sync, "apply_async") as dispatch,
    ):
        result = task_mod.check_directory_sync_schedule()

    assert result == {"status": "not_due", "schedule": "0 3 * * *"}
    dispatch.assert_not_called()
    assert svc.get_settings(use_test_session)["last_run_at"] == before


def test_check_schedule_due_stamps_last_run_and_dispatches_on_cpu_queue(use_test_session):
    from app.services import backup_service as bs
    from app.services import system_settings_service as sss

    sss.set_setting(use_test_session, svc.KEY_ENABLED, "true", "enabled for this test")
    sss.set_setting(use_test_session, svc.KEY_SCHEDULE, "0 3 * * *", "cron for this test")

    with (
        mock.patch.object(bs, "is_due", return_value=True),
        mock.patch.object(task_mod.run_directory_sync, "apply_async") as dispatch,
    ):
        result = task_mod.check_directory_sync_schedule()

    assert result == {"status": "dispatched", "schedule": "0 3 * * *"}
    # LDAP reconciliation is network-bound, never GPU-adjacent.
    dispatch.assert_called_once_with(queue=CeleryQueues.CPU, priority=CPUPriority.MAINTENANCE)
    cfg = svc.get_settings(use_test_session)
    assert cfg["last_run_at"] is not None
    stamped = datetime.fromisoformat(cfg["last_run_at"])
    assert datetime.now(UTC) - stamped < timedelta(seconds=10)


# =============================================================================
# run_directory_sync — the overlap lock
# =============================================================================
@contextlib.contextmanager
def _always_acquire(_key, timeout=0, blocking_timeout=0):
    yield True


@contextlib.contextmanager
def _never_acquire(_key, timeout=0, blocking_timeout=0):
    yield False


def test_run_directory_sync_skips_the_sweep_entirely_when_the_lock_is_held(monkeypatch):
    """Not just 'returns skipped' — the directory must never even be probed,
    which is what actually prevents a concurrent second pass."""
    monkeypatch.setattr(task_mod.task_lock_manager, "acquire_lock", _never_acquire, raising=False)

    def _must_not_run(*_a, **_kw):
        pytest.fail("run_scheduled_sweep must not run while the lock is held")

    monkeypatch.setattr(svc, "run_scheduled_sweep", _must_not_run)

    result = task_mod.run_directory_sync()

    assert result == {"status": "skipped", "reason": "directory sync already running"}


def test_run_directory_sync_runs_and_returns_the_sweep_report_when_lock_acquired(monkeypatch):
    monkeypatch.setattr(task_mod.task_lock_manager, "acquire_lock", _always_acquire, raising=False)
    canned = {"status": "ok", "disabled": 0, "checked": 0}
    with mock.patch.object(svc, "run_scheduled_sweep", return_value=canned) as sweep:
        result = task_mod.run_directory_sync(dry_run=True)

    assert result == canned
    sweep.assert_called_once_with(dry_run=True)


def test_run_directory_sync_none_dry_run_is_passed_through_unmodified(monkeypatch):
    """The beat always calls with dry_run=None so the CONFIGURED value wins.
    A wrapper that coerced None -> False would silently turn every scheduled
    tick into a live run regardless of the admin's setting."""
    monkeypatch.setattr(task_mod.task_lock_manager, "acquire_lock", _always_acquire, raising=False)
    canned = {"status": "ok", "would_disable": 0}
    with mock.patch.object(svc, "run_scheduled_sweep", return_value=canned) as sweep:
        result = task_mod.run_directory_sync(dry_run=None)

    sweep.assert_called_once_with(dry_run=None)
    # The task must pass the sweep's report through unmodified, not just call it.
    assert result == canned


# =============================================================================
# End-to-end through the task: every documented service guarantee still
# holds when the TASK is the caller, not just when the service is called
# directly (as test_directory_sync.py already proves).
# =============================================================================
class FakeUser:
    """Enough of ``models.User`` for the sweep — mirrors test_directory_sync.py."""

    def __init__(self, uid, email, *, role="user", auth_type="ldap", is_active=True):
        self.id = uid
        self.uuid = f"019ec90a-0000-7000-8000-0000000000{uid:02d}"
        self.email = email
        self.full_name = email
        self.role = role
        self.auth_type = auth_type
        self.ldap_uid = email.split("@")[0]
        self.is_active = is_active


class _EmptyQuery:
    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return []


class FakeSession:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def query(self, *args, **kwargs):
        return _EmptyQuery()

    def add(self, obj):
        pass


class RecordingAudit:
    def __init__(self):
        self.events = []

    def log(self, **kwargs):
        self.events.append(kwargs)


@pytest.fixture
def task_env(monkeypatch):
    """Wire the task to run for real, with the directory faked and the lock free."""
    monkeypatch.setattr(task_mod.task_lock_manager, "acquire_lock", _always_acquire, raising=False)
    # run_scheduled_sweep opens its own session when none is passed — replace
    # it with a FakeSession regardless of the (always-None) db argument, the
    # same technique test_speaker_attribute_migration_task.py uses for
    # session_scope.
    monkeypatch.setattr(svc, "_session", lambda _db: contextlib.nullcontext(FakeSession()))

    revocations: list[tuple[str, str]] = []

    def _revoke(_db, user, *, reason):
        revocations.append((str(user.email), reason))
        return 2

    monkeypatch.setattr(svc, "revoke_all_sessions", _revoke)
    audit = RecordingAudit()
    monkeypatch.setattr(svc, "audit_logger", audit)
    return revocations, audit


def _wire_directory(monkeypatch, users, statuses):
    monkeypatch.setattr(svc, "candidate_users", lambda _db: list(users))

    def _probe(_db, candidates):
        for user in candidates:
            answer = statuses.get(str(user.email), DIRECTORY_PRESENT)
            if isinstance(answer, BaseException):
                raise answer
            yield user, answer if isinstance(answer, LdapProbe) else LdapProbe(answer)

    monkeypatch.setattr(svc, "probe_users", _probe)


def _settings(*, enabled=True, dry_run=False, max_disables=10):
    return {
        "enabled": enabled,
        "schedule": "*/15 * * * *",
        "dry_run": dry_run,
        "max_disables_per_run": max_disables,
        "last_run_at": None,
        "last_result": None,
    }


def test_absent_user_is_disabled_and_sessions_revoked_through_the_task(monkeypatch, task_env):
    revocations, audit = task_env
    monkeypatch.setattr(svc, "get_settings", lambda _db: _settings())
    gone = FakeUser(1, "gone@example.com")
    stays = FakeUser(2, "stays@example.com")
    _wire_directory(monkeypatch, [gone, stays], {"gone@example.com": DIRECTORY_ABSENT})

    result = task_mod.run_directory_sync()

    assert gone.is_active is False
    assert stays.is_active is True
    assert result["disabled"] == 1
    assert revocations == [("gone@example.com", "directory_sync:absent_from_directory")]
    assert audit.events[0]["details"]["reason"] == "absent_from_directory"


def test_super_admin_and_local_accounts_are_never_touched_through_the_task(monkeypatch, task_env):
    revocations, _audit = task_env
    monkeypatch.setattr(svc, "get_settings", lambda _db: _settings())
    breakglass = FakeUser(1, "root@example.com", role="super_admin")
    local = FakeUser(2, "local@example.com", auth_type="local")
    ordinary = FakeUser(3, "ldap@example.com")
    _wire_directory(
        monkeypatch,
        [breakglass, local, ordinary],
        dict.fromkeys(
            ["root@example.com", "local@example.com", "ldap@example.com"], DIRECTORY_ABSENT
        ),
    )

    result = task_mod.run_directory_sync()

    assert breakglass.is_active is True
    assert local.is_active is True
    assert ordinary.is_active is False
    assert result["disabled"] == 1
    assert revocations == [("ldap@example.com", "directory_sync:absent_from_directory")]


def test_unreachable_directory_disables_nobody_through_the_task(monkeypatch, task_env):
    revocations, audit = task_env
    monkeypatch.setattr(svc, "get_settings", lambda _db: _settings())
    users = [FakeUser(i, f"u{i}@example.com") for i in range(1, 4)]
    _wire_directory(
        monkeypatch, users, {"u1@example.com": LdapDirectoryUnavailableError("bind failed")}
    )

    result = task_mod.run_directory_sync()

    assert result["status"] == "directory_unavailable"
    assert result["disabled"] == 0
    assert all(u.is_active for u in users)
    assert revocations == []
    assert audit.events == []


def test_max_disables_per_run_caps_the_pass_through_the_task(monkeypatch, task_env):
    revocations, _audit = task_env
    monkeypatch.setattr(svc, "get_settings", lambda _db: _settings(max_disables=2))
    users = [FakeUser(i, f"u{i}@example.com") for i in range(1, 6)]
    _wire_directory(monkeypatch, users, dict.fromkeys([u.email for u in users], DIRECTORY_ABSENT))

    result = task_mod.run_directory_sync()

    assert result["disabled"] == 2
    assert result["capped"] is True
    assert [u.is_active for u in users] == [False, False, True, True, True]
    assert len(revocations) == 2


def test_dry_run_true_reports_but_changes_nothing_through_the_task(monkeypatch, task_env):
    """The task's own ``dry_run`` argument overrides the (here live) configured
    value — this is the admin 'Preview' action's actual entry point."""
    revocations, audit = task_env
    monkeypatch.setattr(svc, "get_settings", lambda _db: _settings(dry_run=False))
    users = [FakeUser(i, f"u{i}@example.com") for i in range(1, 3)]
    _wire_directory(monkeypatch, users, dict.fromkeys([u.email for u in users], DIRECTORY_ABSENT))

    result = task_mod.run_directory_sync(dry_run=True)

    assert all(u.is_active for u in users)
    assert revocations == []
    assert audit.events == []
    assert result["disabled"] == 0
    assert result["would_disable"] == 2


def test_disabled_feature_never_reaches_the_directory_through_the_task(monkeypatch, task_env):
    monkeypatch.setattr(svc, "get_settings", lambda _db: _settings(enabled=False))
    monkeypatch.setattr(
        svc, "candidate_users", lambda _db: pytest.fail("must not query users when disabled")
    )

    result = task_mod.run_directory_sync()

    assert result == {"status": "disabled"}
