"""The destructive half: rename, merge, delete, promote — each fronted by an impact preview.

Shared helpers and the visibility rules live in ``_common``.
"""

from uuid import UUID

from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.tags._common import _apply
from app.api.endpoints.tags._common import _writable_tag_ids
from app.api.endpoints.tags._common import router
from app.db.base import get_db
from app.models.user import User
from app.schemas.media import TagImpact
from app.schemas.media import TagMergeRequest
from app.schemas.media import TagMutationResult
from app.schemas.media import TagPromoteRequest
from app.schemas.media import TagRenameRequest
from app.services.tag_operations import delete_tags
from app.services.tag_operations import merge_tags
from app.services.tag_operations import preview_tag_impact
from app.services.tag_operations import promote_tags_to_shared
from app.services.tag_operations import rename_tag


@router.get("/impact", response_model=TagImpact)
def get_tag_impact(
    tag_uuids: list[UUID] = Query(..., min_length=1),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Report what renaming, merging, or deleting these tags would touch.

    Returns the caller-visible count **and** the global count separately: tags
    are global rows, so an operation confirmed against the accessible number
    alone would understate its own blast radius.
    """
    report = preview_tag_impact(
        db,
        _writable_tag_ids(db, tag_uuids, user_id=current_user.id, is_admin=current_user.is_admin),
        user_id=current_user.id,
        organization_id=ctx.org_id,
    )
    return TagImpact.model_validate(report)


@router.post("/promote", response_model=TagMutationResult)
def promote_tags_endpoint(
    payload: TagPromoteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Publish owned tags into the shared vocabulary (admin only).

    The consolidation lever for a multi-user deployment: ownership keeps one
    account from renaming another's tag, but it also lets four private
    "Interview" rows accumulate with nothing to point at. Promoting one makes it
    the row every account resolves onto, and folds the same-named private rows
    into it so their file associations are preserved rather than orphaned.

    Registered **before** ``PATCH /{tag_uuid}`` and the other path-parameter
    routes for the usual FastAPI reason — ``/promote`` would otherwise be
    swallowed by ``/{tag_uuid}``.
    """
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can promote tags to the shared vocabulary",
        )
    return _apply(
        promote_tags_to_shared,
        db,
        _writable_tag_ids(
            db, payload.tag_uuids, user_id=current_user.id, is_admin=current_user.is_admin
        ),
        user_id=current_user.id,
        organization_id=ctx.org_id,
    )


@router.patch("/{tag_uuid}", response_model=TagMutationResult)
def rename_tag_endpoint(
    tag_uuid: UUID,
    payload: TagRenameRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Rename a tag.

    A new name that resolves to a *different* existing tag is a merge; the
    response comes back with ``requires_confirmation`` and an impact preview and
    nothing is applied until the caller retries with ``confirm_merge``. A case
    variant of the tag's own name is a plain rename.
    """
    tag_id = _writable_tag_ids(
        db, [tag_uuid], user_id=current_user.id, is_admin=current_user.is_admin
    )[0]
    return _apply(
        rename_tag,
        db,
        tag_id,
        payload.name,
        confirm_merge=payload.confirm_merge,
        user_id=current_user.id,
        organization_id=ctx.org_id,
    )


@router.post("/{tag_uuid}/merge", response_model=TagMutationResult)
def merge_tags_endpoint(
    tag_uuid: UUID,
    payload: TagMergeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Fold the listed tags into the tag in the path, which survives."""
    target_id = _writable_tag_ids(
        db, [tag_uuid], user_id=current_user.id, is_admin=current_user.is_admin
    )[0]
    return _apply(
        merge_tags,
        db,
        target_id,
        _writable_tag_ids(
            db, payload.source_uuids, user_id=current_user.id, is_admin=current_user.is_admin
        ),
        user_id=current_user.id,
        organization_id=ctx.org_id,
    )


@router.delete("", response_model=TagMutationResult)
def delete_tags_endpoint(
    tag_uuids: list[UUID] = Query(..., min_length=1),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Delete one or more tags, returning the impact the delete realized."""
    return _apply(
        delete_tags,
        db,
        _writable_tag_ids(db, tag_uuids, user_id=current_user.id, is_admin=current_user.is_admin),
        user_id=current_user.id,
        organization_id=ctx.org_id,
    )
