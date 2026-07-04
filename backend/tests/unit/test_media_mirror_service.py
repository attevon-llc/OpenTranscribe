"""Unit tests for the media mirror settings service + run bookkeeping (issue #242).

Settings round-trips run against the real DB inside the savepoint-rolled-back
``db_session`` fixture (same pattern as ``test_backup_service``); the metrics
projection tests exercise ``update_media_mirror_metrics`` end-to-end against the
process-global Prometheus registry with delta-based assertions.
"""

from __future__ import annotations

import json
from datetime import datetime
from datetime import timezone

import pytest
from prometheus_client import REGISTRY

from app.core import constants as C  # noqa: N812
from app.core.backup_metrics import update_media_mirror_metrics
from app.services import media_mirror_service as mm
from app.services import system_settings_service as sss

# All tests here upsert the same backup.mirror_* SystemSettings keys — run them on
# one xdist worker to avoid unique-index deadlocks during parallel execution (the
# same pattern as test_auth_config_integration.py's shared-table group).
pytestmark = pytest.mark.xdist_group("media_mirror_system_settings")


def _clear_mirror_keys(db_session) -> None:
    from app.models.system_settings import SystemSettings

    db_session.query(SystemSettings).filter(SystemSettings.key.like("backup.mirror_%")).delete(
        synchronize_session=False
    )


def _sample(name: str, labels: dict | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


# =============================================================================
# Settings round-trip
# =============================================================================
def test_get_settings_returns_coded_defaults(db_session):
    _clear_mirror_keys(db_session)
    cfg = mm.get_settings(db_session)
    assert cfg["enabled"] is C.DEFAULT_BACKUP_MIRROR_ENABLED is False  # default-OFF feature
    assert cfg["schedule"] == C.DEFAULT_BACKUP_MIRROR_SCHEDULE
    assert cfg["destination_type"] == C.DEFAULT_BACKUP_MIRROR_DESTINATION_TYPE == "local"
    assert cfg["destination"] == C.DEFAULT_BACKUP_MIRROR_DESTINATION == "/media-mirror"
    assert cfg["throttle_ms"] == C.DEFAULT_BACKUP_MIRROR_THROTTLE_MS
    assert cfg["s3_bucket"] == ""
    assert cfg["s3_secret_key_set"] is False
    assert cfg["last_run_at"] is None
    assert cfg["last_result"] is None


def test_update_settings_roundtrip(db_session):
    out = mm.update_settings(
        db_session,
        enabled=True,
        schedule="15 2 * * *",
        destination_type="local",
        destination="/mnt/mirror",
        throttle_ms=50,
    )
    assert out["enabled"] is True
    assert out["schedule"] == "15 2 * * *"
    assert out["destination"] == "/mnt/mirror"
    assert out["throttle_ms"] == 50
    # Fresh fetch confirms persistence.
    assert mm.get_settings(db_session)["schedule"] == "15 2 * * *"


def test_update_settings_rejects_bad_cron(db_session):
    with pytest.raises(ValueError, match="Invalid cron"):
        mm.update_settings(db_session, schedule="not a cron")


def test_update_settings_rejects_bad_destination_type(db_session):
    with pytest.raises(ValueError, match="destination_type"):
        mm.update_settings(db_session, destination_type="ftp")


def test_update_settings_rejects_negative_throttle(db_session):
    with pytest.raises(ValueError, match="throttle_ms"):
        mm.update_settings(db_session, throttle_ms=-5)


def test_partial_update_leaves_other_fields(db_session):
    mm.update_settings(db_session, enabled=True, throttle_ms=100)
    mm.update_settings(db_session, throttle_ms=25)
    cfg = mm.get_settings(db_session)
    assert cfg["enabled"] is True  # untouched
    assert cfg["throttle_ms"] == 25


def test_mirror_settings_are_separate_from_backup_settings(db_session):
    # The mirror destination is deliberately independent of the DB-dump destination.
    from app.services import backup_service as bs

    mm.update_settings(db_session, destination="/mnt/mirror-only", destination_type="local")
    assert bs.get_settings(db_session)["destination"] != "/mnt/mirror-only"


def test_s3_secret_is_encrypted_and_never_echoed(db_session):
    mm.update_settings(db_session, s3_secret_key="mirror-secret-value")
    cfg = mm.get_settings(db_session)
    assert "s3_secret_key" not in cfg
    assert cfg["s3_secret_key_set"] is True
    raw = sss.get_setting(db_session, mm.KEY_S3_SECRET_KEY)
    assert raw is not None
    assert raw != "mirror-secret-value"  # encrypted at rest
    assert mm.get_s3_secret_key(db_session) == "mirror-secret-value"


def test_s3_secret_clearable_with_empty_string(db_session):
    mm.update_settings(db_session, s3_secret_key="abc123")
    assert mm.get_settings(db_session)["s3_secret_key_set"] is True
    mm.update_settings(db_session, s3_secret_key="")
    assert mm.get_settings(db_session)["s3_secret_key_set"] is False


def test_s3_status_without_bucket(db_session):
    _clear_mirror_keys(db_session)
    status = mm.s3_bucket_status(mm.get_settings(db_session), db_session)
    assert status["reachable"] is False
    assert "bucket" in (status["error"] or "").lower()


def test_test_s3_connection_without_bucket(db_session):
    _clear_mirror_keys(db_session)
    out = mm.test_s3_connection(mm.get_settings(db_session), db_session)
    assert out["ok"] is False


# =============================================================================
# Run-result bookkeeping (the #244 pattern)
# =============================================================================
def test_record_result_success_bookkeeping(db_session):
    _clear_mirror_keys(db_session)
    now_iso = datetime.now(timezone.utc).isoformat()
    result = {
        "ok": True,
        "status": "success",
        "objects_scanned": 10,
        "objects_copied": 3,
        "objects_skipped": 7,
        "objects_failed": 0,
        "objects_excluded": 0,
        "bytes_copied": 300,
    }
    mm.record_result(db_session, now_iso, result)

    cfg = mm.get_settings(db_session)
    assert cfg["last_run_at"] == now_iso
    assert cfg["last_result"]["objects_copied"] == 3
    assert sss.get_setting_int(db_session, mm.KEY_RUNS_SUCCESS, 0) == 1
    assert sss.get_setting_int(db_session, mm.KEY_RUNS_FAILURE, 0) == 0
    assert sss.get_setting(db_session, mm.KEY_LAST_SUCCESS_AT) == now_iso


def test_record_result_failure_bookkeeping(db_session):
    _clear_mirror_keys(db_session)
    now_iso = datetime.now(timezone.utc).isoformat()
    mm.record_result(db_session, now_iso, {"ok": False, "status": "error", "error": "boom"})

    assert sss.get_setting_int(db_session, mm.KEY_RUNS_FAILURE, 0) == 1
    assert sss.get_setting_int(db_session, mm.KEY_RUNS_SUCCESS, 0) == 0
    # A failure must NOT advance the last-success timestamp.
    assert sss.get_setting(db_session, mm.KEY_LAST_SUCCESS_AT) is None
    assert mm.get_settings(db_session)["last_result"]["error"] == "boom"


def test_perform_mirror_noop_when_destination_missing(db_session):
    # No writable /media-mirror mount in the test env → graceful no_destination result.
    from app.services import media_mirror_engine as eng

    _clear_mirror_keys(db_session)
    mm.update_settings(db_session, destination="/nonexistent/media-mirror/zzz")
    result = eng.perform_mirror(db_session)
    assert result["ok"] is False
    assert result["status"] == "no_destination"
    cfg = mm.get_settings(db_session)
    assert cfg["last_result"]["status"] == "no_destination"
    assert cfg["last_run_at"] is not None


# =============================================================================
# Scrape-time metrics projection
# =============================================================================
def test_mirror_metrics_project_persisted_state(db_session):
    ts = datetime(2026, 7, 1, 4, 0, tzinfo=timezone.utc)
    base_success = _sample("media_mirror_runs_total", {"result": "success"})
    base_failure = _sample("media_mirror_runs_total", {"result": "failure"})

    sss.set_setting(db_session, mm.KEY_LAST_SUCCESS_AT, ts.isoformat(), "test")
    sss.set_setting(
        db_session,
        mm.KEY_LAST_RESULT,
        json.dumps(
            {
                "ok": True,
                "objects_copied": 4,
                "objects_skipped": 11,
                "objects_failed": 2,
                "objects_excluded": 1,
            }
        ),
        "test",
    )
    sss.set_setting(db_session, mm.KEY_RUNS_SUCCESS, int(base_success) + 3, "test")
    sss.set_setting(db_session, mm.KEY_RUNS_FAILURE, int(base_failure) + 1, "test")

    update_media_mirror_metrics(db_session)

    assert _sample("media_mirror_last_success_timestamp_seconds") == ts.timestamp()
    assert _sample("media_mirror_last_status") == 1.0
    assert _sample("media_mirror_last_run_objects", {"outcome": "copied"}) == 4.0
    assert _sample("media_mirror_last_run_objects", {"outcome": "skipped"}) == 11.0
    assert _sample("media_mirror_last_run_objects", {"outcome": "failed"}) == 2.0
    assert _sample("media_mirror_last_run_objects", {"outcome": "excluded"}) == 1.0
    assert _sample("media_mirror_runs_total", {"result": "success"}) == int(base_success) + 3
    assert _sample("media_mirror_runs_total", {"result": "failure"}) == int(base_failure) + 1


def test_mirror_metrics_counters_stay_monotonic(db_session):
    current = _sample("media_mirror_runs_total", {"result": "success"})
    sss.set_setting(db_session, mm.KEY_RUNS_SUCCESS, 0, "test")
    update_media_mirror_metrics(db_session)
    assert _sample("media_mirror_runs_total", {"result": "success"}) == current


def test_mirror_metrics_survive_bad_persisted_values(db_session):
    before = _sample("media_mirror_last_status")
    sss.set_setting(db_session, mm.KEY_LAST_RESULT, "not-json", "test")
    sss.set_setting(db_session, mm.KEY_LAST_SUCCESS_AT, "not-a-date", "test")
    sss.set_setting(db_session, mm.KEY_RUNS_SUCCESS, "NaNsense", "test")

    update_media_mirror_metrics(db_session)  # must not raise

    assert _sample("media_mirror_last_status") == before


def test_failed_run_lands_in_metrics_end_to_end(db_session):
    """no_destination run → failure counter + last_status 0 at scrape time."""
    from app.services import media_mirror_engine as eng

    _clear_mirror_keys(db_session)
    # Earlier tests in this worker may have advanced the process-global registry
    # past the (savepoint-fresh) DB counter; sync the DB baseline to the registry
    # sample so the monotonic guard can't swallow this run's increment.
    base_failure = _sample("media_mirror_runs_total", {"result": "failure"})
    sss.set_setting(db_session, mm.KEY_RUNS_FAILURE, int(base_failure), "test baseline")

    mm.update_settings(db_session, destination="/nonexistent/media-mirror/zzz")
    result = eng.perform_mirror(db_session)
    assert result["ok"] is False

    update_media_mirror_metrics(db_session)

    assert _sample("media_mirror_runs_total", {"result": "failure"}) >= base_failure + 1
    assert _sample("media_mirror_last_status") == 0.0
