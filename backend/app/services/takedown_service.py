"""Abuse / DMCA / safe-harbor takedown service — the one place quarantine lives.

This is the enforcement mechanism behind the abuse-intake / DMCA policy (see
``docs/abuse-and-takedown.md``). A **quarantine** (takedown) hides a ``MediaFile``
from every read surface for non-admins while keeping the row, the media, and the
transcript intact (so the action is reversible and leaves an audit + appeal
trail). It is deliberately INDEPENDENT of the processing ``status`` so a fully
``COMPLETED`` file can be taken down and later released back to exactly its prior
state. Note: toxicity/PII redaction masks *text* — it is NOT a takedown; this
service is the takedown.

Four things live here so they can never drift apart:
  * :func:`exclude_quarantined` — the SQL predicate that drops quarantined rows
    from list/gallery/search queries (skipped for admin "see all").
  * :func:`is_hidden_for` — the per-request access gate used by the resource
    lookup choke-point (``get_file_by_uuid_with_permission``): a quarantined
    file 404s for non-admins, stays visible to admins for review.
  * :func:`quarantine_file` / :func:`release_file` — the admin actions, each of
    which writes the DB state, sets the S3 legal-hold (best-effort), and audits.
  * The DMCA §512(g) owner notices (:func:`_notify_owner_takedown` /
    :func:`_notify_owner_release`) — since the file stays hidden (404) from its
    owner while quarantined, a persistent in-app notification is the owner's
    only surface for learning about the takedown and how to counter-notice it.

Community-edition invariance: nothing quarantines automatically, the columns
default to the not-quarantined state, and ``exclude_quarantined``/``is_hidden_for``
are behavior-preserving no-ops for files that were never taken down.
"""

import logging
from datetime import UTC
from datetime import datetime

from sqlalchemy.orm import Query
from sqlalchemy.orm import Session

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.user import User

logger = logging.getLogger(__name__)

# WebSocket event types consumed by the frontend notification store (owner notices).
OWNER_TAKEDOWN_EVENT = "file_takedown"
OWNER_RELEASE_EVENT = "file_takedown_released"


def exclude_quarantined(query: Query, *, include_quarantined: bool = False) -> Query:
    """Drop quarantined (taken-down) files from a ``MediaFile`` query.

    The list/gallery/search read paths call this so a taken-down file never
    appears for a normal user. ``include_quarantined=True`` (used by the admin
    "see all" branch and the dedicated review list) leaves the query untouched.

    Args:
        query: A query selecting from / filtering ``MediaFile``.
        include_quarantined: When True, do not add the exclusion (admin view).

    Returns:
        The query, optionally with ``is_quarantined IS NOT TRUE`` applied.
    """
    if include_quarantined:
        return query
    # ``is_(False)`` (rather than ``!= True``) keeps the partial index usable and
    # is correct because the column is NOT NULL with a false default.
    return query.filter(MediaFile.is_quarantined.is_(False))


def is_hidden_for(file: MediaFile, *, is_admin: bool) -> bool:
    """Whether ``file`` must be treated as non-existent for this caller.

    A quarantined file is hidden from everyone EXCEPT admins (who review/release
    it). The per-resource lookup helper turns a True here into a 404 so a
    taken-down file is indistinguishable from a missing one for normal users.

    Args:
        file: The resolved media file.
        is_admin: Whether the caller has admin (review) privileges.

    Returns:
        True if the file is quarantined and the caller is not an admin.
    """
    return bool(getattr(file, "is_quarantined", False)) and not is_admin


def quarantine_file(
    db: Session,
    file: MediaFile,
    *,
    admin: User,
    reason: str,
    legal_hold: bool = True,
    source_ip: str = "",
    user_agent: str = "",
) -> MediaFile:
    """Take a file down: mark it quarantined, set the legal-hold, and audit.

    Idempotent — re-quarantining an already-held file just refreshes the reason
    and timestamp. The processing ``status`` is overwritten with
    ``QUARANTINED`` for display, but the authoritative ``is_quarantined`` flag is
    what gates access; ``release_file`` restores the file to a servable state.
    The owner gets a best-effort §512(g) notice (:func:`_notify_owner_takedown`).

    Args:
        db: Database session.
        file: The media file to take down.
        admin: The admin performing the takedown (for ``quarantined_by`` + audit).
        reason: Free-text takedown reason (DMCA notice ref, AUP clause, etc.).
        legal_hold: Also place a legal-hold on the row + S3 object (default True).
        source_ip / user_agent: Request metadata for the audit event.

    Returns:
        The updated (committed) media file.
    """
    file.is_quarantined = True
    file.quarantine_reason = reason
    file.quarantined_at = datetime.now(UTC)
    file.quarantined_by = admin.id
    # Preserve the true prior status ONCE (re-quarantine must not overwrite it
    # with QUARANTINED) so release can restore the file's actual state.
    if file.status != FileStatus.QUARANTINED:
        file.pre_quarantine_status = (
            file.status.value if hasattr(file.status, "value") else str(file.status)
        )
    file.status = FileStatus.QUARANTINED
    if legal_hold:
        file.legal_hold = True

    db.commit()
    db.refresh(file)

    # Best-effort storage legal-hold (DB flag is the source of truth).
    if legal_hold and file.storage_path:
        try:
            from app.services.minio_service import set_object_legal_hold

            set_object_legal_hold(str(file.storage_path), True)
        except Exception as e:  # noqa: BLE001 — advisory; never break the takedown
            logger.warning(f"Storage legal-hold enable failed for file {file.id}: {e}")

    _audit(
        AuditEventType.ADMIN_FILE_QUARANTINE,
        admin=admin,
        file=file,
        source_ip=source_ip,
        user_agent=user_agent,
        extra={"reason": reason, "legal_hold": bool(legal_hold)},
    )
    logger.info(f"File {file.id} ({file.uuid}) quarantined by admin {admin.id}: {reason}")
    _notify_owner_takedown(file, reason=reason)
    return file


def release_file(
    db: Session,
    file: MediaFile,
    *,
    admin: User,
    restore_status: FileStatus | None = None,
    clear_legal_hold: bool = True,
    source_ip: str = "",
    user_agent: str = "",
) -> MediaFile:
    """Release a quarantined file: clear the flag, optionally lift the hold, audit.

    Restores access for normal users. The display status goes back to the
    file's recorded ``pre_quarantine_status`` (captured at takedown time);
    ``restore_status`` overrides it when the caller knows better, and
    ``COMPLETED`` is the last-resort fallback for rows quarantined before the
    prior status was recorded (pre-v371). The owner gets a best-effort
    access-restored notice (:func:`_notify_owner_release`).

    Args:
        db: Database session.
        file: The quarantined media file.
        admin: The admin performing the release (for the audit trail).
        restore_status: Explicit status override (default: the recorded prior status).
        clear_legal_hold: Also lift the DB + S3 legal-hold (default True).
        source_ip / user_agent: Request metadata for the audit event.

    Returns:
        The updated (committed) media file.
    """
    file.is_quarantined = False
    file.quarantine_reason = None
    file.quarantined_at = None
    file.quarantined_by = None
    if file.status == FileStatus.QUARANTINED:
        if restore_status is not None:
            file.status = restore_status
        else:
            try:
                file.status = FileStatus(file.pre_quarantine_status)
            except ValueError:
                file.status = FileStatus.COMPLETED
    file.pre_quarantine_status = None
    if clear_legal_hold:
        file.legal_hold = False

    db.commit()
    db.refresh(file)

    if clear_legal_hold and file.storage_path:
        try:
            from app.services.minio_service import set_object_legal_hold

            set_object_legal_hold(str(file.storage_path), False)
        except Exception as e:  # noqa: BLE001 — advisory; never break the release
            logger.warning(f"Storage legal-hold disable failed for file {file.id}: {e}")

    _audit(
        AuditEventType.ADMIN_FILE_RELEASE,
        admin=admin,
        file=file,
        source_ip=source_ip,
        user_agent=user_agent,
        extra={"cleared_legal_hold": bool(clear_legal_hold)},
    )
    logger.info(f"File {file.id} ({file.uuid}) released from quarantine by admin {admin.id}")
    _notify_owner_release(file)

    # Lifting a hold can UNBLOCK a GDPR erasure (issue #442). An Art. 17 request that
    # hit this file was deferred, not abandoned — ``_purge_files`` skipped it under
    # Art. 17(3)(e) and, because ``media_file.user_id`` is a NO ACTION FK, the whole
    # account survived with it. Until this call existed, releasing the hold ended the
    # only justification for that retention and nothing noticed: the deferral became
    # permanent non-compliance. Dispatch only — the sweep decides which entries are now
    # finishable, and it runs on a schedule regardless, so a failed dispatch delays the
    # completion rather than losing it.
    if clear_legal_hold:
        from app.tasks.erasure_reconciliation import notify_hold_released

        notify_hold_released(file)
    return file


def _notify_owner_takedown(file: MediaFile, *, reason: str) -> None:
    """Send the file OWNER the DMCA §512(g) takedown notice. Never raises.

    The quarantined file 404s for the owner on every read surface (by design),
    so this persistent in-app notification is the owner's only in-product way
    to learn a takedown happened and how to dispute it. It carries the
    admin-recorded reason and the deployment's abuse-contact address for a
    counter-notice — and deliberately NOT the acting admin's identity.
    """
    try:
        from app.core.config import settings
        from app.services.notification_service import send_task_notification

        display_name = file.title or file.filename
        contact = settings.ABUSE_CONTACT_EMAIL or ""
        if contact:
            dispute = (
                f"To dispute it, email a counter-notice to {contact} "
                f"referencing file ID {file.uuid}."
            )
        else:
            dispute = (
                "To dispute it, contact your service operator with a "
                f"counter-notice referencing file ID {file.uuid}."
            )
        send_task_notification(
            int(file.user_id),
            OWNER_TAKEDOWN_EVENT,
            status="warning",
            message=(
                f'Your file "{display_name}" has been taken down by the service '
                f"operator following an abuse/copyright report. Reason: {reason}. {dispute}"
            ),
            extra={
                "file_uuid": str(file.uuid),
                "filename": display_name,
                "reason": reason,
                "abuse_contact_email": contact,
            },
        )
    except Exception as e:  # noqa: BLE001 — the notice must never break the takedown
        logger.warning(f"Owner takedown notification failed for file {file.id}: {e}")


def _notify_owner_release(file: MediaFile) -> None:
    """Notify the file OWNER that a taken-down file was restored. Never raises.

    Counterpart of :func:`_notify_owner_takedown` — sent after a successful
    counter-notice / withdrawn complaint, when an admin releases the file.
    ``file_id`` (unlike the takedown notice) is included so the notification
    panel can link to the now-accessible file.
    """
    try:
        from app.services.notification_service import send_task_notification

        display_name = file.title or file.filename
        send_task_notification(
            int(file.user_id),
            OWNER_RELEASE_EVENT,
            status="completed",
            message=f'Your file "{display_name}" has been restored and is accessible again.',
            extra={
                "file_id": str(file.uuid),
                "file_uuid": str(file.uuid),
                "filename": display_name,
            },
        )
    except Exception as e:  # noqa: BLE001 — the notice must never break the release
        logger.warning(f"Owner release notification failed for file {file.id}: {e}")


def _audit(
    event_type: AuditEventType,
    *,
    admin: User,
    file: MediaFile,
    source_ip: str,
    user_agent: str,
    extra: dict,
) -> None:
    """Write a takedown/release audit event (never raises)."""
    details = {"file_id": file.id, "file_uuid": str(file.uuid), **extra}
    try:
        audit_logger.log(
            event_type=event_type,
            outcome=AuditOutcome.SUCCESS,
            user_id=int(admin.id),
            username=str(admin.email),
            source_ip=source_ip or None,
            user_agent=user_agent or None,
            details=details,
            # Org attribution (issue #262a): a takedown of an org-stamped file
            # is that tenant's event — its org admins see it in the audit read.
            organization_id=int(file.organization_id) if file.organization_id else None,
        )
    except Exception as e:  # noqa: BLE001 — audit must never break the action
        logger.warning(f"Audit log for {event_type} failed: {e}")
