"""Takedown notice records: the read side (issue #1254).

List, detail, export, the owner's own history and the quarantine-queue annotation. All
reads; the write side is :mod:`app.services.takedown_notice_service`.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

from sqlalchemy import exists
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy.orm import Session
from sqlalchemy.orm import contains_eager

from app.core.config import settings
from app.core.enums import TakedownLinkStatus
from app.models.media import MediaFile
from app.models.takedown import TakedownNotice
from app.models.takedown import TakedownNoticeFile
from app.schemas.takedown_notice import OwnerTakedownOut
from app.schemas.takedown_notice import TakedownLinkOut
from app.schemas.takedown_notice import TakedownNoticeOut
from app.services.takedown_notice_service import derive_notice_status

_CONTESTED = (TakedownLinkStatus.OPEN.value, TakedownLinkStatus.COUNTER_NOTICE.value)


def file_titles(db: Session, file_ids: Iterable[int | None]) -> dict[int, str | None]:
    ids = {i for i in file_ids if i is not None}
    if not ids:
        return {}
    rows = db.query(MediaFile.id, MediaFile.title, MediaFile.filename).filter(MediaFile.id.in_(ids))
    return {fid: (title or filename) for fid, title, filename in rows}


def _link_out(link: TakedownNoticeFile, titles: dict[int, str | None]) -> TakedownLinkOut:
    exists_now = link.file_id is not None and link.file_id in titles
    return TakedownLinkOut(
        uuid=str(link.uuid),
        file_uuid=str(link.file_uuid),
        file_title=titles.get(link.file_id) if link.file_id is not None else None,
        file_exists=exists_now,
        owner_user_uuid=str(link.owner_user_uuid),
        organization_uuid=str(link.organization_uuid) if link.organization_uuid else None,
        owner_reason=link.owner_reason,
        status=link.status,
        quarantined_at=link.quarantined_at,
        counter_notice_received_at=link.counter_notice_received_at,
        outcome_at=link.outcome_at,
        outcome_note=link.outcome_note,
        legal_hold_placed=link.legal_hold_placed,
    )


def notices_out(db: Session, notices: list[TakedownNotice]) -> list[TakedownNoticeOut]:
    """Serialise notices with their links and live file titles (one title query)."""
    titles = file_titles(db, (link.file_id for n in notices for link in n.files))
    return [
        TakedownNoticeOut(
            uuid=str(n.uuid),
            notice_type=n.notice_type,
            received_at=n.received_at,
            requester_reference=n.requester_reference,
            basis=n.basis,
            recorded_by_authority=n.recorded_by_authority,
            handled_by_user_uuid=str(n.handled_by_user_uuid) if n.handled_by_user_uuid else None,
            created_at=n.created_at,
            status="open" if derive_notice_status(n.files) == "open" else "resolved",
            links=[_link_out(link, titles) for link in n.files],
        )
        for n in notices
    ]


def list_notices(
    db: Session,
    *,
    status: str | None,
    notice_type: str | None,
    received_from: datetime | None,
    received_to: datetime | None,
    organization_uuid: UUID | None,
    file_uuid: UUID | None,
    limit: int,
    offset: int,
) -> tuple[list[TakedownNotice], int]:
    """Filtered, newest-received-first page of notices plus the unpaginated total."""
    query = db.query(TakedownNotice)
    if notice_type:
        query = query.filter(TakedownNotice.notice_type == notice_type)
    if received_from:
        query = query.filter(TakedownNotice.received_at >= received_from)
    if received_to:
        query = query.filter(TakedownNotice.received_at <= received_to)
    if organization_uuid:
        query = query.filter(
            exists().where(
                TakedownNoticeFile.notice_id == TakedownNotice.id,
                TakedownNoticeFile.organization_uuid == organization_uuid,
            )
        )
    if file_uuid:
        query = query.filter(
            exists().where(
                TakedownNoticeFile.notice_id == TakedownNotice.id,
                TakedownNoticeFile.file_uuid == file_uuid,
            )
        )
    contested = exists().where(
        TakedownNoticeFile.notice_id == TakedownNotice.id,
        TakedownNoticeFile.status.in_(_CONTESTED),
    )
    has_links = exists().where(TakedownNoticeFile.notice_id == TakedownNotice.id)
    if status == "open":
        query = query.filter(or_(contested, ~has_links))
    elif status == "resolved":
        query = query.filter(~contested, has_links)
    total = query.with_entities(func.count(TakedownNotice.id)).scalar() or 0
    rows = (
        query.order_by(TakedownNotice.received_at.desc(), TakedownNotice.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return rows, int(total)


def export_rows(
    db: Session, *, received_from: datetime | None, received_to: datetime | None, cap: int
) -> tuple[list[tuple[TakedownNotice, TakedownNoticeFile]], bool]:
    """One (notice, link) pair per covered file, in a stable order, capped at ``cap``.

    Returns the rows and whether more existed beyond the cap, so a caller can say the
    export is partial instead of silently handing over a prefix.
    """
    query = db.query(TakedownNotice, TakedownNoticeFile).join(
        TakedownNoticeFile, TakedownNoticeFile.notice_id == TakedownNotice.id
    )
    if received_from:
        query = query.filter(TakedownNotice.received_at >= received_from)
    if received_to:
        query = query.filter(TakedownNotice.received_at <= received_to)
    rows = query.order_by(TakedownNotice.received_at, TakedownNoticeFile.id).limit(cap + 1).all()
    return [(n, link) for n, link in rows[:cap]], len(rows) > cap


def owner_takedowns(
    db: Session, owner_uuid: UUID, *, limit: int, offset: int
) -> tuple[list[OwnerTakedownOut], int]:
    """The owner's own takedown history, selected by the authenticated identity only.

    ``owner_uuid`` comes from the session, never from a request parameter. The model that
    is returned has no field for the internal basis, the requester or the handler.
    """
    base = db.query(TakedownNoticeFile).filter(TakedownNoticeFile.owner_user_uuid == owner_uuid)
    total = base.with_entities(func.count(TakedownNoticeFile.id)).scalar() or 0
    links = (
        base.join(TakedownNotice, TakedownNotice.id == TakedownNoticeFile.notice_id)
        .options(contains_eager(TakedownNoticeFile.notice))
        .order_by(TakedownNoticeFile.quarantined_at.desc(), TakedownNoticeFile.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    titles = file_titles(db, (link.file_id for link in links))
    contact = settings.ABUSE_CONTACT_EMAIL or None
    out = []
    for link in links:
        organization = link.notice.recorded_by_authority == "organization"
        out.append(
            OwnerTakedownOut(
                link_uuid=str(link.uuid),
                file_uuid=str(link.file_uuid),
                file_title=titles.get(link.file_id) if link.file_id is not None else None,
                notice_type=link.notice.notice_type,
                received_at=link.notice.received_at,
                owner_reason=link.owner_reason,
                status=link.status,
                quarantined_at=link.quarantined_at,
                outcome_at=link.outcome_at,
                dispute_to="organization" if organization else "operator",
                abuse_contact_email=None if organization else contact,
            )
        )
    return out, int(total)


def live_notice_refs(
    db: Session, file_ids: list[int]
) -> dict[int, list[tuple[str, str, str, str]]]:
    """Per file id, the non-released links as ``(notice_uuid, link_uuid, type, status)``."""
    if not file_ids:
        return {}
    rows = (
        db.query(TakedownNoticeFile, TakedownNotice)
        .join(TakedownNotice, TakedownNotice.id == TakedownNoticeFile.notice_id)
        .filter(
            TakedownNoticeFile.file_id.in_(file_ids),
            TakedownNoticeFile.status != TakedownLinkStatus.RELEASED.value,
        )
        .order_by(TakedownNoticeFile.id)
        .all()
    )
    refs: dict[int, list[tuple[str, str, str, str]]] = {}
    for link, notice in rows:
        refs.setdefault(int(link.file_id), []).append(
            (str(notice.uuid), str(link.uuid), notice.notice_type, link.status)
        )
    return refs
