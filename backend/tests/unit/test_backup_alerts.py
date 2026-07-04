"""Unit tests for backup admin alerting (issue #244) + the one-time keys notice (#243).

The WebSocket boundary (``send_task_notification``) is mocked. Recipient tests take the
``admin_user`` fixture so at least one admin exists even on a fresh CI database — they
must never assume the dev stack's seeded admin. Counts are always recomputed from
``_admin_user_ids`` on the same savepoint session, so extra ambient admins are fine.
"""

from __future__ import annotations

from unittest import mock

from app.services import backup_alerts
from app.services import backup_service as bs
from app.services import system_settings_service as sss


def _clear_backup_keys(db_session):
    from app.models.system_settings import SystemSettings

    db_session.query(SystemSettings).filter(SystemSettings.key.like("backup.%")).delete(
        synchronize_session=False
    )


# =============================================================================
# collect_warnings — pure classification of non-fatal issues
# =============================================================================
def test_collect_warnings_empty_for_clean_success():
    assert backup_alerts.collect_warnings({"ok": True, "status": "success"}) == []


def test_collect_warnings_ignores_skipped_opensearch():
    result = {"ok": True, "opensearch": {"status": "skipped", "error": "unreachable"}}
    assert backup_alerts.collect_warnings(result) == []


def test_collect_warnings_flags_prune_snapshot_and_recovery():
    result = {
        "ok": True,
        "prune_error": "disk gone",
        "opensearch": {"status": "error", "error": "snapshot timeout"},
        "recovery": {"status": "error", "error": "read-only fs"},
    }
    warnings = backup_alerts.collect_warnings(result)
    assert len(warnings) == 3
    joined = "; ".join(warnings)
    assert "disk gone" in joined
    assert "snapshot timeout" in joined
    assert "read-only fs" in joined


# =============================================================================
# notify_backup_result — failure / warning / silence
# =============================================================================
def test_failure_sends_admin_notification(db_session, admin_user):
    admin_ids = backup_alerts._admin_user_ids(db_session)
    assert admin_user.id in admin_ids

    with mock.patch("app.services.notification_service.send_task_notification") as sender:
        backup_alerts.notify_backup_result(
            db_session, {"ok": False, "status": "error", "error": "pg_dump exit 1"}
        )

    assert sender.call_count == len(admin_ids)
    called_ids = {call.args[0] for call in sender.call_args_list}
    assert called_ids == set(admin_ids)
    for call in sender.call_args_list:
        assert call.args[1] == backup_alerts.EVENT_TYPE
        assert call.kwargs["status"] == "failed"
        assert "pg_dump exit 1" in call.kwargs["message"]


def test_clean_success_sends_nothing(db_session):
    _clear_backup_keys(db_session)
    result = {"ok": True, "status": "success", "recovery": {"status": "keys_included"}}
    with mock.patch("app.services.notification_service.send_task_notification") as sender:
        backup_alerts.notify_backup_result(db_session, result)
    sender.assert_not_called()


def test_success_with_warnings_notifies(db_session, admin_user):
    _clear_backup_keys(db_session)
    result = {
        "ok": True,
        "status": "success",
        "prune_error": "disk gone",
        "recovery": {"status": "keys_included"},
    }
    with mock.patch("app.services.notification_service.send_task_notification") as sender:
        backup_alerts.notify_backup_result(db_session, result)
    assert sender.call_count >= 1
    assert sender.call_args.kwargs["status"] == "warning"
    assert "disk gone" in sender.call_args.kwargs["message"]


def test_failure_alerting_never_raises(db_session, admin_user):
    # Even if the notification layer explodes, the backup run must not. Needs a
    # real recipient or the raising mock is never called (vacuous on a fresh DB).
    with mock.patch(
        "app.services.notification_service.send_task_notification",
        side_effect=RuntimeError("redis down"),
    ):
        backup_alerts.notify_backup_result(db_session, {"ok": False, "error": "x"})


# =============================================================================
# One-time "backups exclude your encryption keys" notice (#243)
# =============================================================================
def test_keys_notice_sent_once(db_session, admin_user):
    _clear_backup_keys(db_session)
    result = {"ok": True, "status": "success", "recovery": {"status": "readme_written"}}

    with mock.patch("app.services.notification_service.send_task_notification") as sender:
        backup_alerts.notify_backup_result(db_session, result)
        first_count = sender.call_count
        backup_alerts.notify_backup_result(db_session, result)

    admin_count = len(backup_alerts._admin_user_ids(db_session))
    assert first_count == admin_count
    # Second run: flag is set, no repeat notice.
    assert sender.call_count == first_count
    assert "encryption keys" in sender.call_args.kwargs["message"]
    assert sss.get_setting_bool(db_session, bs.KEY_RECOVERY_NOTICE_SENT) is True


def test_keys_notice_not_sent_when_keys_included(db_session):
    _clear_backup_keys(db_session)
    result = {"ok": True, "status": "success", "recovery": {"status": "keys_included"}}
    with mock.patch("app.services.notification_service.send_task_notification") as sender:
        backup_alerts.notify_backup_result(db_session, result)
    sender.assert_not_called()
    assert sss.get_setting_bool(db_session, bs.KEY_RECOVERY_NOTICE_SENT) is False
