"""Read-side questions about organization content holds (issue #1276).

Split from ``content_hold_service`` (the writes) so that file stays about changing holds.
Whether a FILE is held is ``hold_guard``'s question; these are the collection-shaped ones the
collection routes ask, and the list the administrator's screen shows.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.content_hold import ContentHold
from app.models.content_hold import ContentHoldFile
from app.schemas.content_hold import HoldSummary
from app.services.hold_guard import HELD_BY_ORGANIZATION
from app.services.hold_guard import hold_state


def collection_has_active_hold(db: Session, collection_id: int) -> bool:
    """True while an unreleased hold is attached to this collection."""
    return (
        db.execute(
            select(ContentHold.id)
            .where(ContentHold.collection_id == collection_id, ContentHold.released_at.is_(None))
            .limit(1)
        ).first()
        is not None
    )


def held_by_collection_hold(db: Session, collection_id: int, file_ids: Iterable[int]) -> set[int]:
    """The files among ``file_ids`` that an active hold on THIS collection covers."""
    ids = {int(i) for i in file_ids}
    if not ids:
        return set()
    return {
        int(i)
        for i in db.execute(
            select(ContentHoldFile.media_file_id)
            .join(ContentHold, ContentHold.id == ContentHoldFile.hold_id)
            .where(
                ContentHold.collection_id == collection_id,
                ContentHold.released_at.is_(None),
                ContentHoldFile.media_file_id.in_(ids),
            )
        ).scalars()
    }


def list_holds(
    db: Session, org_id: int, *, active_only: bool = True, limit: int = 100, offset: int = 0
) -> tuple[list[ContentHold], int]:
    """The organization's holds, newest first, with the total."""
    where = [ContentHold.organization_id == org_id]
    if active_only:
        where.append(ContentHold.released_at.is_(None))
    total = int(db.execute(select(func.count(ContentHold.id)).where(*where)).scalar() or 0)
    rows = list(
        db.execute(
            select(ContentHold)
            .where(*where)
            .order_by(ContentHold.id.desc())
            .limit(limit)
            .offset(offset)
        ).scalars()
    )
    return rows, total


def file_hold_summary(db: Session, file_id: int, *, reveal_uuids: bool) -> HoldSummary:
    """The badge facts for one file; hold uuids only when ``reveal_uuids``."""
    state = hold_state(db, file_id)
    return HoldSummary(
        held=state.held,
        held_by=state.held_by,
        hold_uuids=list(state.org_hold_uuids) if reveal_uuids else [],
    )


def collection_hold_summaries(
    db: Session, collection_ids: Iterable[int], *, reveal_uuids: bool
) -> dict[int, HoldSummary]:
    """A summary for each collection that carries an active hold of its own.

    Collections with no hold are absent; callers keep the default (not held) for them.
    """
    ids = {int(i) for i in collection_ids}
    if not ids:
        return {}
    uuids: dict[int, list[str]] = {}
    for collection_id, hold_uuid in db.execute(
        select(ContentHold.collection_id, ContentHold.uuid)
        .where(ContentHold.collection_id.in_(ids), ContentHold.released_at.is_(None))
        .order_by(ContentHold.id)
    ):
        uuids.setdefault(int(collection_id), []).append(str(hold_uuid))
    return {
        collection_id: HoldSummary(
            held=True,
            held_by=[HELD_BY_ORGANIZATION],
            hold_uuids=held if reveal_uuids else [],
        )
        for collection_id, held in uuids.items()
    }
