"""Notices for a scheduled organization or member erasure (issue #1105).

Four moments, each sent to every organization administrator and, for a member erasure, to the
person whose data it is: the request, a reminder 24 hours before it runs, a cancellation, and the
completion. The realtime notice is a WebSocket ``erasure_notice`` frame (no file identity, so no
quarantine filter applies); an email goes out as well when a mail transport is configured.

Everything here is best effort and never raises: a notice that fails must not stop an erasure from
being scheduled, cancelled or run, and the ledger and audit log are the record, not the notice.
Self-service (``user``, ``data_subject``) requests are not covered: the person who made them is the
one who asked. An administrator's removal of an account (``user`` by ``super_admin``/``org_admin``,
issue #1104) is covered: the requesting administrator and the administrators of the organizations
the removed account belongs to are told; the removed account is deactivated and is not.
The reminder is de-duplicated with a Redis ``SET NX`` so the hourly tick sends it once.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from enum import StrEnum

from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.redis import get_redis
from app.models.erasure import ErasureLedgerEntry
from app.models.organization import OrganizationMembership
from app.models.user import User

logger = logging.getLogger(__name__)

#: The WebSocket frame type; the frontend titles and words it from ``event``.
ERASURE_NOTICE_EVENT = "erasure_notice"

#: How long before ``scheduled_for`` the reminder goes out.
REMINDER_LEAD = timedelta(hours=24)

#: The reminder's de-duplication key outlives the whole delay (cap 21 days).
_REMINDER_KEY_TTL_SECONDS = 22 * 24 * 3600

#: Subject types an organization administrator schedules.
_NOTIFIED_SUBJECT_TYPES = ("org_member", "organization", "user")


class ErasureNoticeKind(StrEnum):
    """Which moment of the erasure a notice announces."""

    REQUESTED = "requested"
    REMINDER = "reminder"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


def _when(entry: ErasureLedgerEntry) -> str:
    if entry.scheduled_for is None:
        return "an unspecified time"
    return entry.scheduled_for.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _message(kind: ErasureNoticeKind, entry: ErasureLedgerEntry) -> str:
    """The English fallback text; the web client words the notice itself in the user's language."""
    scope = {
        "organization": "an organization",
        "user": "a removed member's personal files",
    }.get(entry.subject_type, "an organization member")
    if kind is ErasureNoticeKind.REQUESTED:
        return (
            f"A data erasure of {scope} was scheduled to run at {_when(entry)}. "
            "An organization administrator can cancel it until then."
        )
    if kind is ErasureNoticeKind.REMINDER:
        return (
            f"A scheduled data erasure of {scope} runs at {_when(entry)}, in about 24 hours. "
            "Cancel it before then if it was not intended."
        )
    if kind is ErasureNoticeKind.CANCELLED:
        return f"The scheduled data erasure of {scope} was cancelled. Nothing was deleted."
    return f"The scheduled data erasure of {scope} has completed."


def resolve_recipients(db: Session, entry: ErasureLedgerEntry) -> list[tuple[int, str | None]]:
    """``(user_id, email)`` of everyone to tell: the organization's admins plus the subject.

    Resolved before an organization erasure runs, because the run deletes the memberships.
    """
    if entry.subject_type not in _NOTIFIED_SUBJECT_TYPES:
        return []
    if entry.subject_type == "user":
        return _removal_recipients(db, entry)
    ids: set[int] = set()
    if entry.subject_organization_id is not None:
        ids.update(
            int(r[0])
            for r in db.query(OrganizationMembership.user_id).filter(
                OrganizationMembership.organization_id == entry.subject_organization_id,
                OrganizationMembership.role == "org:admin",
            )
        )
    if entry.subject_type == "org_member" and entry.subject_user_id is not None:
        ids.add(int(entry.subject_user_id))
    if not ids:
        return []
    rows = db.query(User.id, User.email).filter(User.id.in_(ids), User.is_active.is_(True)).all()
    return [(int(r.id), r.email) for r in rows]


def _removal_recipients(db: Session, entry: ErasureLedgerEntry) -> list[tuple[int, str | None]]:
    """The requesting admin plus the admins of the removed account's organizations (#1104)."""
    if entry.actor_kind == "data_subject":
        return []
    ids: set[int] = set()
    if entry.actor_user_id is not None:
        ids.add(int(entry.actor_user_id))
    if entry.subject_user_id is not None:
        member_orgs = db.query(OrganizationMembership.organization_id).filter(
            OrganizationMembership.user_id == entry.subject_user_id
        )
        ids.update(
            int(r[0])
            for r in db.query(OrganizationMembership.user_id).filter(
                OrganizationMembership.organization_id.in_(member_orgs),
                OrganizationMembership.role == "org:admin",
            )
        )
        ids.discard(int(entry.subject_user_id))
    if not ids:
        return []
    rows = db.query(User.id, User.email).filter(User.id.in_(ids), User.is_active.is_(True)).all()
    return [(int(r.id), r.email) for r in rows]


def resolve_recipients_or_none(
    db: Session, entry: ErasureLedgerEntry
) -> list[tuple[int, str | None]] | None:
    """:func:`resolve_recipients` in a SAVEPOINT; ``None`` when the lookup failed.

    A failed statement aborts the whole PostgreSQL transaction, so without the savepoint the
    caller's next statement (the erasure run itself) would fail too.
    """
    try:
        with db.begin_nested():
            return resolve_recipients(db, entry)
    except SQLAlchemyError:
        logger.warning("Erasure notice: recipients not resolved", exc_info=True)
        return None


def notify(
    db: Session,
    entry: ErasureLedgerEntry,
    kind: ErasureNoticeKind,
    recipients: list[tuple[int, str | None]] | None = None,
) -> int:
    """Send one kind of notice about ``entry``. Never raises. Returns the recipients reached.

    Its database reads run in a SAVEPOINT, so a failed one leaves the caller's transaction usable
    (a removal still commits its restore marker after the request notice, say).
    """
    from app.utils.websocket_notify import send_ws_event

    try:
        with db.begin_nested():
            targets = recipients if recipients is not None else resolve_recipients(db, entry)
            message = _message(kind, entry)
            payload = {
                "event": kind.value,
                "ledger_uuid": str(entry.uuid),
                "subject_type": entry.subject_type,
                "scheduled_for": entry.scheduled_for.isoformat() if entry.scheduled_for else None,
                "status": "warning" if kind is not ErasureNoticeKind.CANCELLED else "completed",
                "message": message,
            }
    except SQLAlchemyError:
        logger.warning("Erasure notice %s: recipients not resolved", kind.value, exc_info=True)
        return 0
    for user_id, email in targets:
        try:
            send_ws_event(user_id, ERASURE_NOTICE_EVENT, payload)
        except (RedisError, OSError):
            logger.warning("Erasure notice %s: websocket send failed", kind.value, exc_info=True)
        _send_mail(email, kind, message)
    return len(targets)


def _send_mail(email: str | None, kind: ErasureNoticeKind, message: str) -> None:
    from app.core.auth_mail import SECURITY_NOTICE_TASK
    from app.core.auth_mail import dispatch_auth_mail
    from app.services.email_service import email_service

    if not email or not email_service.has_transport():
        return
    try:
        dispatch_auth_mail(SECURITY_NOTICE_TASK, email, f"Data erasure {kind.value}", message)
    except Exception:  # boundary: the broker error type varies; the notice is secondary
        logger.warning("Erasure notice %s: email not queued", kind.value, exc_info=True)


def send_due_reminders(db: Session, *, now: datetime | None = None) -> int:
    """Remind about every scheduled erasure that runs within 24 hours. Returns notices sent.

    An erasure scheduled with a delay of 24 hours or less gets no reminder: the request notice
    already is one. The Redis ``SET NX`` makes the hourly tick send each reminder once.
    """
    current = now or datetime.now(UTC)
    entries = (
        db.query(ErasureLedgerEntry)
        .filter(
            ErasureLedgerEntry.subject_type.in_(_NOTIFIED_SUBJECT_TYPES),
            ErasureLedgerEntry.actor_kind != "data_subject",
            ErasureLedgerEntry.status == "deferred",
            ErasureLedgerEntry.deferred_reason == "grace_period",
            ErasureLedgerEntry.scheduled_for > current,
            ErasureLedgerEntry.scheduled_for <= current + REMINDER_LEAD,
        )
        .all()
    )
    sent = 0
    for entry in entries:
        if entry.scheduled_for is None or entry.scheduled_for - entry.requested_at <= REMINDER_LEAD:
            continue
        try:
            first = get_redis().set(
                f"erasure:reminder:{entry.uuid}", "1", nx=True, ex=_REMINDER_KEY_TTL_SECONDS
            )
        except (RedisError, OSError):
            logger.warning("Erasure reminder de-duplication unavailable", exc_info=True)
            continue
        if first:
            notify(db, entry, ErasureNoticeKind.REMINDER)
            sent += 1
    return sent
