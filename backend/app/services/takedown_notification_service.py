"""Email the owners of taken-down files, once per notice, and log what happened (issue #1254).

The in-app notice is separate and already durable (the owner's takedown history route), so
this is the optional second channel. Rules:

* **Recipients come from the database.** The notify route takes a notice uuid and nothing
  else; an address or a user id supplied by the caller never reaches this module.
* **One email per (notice, owner, event)**, listing every file of theirs that the notice
  covers, however many files a bulk takedown touched. A repeat call sends nothing new.
* **Best effort.** A missing transport or a failed send is recorded and returned as a
  count; it never raises into the takedown, and the reason text is never logged.
* Organization administrators are not notified here. What a platform-recorded notice may
  disclose to them is an authorization decision that has not been made.
"""

from __future__ import annotations

import logging
import uuid as uuid_pkg
from collections import defaultdict

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import TakedownLinkStatus
from app.core.enums import TakedownNotifyEvent
from app.core.enums import TakedownNotifyStatus
from app.models.takedown import TakedownNotice
from app.models.takedown import TakedownNoticeFile
from app.models.takedown import TakedownNotification
from app.models.user import User
from app.schemas.takedown_notice import NotifyResponse
from app.services.email_service import email_service
from app.services.takedown_notice_queries import file_titles

logger = logging.getLogger(__name__)

_IN_FORCE = (
    TakedownLinkStatus.OPEN.value,
    TakedownLinkStatus.COUNTER_NOTICE.value,
    TakedownLinkStatus.UPHELD.value,
)
_CHANNEL = "email"
#: A row in one of these states is final; ``failed`` and ``skipped_no_transport`` are retried.
_FINAL = (TakedownNotifyStatus.SENT.value, TakedownNotifyStatus.SKIPPED_NO_RECIPIENT.value)


def _compose(
    event: TakedownNotifyEvent,
    organization: bool,
    items: list[tuple[str, str]],
) -> tuple[str, str, list[str]]:
    """Subject, heading and paragraphs for one owner. ``items`` is ``(title, reason)``."""
    if event is TakedownNotifyEvent.RELEASED:
        lines = [f"- {title}" for title, _ in items]
        return (
            "Access to your files was restored",
            "Your files are accessible again",
            ["The following files were restored:", *lines],
        )
    contact = settings.ABUSE_CONTACT_EMAIL or ""
    if organization:
        dispute = "To dispute this, send a counter-notice to your organization's administrators."
    elif contact:
        dispute = f"To dispute this, email a counter-notice to {contact}."
    else:
        dispute = "To dispute this, contact your service operator with a counter-notice."
    lines = [f"- {title}: {reason}" for title, reason in items]
    return (
        "Files taken down",
        "Some of your files were taken down",
        ["The following files were taken down, with the reason recorded:", *lines, dispute],
    )


def notify_owners(
    db: Session, notice: TakedownNotice, event: TakedownNotifyEvent
) -> NotifyResponse:
    """Email each affected owner once for ``event`` on ``notice``; record every outcome.

    Commits the delivery log per owner so a crash part-way keeps what was already sent.
    """
    wanted = (
        (TakedownLinkStatus.RELEASED.value,) if event is TakedownNotifyEvent.RELEASED else _IN_FORCE
    )
    links = [link for link in notice.files if link.status in wanted]
    counts = {"sent": 0, "failed": 0, "skipped_no_transport": 0, "already_notified": 0}
    if not links:
        return NotifyResponse(**counts)

    existing = {
        (row.link_id, row.recipient_user_uuid): row
        for row in db.query(TakedownNotification).filter(
            TakedownNotification.link_id.in_([link.id for link in links]),
            TakedownNotification.event == event.value,
            TakedownNotification.channel == _CHANNEL,
        )
    }
    by_owner: dict[uuid_pkg.UUID, list[TakedownNoticeFile]] = defaultdict(list)
    for link in links:
        row = existing.get((link.id, link.owner_user_uuid))
        if row is not None and row.status in _FINAL:
            counts["already_notified"] += 1
            continue
        by_owner[link.owner_user_uuid].append(link)

    titles = file_titles(db, (link.file_id for group in by_owner.values() for link in group))
    organization = notice.recorded_by_authority == "organization"
    transport = email_service.has_transport()
    for owner_uuid, group in by_owner.items():
        owner = db.query(User).filter(User.uuid == owner_uuid, User.is_active.is_(True)).first()
        status = _deliver(owner, group, event, organization, titles, transport)
        for link in group:
            row = existing.get((link.id, owner_uuid))
            if row is None:
                row = TakedownNotification(
                    link_id=link.id,
                    event=event.value,
                    recipient_kind="owner",
                    recipient_user_uuid=owner_uuid,
                    channel=_CHANNEL,
                )
                db.add(row)
            row.recipient_user_id = owner.id if owner is not None else None
            row.status = status.value
        db.commit()
        if status is TakedownNotifyStatus.SENT:
            counts["sent"] += 1
        elif status is TakedownNotifyStatus.FAILED:
            counts["failed"] += 1
        elif status is TakedownNotifyStatus.SKIPPED_NO_TRANSPORT:
            counts["skipped_no_transport"] += 1
    return NotifyResponse(**counts)


def _deliver(
    owner: User | None,
    group: list[TakedownNoticeFile],
    event: TakedownNotifyEvent,
    organization: bool,
    titles: dict[int, str | None],
    transport: bool,
) -> TakedownNotifyStatus:
    """Send one owner's email and classify the result. Never raises, never logs the text."""
    if owner is None or not owner.email:
        return TakedownNotifyStatus.SKIPPED_NO_RECIPIENT
    if not transport:
        logger.info("Takedown email not sent: no mail transport is configured")
        return TakedownNotifyStatus.SKIPPED_NO_TRANSPORT
    items = [
        (
            (titles.get(link.file_id) if link.file_id is not None else None) or "A file",
            link.owner_reason,
        )
        for link in group
    ]
    subject, heading, paragraphs = _compose(event, organization, items)
    try:
        email_service.send_takedown_notice(str(owner.email), subject, heading, paragraphs)
    except Exception as exc:  # noqa: BLE001 - a failed notice must never break the takedown
        logger.warning("Takedown email failed (%s)", type(exc).__name__)
        return TakedownNotifyStatus.FAILED
    return TakedownNotifyStatus.SENT
