"""Read-side questions about content holds (issue #1276; platform holds v467).

Split from ``content_hold_service`` (the writes) so that file stays about changing holds.
Whether a FILE is held is ``hold_guard``'s question; these are the collection-shaped ones the
collection routes ask, and the list the administrator's screen shows.
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import ColumnElement
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.content_hold import AUTHORITY_ORGANIZATION
from app.models.content_hold import AUTHORITY_PLATFORM
from app.models.content_hold import ContentHold
from app.models.content_hold import ContentHoldFile
from app.models.media import MediaFile
from app.schemas.content_hold import HoldSummary
from app.services.hold_guard import HoldState
from app.services.hold_guard import hold_state
from app.services.hold_guard import hold_states
from app.services.hold_guard import revealed_uuids


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
    """The organization's holds, newest first, with the total.

    A platform hold has no organization, so it never appears here.
    """
    return _page(db, [ContentHold.organization_id == org_id], active_only, limit, offset)


def list_platform_holds(
    db: Session, *, active_only: bool = True, limit: int = 100, offset: int = 0
) -> tuple[list[ContentHold], int]:
    """The platform's own holds (any tenant's files), newest first, with the total."""
    return _page(db, [ContentHold.authority == AUTHORITY_PLATFORM], active_only, limit, offset)


def _page(
    db: Session, where: list[ColumnElement[bool]], active_only: bool, limit: int, offset: int
) -> tuple[list[ContentHold], int]:
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


def hold_summary(
    state: HoldState, *, reveal_uuids: bool, reveal_platform_uuids: bool = False
) -> HoldSummary:
    """The badge facts for a state; each kind of hold uuid only to its own administrator."""
    return HoldSummary(
        held=state.held,
        held_by=state.held_by,
        hold_uuids=revealed_uuids(
            state, reveal_uuids=reveal_uuids, reveal_platform_uuids=reveal_platform_uuids
        ),
    )


def file_hold_summary(
    db: Session, file_id: int, *, reveal_uuids: bool, reveal_platform_uuids: bool = False
) -> HoldSummary:
    """The badge facts for one file.

    Organization hold uuids only when ``reveal_uuids``, platform ones only when
    ``reveal_platform_uuids``.
    """
    return hold_summary(
        hold_state(db, file_id),
        reveal_uuids=reveal_uuids,
        reveal_platform_uuids=reveal_platform_uuids,
    )


def list_hold_summaries(db: Session, file_ids: Iterable[int]) -> dict[int, HoldSummary]:
    """The badge facts for every file on one list page, from ONE batched ``hold_states`` call.

    Every id gets a summary (free files get the not-held default). Hold uuids are never put on a
    list, whoever asks: the list says only who holds; the detail page is where an administrator
    gets the release handle.
    """
    ids = {int(i) for i in file_ids}
    states = hold_states(db, ids)
    summaries = {file_id: HoldSummary() for file_id in ids}
    for file_id, state in states.items():
        summaries[file_id] = HoldSummary(held=state.held, held_by=state.held_by)
    return summaries


def collection_hold_states(db: Session, collection_ids: Iterable[int]) -> dict[int, HoldState]:
    """Who holds each collection that carries an active hold of its own; others are absent."""
    ids = {int(i) for i in collection_ids}
    if not ids:
        return {}
    uuids: dict[int, dict[str, list[str]]] = {}
    for collection_id, hold_uuid, authority in db.execute(
        select(ContentHold.collection_id, ContentHold.uuid, ContentHold.authority)
        .where(ContentHold.collection_id.in_(ids), ContentHold.released_at.is_(None))
        .order_by(ContentHold.id)
    ):
        if collection_id is None:
            continue
        by = uuids.setdefault(int(collection_id), {})
        by.setdefault(str(authority), []).append(str(hold_uuid))
    return {
        collection_id: HoldState(
            platform_flag=False,
            org_hold_uuids=tuple(by.get(AUTHORITY_ORGANIZATION, ())),
            platform_hold_uuids=tuple(by.get(AUTHORITY_PLATFORM, ())),
        )
        for collection_id, by in uuids.items()
    }


def collection_hold_summaries(
    db: Session,
    collection_ids: Iterable[int],
    *,
    reveal_uuids: bool,
    reveal_platform_uuids: bool = False,
) -> dict[int, HoldSummary]:
    """A summary for each collection that carries an active hold of its own.

    Collections with no hold are absent; callers keep the default (not held) for them.
    """
    return {
        collection_id: hold_summary(
            state, reveal_uuids=reveal_uuids, reveal_platform_uuids=reveal_platform_uuids
        )
        for collection_id, state in collection_hold_states(db, collection_ids).items()
    }


def find_org_hold(db: Session, org_id: int, hold_uuid: UUID) -> ContentHold | None:
    """The hold with this uuid that belongs to organization ``org_id``, or ``None``."""
    return db.execute(
        select(ContentHold).where(
            ContentHold.uuid == hold_uuid, ContentHold.organization_id == org_id
        )
    ).scalar_one_or_none()


def org_files_by_uuid(db: Session, org_id: int, file_uuids: Iterable[str]) -> list[MediaFile]:
    """The files of organization ``org_id`` among ``file_uuids`` (a stranger's uuid is absent)."""
    return list(
        db.execute(
            select(MediaFile).where(
                MediaFile.uuid.in_(list(file_uuids)), MediaFile.organization_id == org_id
            )
        ).scalars()
    )
