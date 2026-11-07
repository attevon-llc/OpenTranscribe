"""The administrator's record of content holds (``HoldOut``) and the platform's lookups.

Both hold screens show the same record: the organization admin's (``/org-admin/holds``) and the
instance admin's (``/admin/holds``, platform holds placed without a takedown). They differ only
in how a person is named (``labels``: an organization names its own members; the platform names
any account) and in how a target is found (an organization resolves inside its own tenant, the
platform across every tenant), so the record is built here once.

The record carries the hold's ``reason`` and ``matter_ref``: it is for the administrators of the
hold's authority only, and is never put in an audit event, a socket message or a file response.
"""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.collection import Collection
from app.models.content_hold import AUTHORITY_PLATFORM
from app.models.content_hold import ContentHold
from app.models.content_hold import ContentHoldFile
from app.models.media import MediaFile
from app.models.organization import Organization
from app.models.user import User
from app.schemas.content_hold import HeldFile
from app.schemas.content_hold import HoldOut

#: Most covered files a single hold read lists; the list endpoint carries counts only.
MAX_FILES_LISTED = 500


def hold_records(
    db: Session,
    rows: Iterable[ContentHold],
    labels: Mapping[int, str],
    *,
    with_files: bool = False,
) -> list[HoldOut]:
    """``HoldOut`` for each hold; ``labels`` names the people who placed and released them."""
    rows = list(rows)
    if not rows:
        return []
    ids = [h.id for h in rows]
    counts = {
        int(hold_id): int(n)
        for hold_id, n in db.execute(
            select(ContentHoldFile.hold_id, func.count())
            .where(ContentHoldFile.hold_id.in_(ids))
            .group_by(ContentHoldFile.hold_id)
        )
    }
    org_ids = {int(h.organization_id) for h in rows if h.organization_id is not None}
    org_uuids = (
        {
            int(i): u
            for i, u in db.execute(
                select(Organization.id, Organization.uuid).where(Organization.id.in_(org_ids))
            )
        }
        if org_ids
        else {}
    )
    out: list[HoldOut] = []
    for hold in rows:
        files = None
        if with_files:
            files = [
                HeldFile(uuid=f.uuid, title=f.title, filename=f.filename)
                for f in db.execute(
                    select(MediaFile)
                    .join(ContentHoldFile, ContentHoldFile.media_file_id == MediaFile.id)
                    .where(ContentHoldFile.hold_id == hold.id)
                    .order_by(MediaFile.id)
                    .limit(MAX_FILES_LISTED)
                ).scalars()
            ]
        collection = hold.collection
        out.append(
            HoldOut(
                uuid=hold.uuid,
                authority=hold.authority,
                organization_uuid=org_uuids.get(int(hold.organization_id))
                if hold.organization_id is not None
                else None,
                scope=hold.scope,
                collection_uuid=collection.uuid if collection is not None else None,
                collection_name=collection.name if collection is not None else None,
                matter_ref=hold.matter_ref,
                reason=hold.reason,
                placed_at=hold.placed_at,
                placed_by_display=labels.get(int(hold.placed_by_user_id))
                if hold.placed_by_user_id is not None
                else None,
                released_at=hold.released_at,
                released_by_display=labels.get(int(hold.released_by_user_id))
                if hold.released_by_user_id is not None
                else None,
                release_reason=hold.release_reason,
                active=hold.released_at is None,
                file_count=counts.get(int(hold.id), 0),
                files=files,
            )
        )
    return out


def people_of(rows: Iterable[ContentHold]) -> set[int]:
    """The accounts that placed or released ``rows``."""
    return {
        int(i) for h in rows for i in (h.placed_by_user_id, h.released_by_user_id) if i is not None
    }


def account_labels(db: Session, user_ids: set[int]) -> dict[int, str]:
    """A name (or the address when there is none) for each account, for the platform screen."""
    if not user_ids:
        return {}
    return {
        int(i): str(name or email)
        for i, name, email in db.execute(
            select(User.id, User.full_name, User.email).where(User.id.in_(user_ids))
        )
    }


def find_platform_hold(db: Session, hold_uuid: UUID) -> ContentHold | None:
    """The platform hold with this uuid; an organization's hold is not one (``None``)."""
    return db.execute(
        select(ContentHold).where(
            ContentHold.uuid == hold_uuid, ContentHold.authority == AUTHORITY_PLATFORM
        )
    ).scalar_one_or_none()


def files_by_uuid(db: Session, uuids: Iterable[UUID]) -> list[MediaFile]:
    """The files with these uuids, in ANY tenant (a platform hold's targets)."""
    wanted = {str(u) for u in uuids}
    if not wanted:
        return []
    return list(db.execute(select(MediaFile).where(MediaFile.uuid.in_(wanted))).scalars())


def collection_by_uuid(db: Session, collection_uuid: UUID) -> Collection | None:
    """The collection with this uuid, in ANY tenant (a platform hold's target)."""
    return db.execute(
        select(Collection).where(Collection.uuid == collection_uuid)
    ).scalar_one_or_none()


def tenants_of(db: Session, rows: Iterable[ContentHold]) -> set[int]:
    """The organizations whose files or collections ``rows`` name, for the metadata audit."""
    holds = list(rows)
    if not holds:
        return set()
    ids = [h.id for h in holds]
    named: list[int | None] = list(
        db.execute(
            select(MediaFile.organization_id)
            .join(ContentHoldFile, ContentHoldFile.media_file_id == MediaFile.id)
            .where(ContentHoldFile.hold_id.in_(ids))
            .distinct()
        ).scalars()
    )
    named.extend(h.collection.organization_id for h in holds if h.collection is not None)
    return {int(o) for o in named if o is not None}
