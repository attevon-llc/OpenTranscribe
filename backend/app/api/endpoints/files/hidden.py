"""``PUT`` / ``DELETE /files/{uuid}/hidden``: hide a file from the caller's own views (#1102).

Hide is a per-user view preference, not access control: it changes nothing for anyone else,
and the file stays reachable by direct link. Anyone who can read the file may hide it (owner
included); the file is resolved through the permission choke point first, so a file outside
the caller's tenant, without a share, trashed or quarantined answers 404 exactly like a missing
one. Both verbs are idempotent and answer 204. Not audited: a personal preference with no
governance value (plan §4.6). The bulk form is the ``hide``/``unhide`` bulk action.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Response
from fastapi import status
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.db.base import get_db
from app.models.user import User
from app.services import hidden_media_service
from app.services.request_context import RequestContext
from app.services.uuid_helpers import get_file_by_uuid_with_permission

router = APIRouter()

_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"description": "No such file, or not one the caller can read."}
}


@router.put(
    "/{file_uuid}/hidden",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        **_NOT_FOUND,
        409: {"description": "HIDDEN_LIMIT: the caller already hides the most files allowed."},
    },
)
def hide_file(
    file_uuid: UUID,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    _active: User = Depends(get_current_active_user),  # preserve the is_active gate
) -> Response:
    """Hide one file from the caller's gallery, lists, search and corpus-wide chat."""
    media_file = get_file_by_uuid_with_permission(
        db,
        file_uuid,
        ctx.user.id,
        bypass=ctx.bypass,
        organization_id=ctx.org_id,
        conceal_inaccessible=True,
    )
    hidden_media_service.hide(db, ctx.user.id, [int(media_file.id)])
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/{file_uuid}/hidden",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_NOT_FOUND,
)
def unhide_file(
    file_uuid: UUID,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    _active: User = Depends(get_current_active_user),  # preserve the is_active gate
) -> Response:
    """Show a hidden file in the caller's views again. A file that was not hidden is a no-op."""
    media_file = get_file_by_uuid_with_permission(
        db,
        file_uuid,
        ctx.user.id,
        bypass=ctx.bypass,
        organization_id=ctx.org_id,
        conceal_inaccessible=True,
    )
    hidden_media_service.unhide(db, ctx.user.id, [int(media_file.id)])
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
