"""Takedown notice records: the write side (issue #1254).

This module never commits. ``takedown_service.quarantine_file`` / ``release_file`` own the
transaction, so the quarantine flags and the notice link land together or not at all: a
quarantined file with no record cannot exist, which is the point of the record.

The routes read through :mod:`app.services.takedown_notice_queries`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.core.enums import TakedownLinkStatus
from app.core.enums import TakedownNoticeType
from app.models.media import MediaFile
from app.models.organization import Organization
from app.models.takedown import TakedownNotice
from app.models.takedown import TakedownNoticeFile
from app.models.user import User

if TYPE_CHECKING:
    from app.services.takedown_authority import TakedownActor

logger = logging.getLogger(__name__)

CODE_NOTICE_NOT_FOUND = "takedown_notice_not_found"
CODE_LINK_NOT_FOUND = "takedown_notice_file_not_found"
CODE_LINK_CLOSED = "takedown_notice_link_closed"
CODE_NOT_IN_FORCE = "takedown_not_in_force"
CODE_ILLEGAL_TRANSITION = "takedown_illegal_transition"
CODE_NOTICE_REQUIRED = "takedown_notice_required"

#: from -> the states a PATCH may move a link to. ``released`` is deliberately absent:
#: only the release action sets it, so the record can never claim a file was restored
#: while it is still hidden.
_PATCH_TRANSITIONS: dict[TakedownLinkStatus, frozenset[TakedownLinkStatus]] = {
    TakedownLinkStatus.OPEN: frozenset(
        {TakedownLinkStatus.COUNTER_NOTICE, TakedownLinkStatus.UPHELD}
    ),
    TakedownLinkStatus.COUNTER_NOTICE: frozenset({TakedownLinkStatus.UPHELD}),
    TakedownLinkStatus.UPHELD: frozenset({TakedownLinkStatus.COUNTER_NOTICE}),
    TakedownLinkStatus.RELEASED: frozenset(),
}


class TakedownNoticeError(Exception):
    """A notice operation was refused. Nothing was changed."""

    def __init__(self, code: str, status_code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.message = message


def create_notice(
    db: Session,
    *,
    handler: User,
    authority: str,
    notice_type: TakedownNoticeType,
    received_at: datetime,
    requester_reference: str | None,
    basis: str,
) -> TakedownNotice:
    """Add a notice to the session and flush it (no commit).

    ``requester_reference`` must be ``None`` for an ``operator_action`` and set for any
    other type; the database enforces the same rule, this keeps the error readable.
    """
    is_operator_action = notice_type is TakedownNoticeType.OPERATOR_ACTION
    if is_operator_action == bool(requester_reference):
        raise ValueError("an operator action carries no requester reference; a notice must")
    notice = TakedownNotice(
        notice_type=notice_type.value,
        received_at=received_at,
        requester_reference=requester_reference,
        basis=basis,
        recorded_by_authority=authority,
        handled_by_user_id=handler.id,
        handled_by_user_uuid=handler.uuid,
    )
    db.add(notice)
    db.flush()
    return notice


def find_notice(db: Session, notice_uuid: object) -> TakedownNotice:
    """Load a notice by its external uuid, or raise a 404 :class:`TakedownNoticeError`."""
    notice = db.query(TakedownNotice).filter(TakedownNotice.uuid == notice_uuid).first()
    if notice is None:
        raise TakedownNoticeError(CODE_NOTICE_NOT_FOUND, 404, "Takedown notice not found")
    return notice


def record_quarantine(
    db: Session,
    file: MediaFile,
    *,
    actor: TakedownActor,
    notice: TakedownNotice | None,
    operator_action_reason: str | None,
    reason: str,
    legal_hold: bool,
    when: datetime,
) -> TakedownNoticeFile:
    """Write the link between ``file`` and its notice (no commit).

    Exactly one of ``notice`` and ``operator_action_reason`` must be given: a takedown
    either cites a recorded notice or states that it is an operator action with no notice.
    Re-quarantining under a notice the file already carries is a no-op on the link; under
    a different notice it adds a second link (two complainants, one file).

    Raises:
        ValueError: Neither or both of the two were given.
        TakedownNoticeError: The notice's link for this file is already released.
    """
    if (notice is None) == (operator_action_reason is None):
        raise ValueError("quarantine needs exactly one of notice or operator_action_reason")
    if notice is None:
        notice = create_notice(
            db,
            handler=actor.user,
            authority=actor.authority.value,
            notice_type=TakedownNoticeType.OPERATOR_ACTION,
            received_at=when,
            requester_reference=None,
            basis=str(operator_action_reason),
        )
    existing = (
        db.query(TakedownNoticeFile)
        .filter(
            TakedownNoticeFile.notice_id == notice.id, TakedownNoticeFile.file_uuid == file.uuid
        )
        .first()
    )
    if existing is not None:
        if existing.status == TakedownLinkStatus.RELEASED.value:
            raise TakedownNoticeError(
                CODE_LINK_CLOSED,
                409,
                "This notice was already closed for this file; record a new notice",
            )
        return existing

    # A missing owner would break the file's own NOT NULL foreign key first; a None here
    # fails the NOT NULL on the snapshot column rather than being papered over.
    owner_uuid = db.query(User.uuid).filter(User.id == file.user_id).scalar()
    org_uuid = (
        db.query(Organization.uuid).filter(Organization.id == file.organization_id).scalar()
        if file.organization_id
        else None
    )
    link = TakedownNoticeFile(
        notice_id=notice.id,
        file_id=file.id,
        file_uuid=file.uuid,
        owner_user_uuid=owner_uuid,
        organization_id=file.organization_id,
        organization_uuid=org_uuid,
        owner_reason=reason[:2000],
        status=TakedownLinkStatus.OPEN.value,
        quarantined_at=when,
        legal_hold_placed=bool(legal_hold),
    )
    db.add(link)
    db.flush()
    return link


def close_links_on_release(
    db: Session, file: MediaFile, *, note: str | None, when: datetime
) -> list[TakedownNoticeFile]:
    """Mark every non-released link of ``file`` released (no commit).

    A file may carry several (two complainants); a release restores it for all of them.
    """
    links = (
        db.query(TakedownNoticeFile)
        .filter(
            TakedownNoticeFile.file_id == file.id,
            TakedownNoticeFile.status != TakedownLinkStatus.RELEASED.value,
        )
        .all()
    )
    for link in links:
        link.status = TakedownLinkStatus.RELEASED.value
        link.outcome_at = when
        if note:
            link.outcome_note = note[:2000]
    return links


def transition_link(
    db: Session,
    link: TakedownNoticeFile,
    *,
    to: TakedownLinkStatus,
    received_at: datetime | None,
    note: str | None,
) -> TakedownNoticeFile:
    """Record a counter-notice or an upheld decision on ``link`` (no commit).

    These are record-keeping transitions: they never change the file's state.

    Raises:
        TakedownNoticeError: The link is closed, the move is not allowed, or the file is
            not currently under takedown (409 in each case).
    """
    # Lock the file row (the row ``release_file`` locks first), then re-read the link. A
    # release that committed after this request loaded them would otherwise be overwritten
    # by a status saying the file is still taken down while it is in fact restored.
    file = (
        db.query(MediaFile)
        .filter(MediaFile.id == link.file_id)
        .populate_existing()
        .with_for_update()
        .first()
        if link.file_id is not None
        else None
    )
    db.refresh(link)
    current = TakedownLinkStatus(link.status)
    if current is TakedownLinkStatus.RELEASED:
        raise TakedownNoticeError(CODE_LINK_CLOSED, 409, "This takedown has already been released")
    if to not in _PATCH_TRANSITIONS[current]:
        raise TakedownNoticeError(
            CODE_ILLEGAL_TRANSITION,
            409,
            f"A takedown that is {current.value} cannot become {to.value}",
        )
    if file is None or link.file_id is None or not file.is_quarantined:
        raise TakedownNoticeError(CODE_NOT_IN_FORCE, 409, "This file is not currently taken down")
    now = datetime.now(UTC)
    link.status = to.value
    if to is TakedownLinkStatus.COUNTER_NOTICE:
        link.counter_notice_received_at = received_at or now
        link.outcome_at = None
        link.outcome_note = None
    else:
        link.outcome_at = now
        link.outcome_note = note[:2000] if note else None
    return link


def derive_notice_status(links: Iterable[TakedownNoticeFile]) -> str:
    """``open`` while any file is open or contested (or none is linked yet), else ``resolved``.

    Derived, never stored: a letter can end differently for each file it names, so a
    stored notice-level state would be wrong for a mixed letter.
    """
    statuses = [link.status for link in links]
    contested = {TakedownLinkStatus.OPEN.value, TakedownLinkStatus.COUNTER_NOTICE.value}
    if not statuses or any(s in contested for s in statuses):
        return "open"
    return "resolved"
