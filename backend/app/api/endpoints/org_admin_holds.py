"""``/org-admin/holds``: an organization's legal holds (issue #1276).

An organization administrator places a hold on named files or on a whole collection, lists
them, and releases them. A hold keeps its files from deletion, retention, account or GDPR
erasure and reprocessing (``services/hold_guard`` is what every destroy path asks); this router
only changes the facts that answer reads (``services/content_hold_service``).

Isolation: every lookup carries ``organization_id == ctx.org_id`` IN THE QUERY. A file or
collection of another organization, a personal one and a missing one are the same 404, and so
is a hold of another organization, so the routes are not an existence oracle. The organization
comes from the resolved request context, never from the path, query or body.

Gating: ``require_org_admin`` plus the server-enforced ``org.legal_hold`` capability (mounted in
``api/router.py`` with no platform-admin bypass and refused under a support grant). Platform
staff are not organization administrators, so they cannot place or release an organization's
hold through the product. The reason is read by this organization's administrators only.
"""

import logging
from collections.abc import Iterable
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi import status
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import require_org_admin
from app.api.endpoints.org_admin import org_id_of
from app.auth.rate_limit import limiter
from app.auth.rate_limit import user_or_ip_key
from app.core.constants import ORG_HOLD_RATE_LIMIT
from app.db.base import get_db
from app.models.content_hold import ContentHold
from app.models.content_hold import ContentHoldFile
from app.models.media import MediaFile
from app.schemas.content_hold import HeldFile
from app.schemas.content_hold import HoldCreate
from app.schemas.content_hold import HoldList
from app.schemas.content_hold import HoldOut
from app.schemas.content_hold import HoldRelease
from app.services import content_hold_service as holds
from app.services.collection_visibility import visible_collections_by_uuid
from app.services.content_hold_queries import list_holds
from app.services.org_audit_view import member_labels

logger = logging.getLogger(__name__)

router = APIRouter()

#: Most covered files a single hold read lists; the list endpoint carries counts only.
MAX_FILES_LISTED = 500

_ERROR_STATUS = {
    "reason_length": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "no_files": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "too_many_files": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "already_released": status.HTTP_409_CONFLICT,
    # Not-in-your-organization and not-found are one answer by design.
    "not_org_file": status.HTTP_404_NOT_FOUND,
    "not_org_collection": status.HTTP_404_NOT_FOUND,
    "not_found": status.HTTP_404_NOT_FOUND,
}


def _http(error: holds.HoldRequestError) -> HTTPException:
    return HTTPException(
        status_code=_ERROR_STATUS.get(error.code, status.HTTP_400_BAD_REQUEST),
        detail=error.message,
    )


def _find_hold(db: Session, org_id: int, hold_uuid: UUID) -> ContentHold:
    hold = db.execute(
        select(ContentHold).where(
            ContentHold.uuid == hold_uuid, ContentHold.organization_id == org_id
        )
    ).scalar_one_or_none()
    if hold is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hold not found")
    return hold


def _serialize(
    db: Session, org_id: int, rows: Iterable[ContentHold], *, with_files: bool = False
) -> list[HoldOut]:
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
    people = {
        int(i) for h in rows for i in (h.placed_by_user_id, h.released_by_user_id) if i is not None
    }
    names = member_labels(db, org_id, people)
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
                scope=hold.scope,
                collection_uuid=collection.uuid if collection is not None else None,
                collection_name=collection.name if collection is not None else None,
                matter_ref=hold.matter_ref,
                reason=hold.reason,
                placed_at=hold.placed_at,
                placed_by_display=names.get(int(hold.placed_by_user_id))
                if hold.placed_by_user_id is not None
                else None,
                released_at=hold.released_at,
                released_by_display=names.get(int(hold.released_by_user_id))
                if hold.released_by_user_id is not None
                else None,
                release_reason=hold.release_reason,
                active=hold.released_at is None,
                file_count=counts.get(int(hold.id), 0),
                files=files,
            )
        )
    return out


@router.get("", response_model=HoldList)
def list_org_holds(
    active_only: bool = Query(True, description="Only holds that are still in force"),
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(require_org_admin),
):
    """This organization's holds, newest first."""
    org_id = org_id_of(ctx)
    rows, total = list_holds(db, org_id, active_only=active_only, limit=limit, offset=offset)
    return HoldList(holds=_serialize(db, org_id, rows), total=total)


@router.get("/{hold_uuid}", response_model=HoldOut)
def get_org_hold(
    hold_uuid: UUID,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(require_org_admin),
):
    """One hold of this organization, with the files it covers (up to 500)."""
    org_id = org_id_of(ctx)
    return _serialize(db, org_id, [_find_hold(db, org_id, hold_uuid)], with_files=True)[0]


@router.post("", response_model=HoldOut, status_code=status.HTTP_201_CREATED)
@limiter.shared_limit(ORG_HOLD_RATE_LIMIT, scope="org_hold", key_func=user_or_ip_key)
def place_org_hold(
    request: Request,
    response: Response,
    body: HoldCreate,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(require_org_admin),
):
    """Place a hold on named files, or on a whole collection (its files now and later)."""
    org_id = org_id_of(ctx)
    try:
        if body.scope == "file":
            wanted = {str(u) for u in body.file_uuids}
            files = list(
                db.execute(
                    select(MediaFile).where(
                        MediaFile.uuid.in_(wanted), MediaFile.organization_id == org_id
                    )
                ).scalars()
            )
            if len(files) != len(wanted):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
            change = holds.place_file_hold(
                db,
                org_id=org_id,
                actor=ctx.user,
                files=files,
                reason=body.reason,
                matter_ref=body.matter_ref,
            )
        else:
            found = visible_collections_by_uuid(
                db, int(ctx.user.id), org_id, [str(body.collection_uuid)]
            )
            if not found:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found"
                )
            change = holds.place_collection_hold(
                db,
                org_id=org_id,
                actor=ctx.user,
                collection=found[0],
                reason=body.reason,
                matter_ref=body.matter_ref,
            )
    except holds.HoldRequestError as error:
        # Every refusal is raised before the service writes anything, so nothing to undo.
        raise _http(error) from None
    db.commit()
    holds.finish_hold_change(db, request, ctx.user, change)
    return _serialize(db, org_id, [change.hold])[0]


@router.post("/{hold_uuid}/release", response_model=HoldOut)
@limiter.shared_limit(ORG_HOLD_RATE_LIMIT, scope="org_hold", key_func=user_or_ip_key)
def release_org_hold(
    request: Request,
    response: Response,
    hold_uuid: UUID,
    body: HoldRelease,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(require_org_admin),
):
    """Release a hold. Files another hold or the platform still covers stay held."""
    org_id = org_id_of(ctx)
    hold = _find_hold(db, org_id, hold_uuid)
    try:
        change = holds.release_hold(
            db, org_id=org_id, actor=ctx.user, hold=hold, reason=body.reason
        )
    except holds.HoldRequestError as error:
        # Every refusal is raised before the service writes anything, so nothing to undo.
        raise _http(error) from None
    db.commit()
    holds.finish_hold_change(db, request, ctx.user, change)
    return _serialize(db, org_id, [change.hold])[0]
