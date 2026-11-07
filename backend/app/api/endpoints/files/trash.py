"""The trash routes (issue #1101): list, restore, delete forever, empty.

``DELETE /files/{uuid}`` (``cancel_upload`` -> ``crud.delete_media_file``) is what moves a file
here. Every route resolves a trashed file through ``services/trash_access`` (the normal lookup
404s a trashed file for everyone) and changes it through ``services/trash_service``; the
permission table is in ``trash_access``'s docstring. Included in the files router BEFORE the
``/{file_uuid}`` routes, so ``/trash`` is never read as a file uuid.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.core.exceptions import DomainError
from app.core.exceptions import ResourceNotFoundError
from app.db.base import get_db
from app.models.media import MediaFile
from app.models.user import User
from app.schemas.media import MediaFile as MediaFileSchema
from app.schemas.trash import TrashedFile
from app.schemas.trash import TrashPage
from app.schemas.trash import TrashPurgeQueued
from app.services import trash_access
from app.services import trash_service
from app.services.hold_guard import hold_state
from app.services.hold_guard import hold_states
from app.services.hold_guard import refusal_detail
from app.services.request_context import RequestContext

router = APIRouter()


def _row(file: MediaFile, users: dict[int, User], held: bool, ctx: RequestContext) -> TrashedFile:
    owner = users.get(int(file.user_id))
    deleter = users.get(int(file.deleted_by_user_id)) if file.deleted_by_user_id else None
    can_restore, can_purge = trash_access.row_flags(ctx, file)
    return TrashedFile(
        uuid=str(file.uuid),
        filename=str(file.filename),
        title=file.title,
        owner_uuid=str(owner.uuid) if owner else None,
        owner_name=(owner.full_name or owner.email) if owner else None,
        deleted_at=file.deleted_at,  # type: ignore[arg-type]  # trash rows always carry it
        purge_after=file.purge_after,  # type: ignore[arg-type]
        trash_origin=str(file.trash_origin),
        deleted_by_uuid=str(deleter.uuid) if deleter else None,
        deleted_by_name=(deleter.full_name or deleter.email) if deleter else None,
        held=held,
        can_restore=can_restore,
        can_purge=can_purge and not held,
    )


@router.get("/trash", response_model=TrashPage)
def list_trash(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    _active: User = Depends(get_current_active_user),
) -> TrashPage:
    """The caller's trash in the active tenant, newest deletion first."""
    files, total = trash_access.trash_page(db, ctx, page=page, page_size=page_size)
    users = trash_access.people_of(db, files)
    held = hold_states(db, [int(f.id) for f in files])
    return TrashPage(
        items=[_row(f, users, int(f.id) in held, ctx) for f in files],
        total=total,
        page=page,
        page_size=page_size,
        retention_days=trash_service.trash_retention_days(db),
    )


@router.post("/{file_uuid}/restore", response_model=MediaFileSchema)
def restore_file(
    file_uuid: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    _active: User = Depends(get_current_active_user),
) -> Any:
    """Bring a file back from the trash; 409 ``NOT_RESTORABLE`` once its purge is due."""
    file = trash_access.get_trashed_file(db, file_uuid, ctx)
    trash_access.require_restorable_by(ctx, file)
    restored, _refused = trash_service.restore_files(
        db, [file], actor=ctx.user, request=request, bypass=ctx.bypass
    )
    if not restored:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": trash_service.NOT_RESTORABLE,
                "message": "This file's time in the trash has ended and it is being deleted.",
                "file_id": str(file.uuid),
            },
        )
    return restored[0]


def _not_in_trash_or_404(db: Session, file_uuid: str, ctx: RequestContext) -> None:
    """Raise 409 ``FILE_NOT_IN_TRASH`` for a live file the caller may delete, else 404."""
    from app.services.delete_permissions import get_deletable_file

    try:
        get_deletable_file(
            db,
            file_uuid,
            ctx.user,
            organization_id=ctx.org_id,
            is_org_admin=ctx.is_org_admin,
            bypass=ctx.bypass,
        )
    except DomainError:
        raise ResourceNotFoundError("File not found in the trash") from None
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error": trash_service.FILE_NOT_IN_TRASH,
            "message": "Move the file to the trash before deleting it forever.",
            "file_id": file_uuid,
        },
    )


@router.delete(
    "/{file_uuid}/purge", status_code=status.HTTP_202_ACCEPTED, response_model=TrashPurgeQueued
)
def purge_file(
    file_uuid: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    _active: User = Depends(get_current_active_user),
) -> TrashPurgeQueued:
    """Delete a trashed file forever. 409 when it is not in the trash, held or under takedown."""
    try:
        file = trash_access.get_trashed_file(db, file_uuid, ctx)
    except ResourceNotFoundError:
        _not_in_trash_or_404(db, file_uuid, ctx)
        raise
    trash_access.require_purgeable_by(ctx, file)
    marked, _refused = trash_service.mark_for_immediate_purge(
        db, [file], actor=ctx.user, request=request, bypass=ctx.bypass
    )
    if not marked:
        state = hold_state(db, int(file.id))
        if state.held:
            detail = refusal_detail(state, reveal_uuids=trash_access.can_purge(ctx, file))
        else:
            detail = {
                "error": trash_service.FILE_UNDER_TAKEDOWN,
                "message": "A file under takedown review is kept until the takedown is released.",
            }
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail={**detail, "file_id": str(file.uuid)}
        )
    trash_service.dispatch_purge([int(f.id) for f in marked])
    return TrashPurgeQueued(queued=len(marked))


@router.post("/trash/empty", status_code=status.HTTP_202_ACCEPTED, response_model=TrashPurgeQueued)
def empty_trash(
    request: Request,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    _active: User = Depends(get_current_active_user),
) -> TrashPurgeQueued:
    """Delete forever everything in the caller's trash they may purge (held files stay).

    403 for an organization member: in an organization only an admin deletes forever.
    """
    trash_access.require_may_empty(ctx)
    candidates = [
        f for f in trash_access.purgeable_query(db, ctx).all() if trash_access.can_purge(ctx, f)
    ]
    marked = trash_service.empty_trash(
        db, candidates, actor=ctx.user, request=request, bypass=ctx.bypass, org_id=ctx.org_id
    )
    trash_service.dispatch_purge([int(f.id) for f in marked])
    return TrashPurgeQueued(queued=len(marked))


__all__ = ["router"]
