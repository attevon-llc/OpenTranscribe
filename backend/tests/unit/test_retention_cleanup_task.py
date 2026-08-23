"""``cleanup_expired_files`` — the hourly task that DELETES user media, previously untested.

Three defects made a broken retention job indistinguishable from a working one:
a catch-all that returned ``{"status": "error"}`` (Celery records that as SUCCESS),
a ``run_time`` parse that raised straight into it, and a forced run that claimed
the day's slot and suppressed the scheduled pass.

Every test drives the task FUNCTION BODY (``.run``) against the savepoint-rolled-back
``db_session``, with a 100-year retention window so no real file is ever eligible.
"""

import contextlib
from datetime import UTC
from datetime import datetime

import pytest

from app.services import system_settings_service
from app.tasks import cleanup

pytestmark = pytest.mark.xdist_group("retention_system_settings")

#: A retention window nothing in any dev database can fall outside of.
_UNREACHABLE_RETENTION_DAYS = 36500


#: 02:30 UTC — inside the default 02:00 retention hour, so the scheduled-hour
#: guard is exercised deterministically instead of only passing between 02:00
#: and 03:00 UTC.
_FIXED_NOW = datetime(2026, 8, 12, 2, 30, tzinfo=UTC)


class _FrozenDatetime(datetime):
    """``datetime`` whose ``now()`` is :data:`_FIXED_NOW`, in any timezone."""

    @classmethod
    def now(cls, tz=None):  # noqa: D102 - inherited contract
        return _FIXED_NOW.astimezone(tz) if tz is not None else _FIXED_NOW


def _config(**overrides):
    """A retention config dict shaped like ``get_retention_config``'s return."""
    config = {
        "retention_enabled": True,
        "retention_days": _UNREACHABLE_RETENTION_DAYS,
        "delete_error_files": False,
        "run_time": "02:00",
        "timezone": "UTC",
        "last_run": None,
        "last_run_deleted": 0,
    }
    config.update(overrides)
    return config


@pytest.fixture
def retention_env(db_session, monkeypatch):
    """Point the task's session and config reader at the test transaction.

    Returns:
        A callable taking the config overrides the task should read.
    """
    monkeypatch.setattr(
        cleanup, "session_scope", lambda: contextlib.nullcontext(db_session), raising=True
    )
    monkeypatch.setattr(cleanup, "datetime", _FrozenDatetime, raising=True)

    def _configure(**overrides):
        config = _config(**overrides)
        monkeypatch.setattr(
            system_settings_service, "get_retention_config", lambda db: config, raising=True
        )
        return config

    return _configure


def test_unexpected_failure_is_reported_as_a_task_failure(retention_env, monkeypatch):
    """Defect: ``except Exception -> {"status": "error"}``, which Celery records as SUCCESS.

    A retention job broken by an unreadable settings row or an unreachable MinIO
    looked healthy on every one of its hourly runs. The exception must propagate.
    """
    retention_env()

    def _explode(db):
        raise RuntimeError("settings table is unreadable")

    monkeypatch.setattr(system_settings_service, "get_retention_config", _explode, raising=True)

    with pytest.raises(RuntimeError, match="settings table is unreadable"):
        cleanup.cleanup_expired_files.run(force=True)


def test_malformed_run_time_parses_to_the_documented_default():
    """Defect: ``int(config["run_time"].split(":")[0])`` raised on any malformed value.

    That exception landed in the catch-all above, so a single bad settings row
    stopped retention permanently while reporting success. An unparseable schedule
    now falls back to the coded default hour.
    """
    assert cleanup._scheduled_retention_hour("not-a-time") == cleanup._DEFAULT_RETENTION_HOUR
    assert cleanup._scheduled_retention_hour(None) == cleanup._DEFAULT_RETENTION_HOUR
    assert cleanup._scheduled_retention_hour("99:00") == cleanup._DEFAULT_RETENTION_HOUR
    assert cleanup._scheduled_retention_hour("05:30") == 5


def test_malformed_run_time_still_lets_the_pass_run(retention_env):
    """Defect (whole-task view): a malformed ``run_time`` aborted the run entirely.

    With the fallback hour reached, the pass completes normally instead of
    returning an error status that nothing was watching.
    """
    retention_env(run_time="garbage")

    result = cleanup.cleanup_expired_files.run()

    assert result["status"] == "completed"
    assert result["deleted"] == 0


def test_forced_run_does_not_claim_the_scheduled_slot(retention_env, monkeypatch):
    """Defect: a forced run stamped ``files.retention_last_run``.

    That field is exactly what the already-ran-today guard reads, so an admin
    pressing "run now" cancelled the day's scheduled pass — the one that honours
    the configured window and error-file policy.
    """
    retention_env()
    written: list[str] = []
    monkeypatch.setattr(
        system_settings_service,
        "set_setting",
        lambda db, key, value, *args, **kwargs: written.append(key),
        raising=True,
    )

    result = cleanup.cleanup_expired_files.run(force=True)

    assert result["status"] == "completed"
    assert written == ["files.retention_last_run_deleted"]


def test_scheduled_run_does_claim_the_slot(retention_env, monkeypatch):
    """Control: the scheduled pass must still record itself, or it repeats hourly.

    Same code path as the test above with ``force=False``: the stamp is written,
    proving the exclusion is conditional rather than a removed feature.
    """
    retention_env(run_time="02:00")
    written: list[str] = []
    monkeypatch.setattr(
        system_settings_service,
        "set_setting",
        lambda db, key, value, *args, **kwargs: written.append(key),
        raising=True,
    )

    result = cleanup.cleanup_expired_files.run()

    assert result["status"] == "completed"
    assert written == ["files.retention_last_run", "files.retention_last_run_deleted"]


def test_retention_disabled_short_circuits(retention_env):
    """Control: the enabled flag still wins over everything else.

    Guards the guard order — a refactor that moved the run_time parse ahead of
    the enabled check would run retention on a deployment that switched it off.
    """
    retention_env(retention_enabled=False, run_time="garbage")

    result = cleanup.cleanup_expired_files.run()

    assert result["status"] == "disabled"
