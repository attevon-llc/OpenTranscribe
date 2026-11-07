"""``/admin/holds``: the instance administrator's legal holds, without a takedown (v467).

A platform hold keeps files from deletion, the trash purge, retention, account and GDPR erasure
and reprocessing, exactly as an organization's does (``services/hold_guard`` is what every
destroy path asks), but the file stays visible to its owner: nothing is quarantined. It is the
hold a self-hosted operator places on a legal demand. The takedown's own hold (the
``media_file.legal_hold`` flag set by ``/admin/files/{uuid}/quarantine``) is unchanged and is
managed there.

Gating: ``get_current_admin_user`` (``admin`` and ``super_admin``, the same gate as the takedown
routes) and no capability, because an operator must always be able to act on a legal demand
(the reasoning behind the ungated ``admin.takedown``). Under a support-access grant every route
answers 403 (``/admin`` prefix). A platform hold may name any tenant's file or collection; a
missing one is a 404. Only platform holds are reachable here: an organization's hold is a 404 to
read and to release, and this screen never shows one.

The reason and matter reference are returned to instance administrators only. They are never in
an audit event (``record_hold_change`` stages ids and counts), a socket message or a file
response, and no caught exception text is ever put in a response.
"""

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi import status
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_admin_user
from app.auth.rate_limit import limiter
from app.auth.rate_limit import user_or_ip_key
from app.core.constants import ADMIN_HOLD_RATE_LIMIT
from app.db.base import get_db
from app.models.content_hold import AUTHORITY_PLATFORM
from app.models.content_hold import ContentHold
from app.models.user import User
from app.schemas.content_hold import HoldCreate
from app.schemas.content_hold import HoldList
from app.schemas.content_hold import HoldOut
from app.schemas.content_hold import HoldRelease
from app.services import content_hold_service as holds
from app.services.content_hold_queries import list_platform_holds
from app.services.content_hold_records import account_labels
from app.services.content_hold_records import collection_by_uuid
from app.services.content_hold_records import files_by_uuid
from app.services.content_hold_records import find_platform_hold
from app.services.content_hold_records import hold_records
from app.services.content_hold_records import people_of
from app.services.content_hold_records import tenants_of
from app.services.platform_bypass import audit_metadata_access

logger = logging.getLogger(__name__)

router = APIRouter()

_ERROR_STATUS = {
    "reason_length": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "no_files": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "too_many_files": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "already_released": status.HTTP_409_CONFLICT,
    "not_found": status.HTTP_404_NOT_FOUND,
}


def _http(error: holds.HoldRequestError) -> HTTPException:
    # ``error.message`` is one of the service's fixed sentences, never exception text.
    return HTTPException(
        status_code=_ERROR_STATUS.get(error.code, status.HTTP_400_BAD_REQUEST),
        detail=error.message,
    )


def _find(db: Session, hold_uuid: UUID) -> ContentHold:
    hold = find_platform_hold(db, hold_uuid)
    if hold is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hold not found")
    return hold


def _serialize(db: Session, rows: list[ContentHold], *, with_files: bool = False) -> list[HoldOut]:
    return hold_records(db, rows, account_labels(db, people_of(rows)), with_files=with_files)


@router.get("", response_model=HoldList)
def list_admin_holds(
    active_only: bool = Query(True, description="Only holds that are still in force"),
    limit: int = Query(100, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> Any:
    """The platform's legal holds, newest first. Organization holds are never listed here."""
    rows, total = list_platform_holds(db, active_only=active_only, limit=limit, offset=offset)
    audit_metadata_access(
        db,
        current_user,
        route="/api/admin/holds",
        count=len(rows),
        organization_ids=tenants_of(db, rows),
    )
    return HoldList(holds=_serialize(db, rows), total=total)


@router.get("/{hold_uuid}", response_model=HoldOut)
def get_admin_hold(
    hold_uuid: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> Any:
    """One platform hold, with the files it covers (up to 500)."""
    hold = _find(db, hold_uuid)
    audit_metadata_access(
        db,
        current_user,
        route="/api/admin/holds/{hold_uuid}",
        count=1,
        organization_ids=tenants_of(db, [hold]),
    )
    return _serialize(db, [hold], with_files=True)[0]


@router.post("", response_model=HoldOut, status_code=status.HTTP_201_CREATED)
@limiter.shared_limit(ADMIN_HOLD_RATE_LIMIT, scope="admin_hold", key_func=user_or_ip_key)
def place_admin_hold(
    request: Request,
    response: Response,
    body: HoldCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> Any:
    """Place a platform hold on named files of any tenant, or on a whole collection.

    A collection hold covers its files now and every file added later. Nothing is quarantined.
    """
    try:
        if body.scope == "file":
            files = files_by_uuid(db, body.file_uuids)
            if len(files) != len({str(u) for u in body.file_uuids}):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
            change = holds.place_file_hold(
                db,
                org_id=None,
                authority=AUTHORITY_PLATFORM,
                actor=current_user,
                files=files,
                reason=body.reason,
                matter_ref=body.matter_ref,
            )
        else:
            collection = (
                collection_by_uuid(db, body.collection_uuid)
                if body.collection_uuid is not None
                else None
            )
            if collection is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Collection not found"
                )
            change = holds.place_collection_hold(
                db,
                org_id=None,
                authority=AUTHORITY_PLATFORM,
                actor=current_user,
                collection=collection,
                reason=body.reason,
                matter_ref=body.matter_ref,
            )
    except holds.HoldRequestError as error:
        # Every refusal is raised before the service writes anything, so nothing to undo.
        raise _http(error) from None
    holds.record_hold_change(db, request, current_user, change)
    db.commit()
    holds.finish_hold_change(db, request, current_user, change)
    return _serialize(db, [change.hold])[0]


@router.post("/{hold_uuid}/release", response_model=HoldOut)
@limiter.shared_limit(ADMIN_HOLD_RATE_LIMIT, scope="admin_hold", key_func=user_or_ip_key)
def release_admin_hold(
    request: Request,
    response: Response,
    hold_uuid: UUID,
    body: HoldRelease,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> Any:
    """Release a platform hold. Files another hold or the takedown still covers stay held."""
    hold = _find(db, hold_uuid)
    try:
        change = holds.release_hold(
            db,
            org_id=None,
            authority=AUTHORITY_PLATFORM,
            actor=current_user,
            hold=hold,
            reason=body.reason,
        )
    except holds.HoldRequestError as error:
        # Every refusal is raised before the service writes anything, so nothing to undo.
        raise _http(error) from None
    holds.record_hold_change(db, request, current_user, change)
    db.commit()
    holds.finish_hold_change(db, request, current_user, change)
    return _serialize(db, [change.hold])[0]
