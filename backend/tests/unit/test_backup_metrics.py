"""Unit tests for the scrape-time backup metric refresh (issue #244).

``update_backup_metrics`` projects DB-persisted run state (SystemSettings) onto the
Prometheus collectors — these tests seed the settings rows within the savepoint and
assert the collector samples. The registry is process-global, so counter assertions
are delta-based (other tests in the same worker may have advanced them).
"""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime

import pytest
from prometheus_client import REGISTRY

from app.core.backup_metrics import update_backup_metrics
from app.services import backup_service as bs
from app.services import system_settings_service as sss

# This file, test_backup_service.py, and test_backup_alerts.py all upsert the same
# backup.* SystemSettings keys (bs.KEY_*) with no coordination between them — under
# `-n auto` two workers inserting overlapping keys in different orders can deadlock on
# the system_settings_key_key unique index (issue #389). Same pattern/precedent as
# test_media_mirror_service.py's "media_mirror_system_settings" group (a disjoint key
# prefix, so it stays a separate group) and test_auth_config_integration.py's
# "auth_config" group.
pytestmark = pytest.mark.xdist_group("backup_system_settings")


def _sample(name: str, labels: dict | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


def test_last_success_timestamp_gauge(db_session):
    ts = datetime(2026, 7, 1, 3, 0, tzinfo=UTC)
    sss.set_setting(db_session, bs.KEY_LAST_SUCCESS_AT, ts.isoformat(), "test")

    update_backup_metrics(db_session)

    assert _sample("backup_last_success_timestamp_seconds") == ts.timestamp()


def test_last_status_gauge_tracks_last_result(db_session):
    sss.set_setting(db_session, bs.KEY_LAST_RESULT, json.dumps({"ok": True}), "test")
    update_backup_metrics(db_session)
    assert _sample("backup_last_status") == 1.0

    sss.set_setting(db_session, bs.KEY_LAST_RESULT, json.dumps({"ok": False, "error": "x"}), "test")
    update_backup_metrics(db_session)
    assert _sample("backup_last_status") == 0.0


def test_run_counters_sync_up_to_db_values(db_session):
    base_success = _sample("backup_runs_total", {"result": "success"})
    base_failure = _sample("backup_runs_total", {"result": "failure"})

    sss.set_setting(db_session, bs.KEY_RUNS_SUCCESS, int(base_success) + 3, "test")
    sss.set_setting(db_session, bs.KEY_RUNS_FAILURE, int(base_failure) + 2, "test")
    update_backup_metrics(db_session)

    assert _sample("backup_runs_total", {"result": "success"}) == int(base_success) + 3
    assert _sample("backup_runs_total", {"result": "failure"}) == int(base_failure) + 2


def test_run_counters_stay_monotonic_when_db_lower(db_session):
    # Simulate a DB reset: persisted count below the live counter must be ignored
    # (Counters can never go backwards within a process).
    current = _sample("backup_runs_total", {"result": "success"})
    sss.set_setting(db_session, bs.KEY_RUNS_SUCCESS, 0, "test")

    update_backup_metrics(db_session)

    assert _sample("backup_runs_total", {"result": "success"}) == current


def test_refresh_survives_bad_persisted_values(db_session):
    before = _sample("backup_last_status")
    sss.set_setting(db_session, bs.KEY_LAST_RESULT, "not-json", "test")
    sss.set_setting(db_session, bs.KEY_LAST_SUCCESS_AT, "not-a-date", "test")
    sss.set_setting(db_session, bs.KEY_RUNS_SUCCESS, "NaNsense", "test")

    update_backup_metrics(db_session)  # must not raise

    assert _sample("backup_last_status") == before


def test_perform_backup_failure_lands_in_metrics(db_session):
    """End-to-end within the process: a failed run bumps the failure counter."""
    from app.models.system_settings import SystemSettings

    db_session.query(SystemSettings).filter(SystemSettings.key.like("backup.%")).delete(
        synchronize_session=False
    )
    base_failure = _sample("backup_runs_total", {"result": "failure"})

    bs.update_settings(db_session, destination="/nonexistent/backups/zzz")
    # The registry is process-global and _sync_run_counters only ever RAISES the
    # counter up to the DB cumulative count. Earlier tests in this worker (e.g.
    # test_run_counters_sync_up_to_db_values) advance the live counter while their
    # DB writes roll back with the savepoint — so a fresh DB count of 1 could
    # never surface. Seed the persisted count at the live counter so this run's
    # +1 is observable regardless of what ran before in the process.
    sss.set_setting(db_session, bs.KEY_RUNS_FAILURE, int(base_failure), "test")
    result = bs.perform_backup(db_session)  # no_destination → ok=False
    assert result["ok"] is False

    update_backup_metrics(db_session)

    assert _sample("backup_runs_total", {"result": "failure"}) >= base_failure + 1
    assert _sample("backup_last_status") == 0.0
