"""Owner notices and audit events for takedown actions.

Split out of ``takedown_service`` (issue #1253). Both functions that notify a file's
owner are best-effort and never raise: the notice must not break the takedown. The
audit writer is likewise best-effort.

The acting administrator is never disclosed to the owner. For a takedown placed by an
organization administrator the notice names "an administrator of your organization"
and directs a counter-notice to that organization's administrators, never to the
deployment's abuse contact (the operator did not make that decision).
"""

import logging

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.core.enums import TakedownOrigin
from app.models.media import MediaFile
from app.services.takedown_authority import TakedownActor

logger = logging.getLogger(__name__)

# WebSocket event types consumed by the frontend notification store (owner notices).
OWNER_TAKEDOWN_EVENT = "file_takedown"
OWNER_RELEASE_EVENT = "file_takedown_released"


def notify_owner_takedown(
    file: MediaFile,
    *,
    reason: str,
    origin: TakedownOrigin = TakedownOrigin.PLATFORM,
    link_uuid: str | None = None,
) -> None:
    """Send the file OWNER the DMCA §512(g) takedown notice. Never raises.

    The quarantined file 404s for the owner on every read surface (by design),
    so this persistent in-app notification is the owner's only in-product way
    to learn a takedown happened and how to dispute it. It carries the
    recorded reason and a counter-notice route — and deliberately NOT the acting
    administrator's identity.

    A platform takedown routes the counter-notice to the deployment's abuse
    contact. An organization takedown names "an administrator of your
    organization" and routes it to that organization's administrators: the
    operator did not make the decision, so its abuse mailbox is the wrong place
    to dispute it, and there is no organization contact address to name.
    """
    try:
        from app.core.config import settings
        from app.services.notification_service import send_task_notification

        display_name = file.title or file.filename
        if origin is TakedownOrigin.ORGANIZATION:
            contact = ""
            by = "an administrator of your organization"
            dispute = (
                "To dispute it, send a counter-notice to your organization's "
                f"administrators referencing file ID {file.uuid}."
            )
        else:
            contact = settings.ABUSE_CONTACT_EMAIL or ""
            by = "the service operator following an abuse/copyright report"
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
                f'Your file "{display_name}" has been taken down by {by}. '
                f"Reason: {reason}. {dispute}"
            ),
            extra={
                "file_uuid": str(file.uuid),
                "filename": display_name,
                "reason": reason,
                "abuse_contact_email": contact,
                "origin": origin.value,
                "link_uuid": link_uuid,
            },
        )
    except Exception as e:  # noqa: BLE001 — the notice must never break the takedown
        logger.warning(f"Owner takedown notification failed for file {file.id}: {e}")


def notify_owner_release(file: MediaFile, *, link_uuid: str | None = None) -> None:
    """Notify the file OWNER that a taken-down file was restored. Never raises.

    Counterpart of :func:`notify_owner_takedown` — sent after a successful
    counter-notice / withdrawn complaint, when the file is released.
    ``file_id`` (unlike the takedown notice) is included so the notification
    panel can link to the now-accessible file. A release reason, if one was
    recorded, is audit-only and never echoed here.
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
                "link_uuid": link_uuid,
            },
        )
    except Exception as e:  # noqa: BLE001 — the notice must never break the release
        logger.warning(f"Owner release notification failed for file {file.id}: {e}")


def audit_takedown(
    event_type: AuditEventType,
    *,
    actor: TakedownActor,
    file: MediaFile,
    source_ip: str,
    user_agent: str,
    extra: dict,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    error_code: str | None = None,
) -> None:
    """Write a takedown/release audit event (never raises).

    Every event records ``actor_role`` and ``authority`` so "who did this, as what"
    survives the actor being demoted or deleted.

    A refused attempt (``outcome=FAILURE``) is stamped with the ACTOR's
    organization, never the file's: a cross-tenant probe must not appear in the
    victim organization's audit read, which would disclose the prober's identity to
    the tenant it probed. The file's uuid is included only when the file is in the
    actor's own tenant.
    """
    details = {
        "actor_role": actor.role_label,
        "authority": actor.authority.value,
        **extra,
    }
    refused = outcome is AuditOutcome.FAILURE
    in_own_tenant = (
        actor.authority is TakedownOrigin.PLATFORM or file.organization_id == actor.organization_id
    )
    if not refused or in_own_tenant:
        details = {"file_id": file.id, "file_uuid": str(file.uuid), **details}
    if refused and actor.authority is TakedownOrigin.ORGANIZATION:
        org_id = actor.organization_id
    else:
        org_id = int(file.organization_id) if file.organization_id else None
    try:
        audit_logger.log(
            event_type=event_type,
            outcome=outcome,
            user_id=int(actor.user.id),
            username=str(actor.user.email),
            source_ip=source_ip or None,
            user_agent=user_agent or None,
            error_code=error_code,
            details=details,
            # Org attribution (issue #262a): a takedown of an org-stamped file
            # is that tenant's event — its org admins see it in the audit read.
            organization_id=org_id,
            # issue #828: the FILE OWNER is the target of a quarantine/release
            # action. A refused cross-tenant probe has no legitimate target in the
            # actor's tenant, so it names none.
            target_user_id=int(file.user_id) if (not refused or in_own_tenant) else None,
        )
    except Exception as e:  # noqa: BLE001 — audit must never break the action
        logger.warning(f"Audit log for {event_type} failed: {e}")


def audit_unresolved_lookup(
    event_type: AuditEventType,
    *,
    actor: TakedownActor,
    error_code: str,
    source_ip: str,
    user_agent: str,
) -> None:
    """Record an organization action whose target file did not resolve in its tenant.

    A file in another organization, a personal file and a file that does not exist are
    one and the same answer to the caller, and are one and the same event here: stamped
    with the ACTOR's organization and carrying no file reference at all (the uuid is
    caller-supplied and may name someone else's data). Never raises.
    """
    try:
        audit_logger.log(
            event_type=event_type,
            outcome=AuditOutcome.FAILURE,
            user_id=int(actor.user.id),
            username=str(actor.user.email),
            source_ip=source_ip or None,
            user_agent=user_agent or None,
            error_code=error_code,
            details={"actor_role": actor.role_label, "authority": actor.authority.value},
            organization_id=actor.organization_id,
        )
    except Exception as e:  # noqa: BLE001 — audit must never break the action
        logger.warning(f"Audit log for {event_type} failed: {e}")
