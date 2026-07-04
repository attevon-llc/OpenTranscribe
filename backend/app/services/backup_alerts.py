"""Proactive admin alerting for scheduled-backup outcomes (issue #244).

A silently failing backup is worse than none — it manufactures false confidence. This
module closes the loop after every recorded run (``backup_service._record_result``):

- **failure** → a ``backup_status`` WebSocket notification to every admin, carrying the
  persisted error message (the admin Backups page shows the same ``last_result``).
- **success with warnings** (retention prune failed, OpenSearch snapshot failed, or the
  recovery companion could not be written) → a warning notification; the dump itself
  succeeded so the run is NOT marked failed.
- **one-time keys notice** (issue #243): the first successful run that lands *without*
  key material (encryption off → README only) warns admins that the dumps alone are not
  restorable. Gated by the ``backup.recovery_notice_sent`` flag so it fires once.

Everything here is best-effort and never raises — alerting must not break the backup.
Prometheus surfacing lives in ``app.core.backup_metrics`` (scrape-time, DB-backed).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# WebSocket event type consumed by the frontend notification store.
EVENT_TYPE = "backup_status"


def _admin_user_ids(db: Session) -> list[int]:
    """Return ids of all admin/super_admin users (alert recipients)."""
    from app.models.user import User

    rows = db.query(User.id).filter(User.role.in_(("admin", "super_admin"))).all()
    return [row[0] for row in rows]


def _notify_admins(db: Session, *, status: str, message: str) -> None:
    """Fan a task notification out to every admin (best-effort)."""
    from app.services.notification_service import send_task_notification

    for user_id in _admin_user_ids(db):
        send_task_notification(user_id, EVENT_TYPE, status=status, message=message)


def collect_warnings(result: dict[str, Any]) -> list[str]:
    """Extract non-fatal warnings from a successful run's result dict."""
    warnings: list[str] = []
    if result.get("prune_error"):
        warnings.append(f"retention pruning failed: {result['prune_error']}")
    opensearch = result.get("opensearch") or {}
    if opensearch.get("status") in ("error", "unsupported"):
        warnings.append(f"OpenSearch snapshot failed: {opensearch.get('error', 'unknown error')}")
    recovery = result.get("recovery") or {}
    if recovery.get("status") == "error":
        warnings.append(f"recovery key companion failed: {recovery.get('error', 'unknown error')}")
    return warnings


def _maybe_send_keys_notice(db: Session, result: dict[str, Any]) -> None:
    """One-time admin warning that unencrypted backups exclude the encryption keys."""
    from app.services import backup_service
    from app.services import system_settings_service as sss

    if (result.get("recovery") or {}).get("status") != "readme_written":
        return
    if sss.get_setting_bool(db, backup_service.KEY_RECOVERY_NOTICE_SENT, False):
        return
    _notify_admins(
        db,
        status="warning",
        message=(
            "Backups do not include your encryption keys (backup encryption is off). "
            "A restored dump is unrecoverable without ENCRYPTION_KEY and JWT_SECRET_KEY "
            "from .env — store them in a password manager, or enable backup encryption "
            "to include them beside the dumps. See RECOVERY-README.txt in the backup "
            "destination."
        ),
    )
    sss.set_setting(
        db,
        backup_service.KEY_RECOVERY_NOTICE_SENT,
        True,
        "One-time 'backups exclude encryption keys' admin notice was sent",
    )


def notify_backup_result(db: Session, result: dict[str, Any]) -> None:
    """Send admin notifications for one recorded backup run. Never raises."""
    try:
        if not result.get("ok"):
            _notify_admins(
                db,
                status="failed",
                message=f"Scheduled database backup failed: {result.get('error', 'unknown error')}",
            )
            return
        warnings = collect_warnings(result)
        if warnings:
            _notify_admins(
                db,
                status="warning",
                message="Database backup succeeded with warnings: " + "; ".join(warnings),
            )
        _maybe_send_keys_notice(db, result)
    except Exception as exc:  # noqa: BLE001 - alerting must never break the backup run
        logger.error("Backup alerting failed: %s", exc)
