import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.core.constants import TAG_SOURCE_MANUAL
from app.db.base import get_db
from app.models.media import FileTag
from app.models.media import Tag
from app.models.user import User
from app.schemas.media import TagImpact
from app.schemas.media import TagMergeRequest
from app.schemas.media import TagMutationResult
from app.schemas.media import TagPromoteRequest
from app.schemas.media import TagRenameRequest
from app.schemas.media import TagShareTarget
from app.services.tag_operations import TagNotFoundError
from app.services.tag_operations import delete_tags
from app.services.tag_operations import merge_tags
from app.services.tag_operations import preview_tag_impact
from app.services.tag_operations import promote_tags_to_shared
from app.services.tag_operations import rename_tag
from app.services.tag_service import InvalidTagNameError
from app.services.tag_service import accessible_file_ids_subquery
from app.services.tag_service import resolve_or_create_tag
from app.utils.uuid_helpers import get_by_uuid

logger = logging.getLogger(__name__)


def _owned_or_system(user_id: int) -> ColumnElement[bool]:
    """Tags the user may write against: their own, plus the system vocabulary."""
    return or_(Tag.user_id == user_id, Tag.user_id.is_(None))


def _visible_to(db: Session, user_id: int, organization_id: Any) -> ColumnElement[bool]:
    """Predicate for the tags ``user_id`` is allowed to see.

    A tag is visible when it is a system tag (``user_id IS NULL``), owned by the
    caller, or attached to a file the caller can access.
    ``get_accessible_file_ids_subquery`` already covers files shared directly and
    via groups and applies the org tenant gate, so sharing needs no extra rule
    here — do not add a parallel one.
    """
    accessible_files = accessible_file_ids_subquery(db, user_id, organization_id)
    attached_to_accessible = select(FileTag.tag_id).where(
        FileTag.media_file_id.in_(select(accessible_files))
    )
    return or_(_owned_or_system(user_id), Tag.id.in_(attached_to_accessible))


def _resolve_tag(db: Session, name: str, user_id: int) -> Tag:
    """Resolve a user-supplied name to a tag, mapping a blank name to a 422.

    Resolution is normalized-exact (``app/services/tag_service.py``) and scoped
    to the caller's own vocabulary plus the system one, so a typed name can never
    resolve onto another account's row. A near match is never applied here — a
    person typed this name, so a fuzzy hit may only ever be offered as a
    suggestion.
    """
    try:
        return resolve_or_create_tag(db, name, user_id=user_id, source=TAG_SOURCE_MANUAL)
    except InvalidTagNameError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Tag name is required",
        ) from exc


def _writable_tag_ids(
    db: Session, tag_uuids: list[UUID], *, user_id: int, is_admin: bool
) -> list[int]:
    """Resolve public tag UUIDs to internal ids the caller may **mutate**.

    Reading a tag and rewriting it are different rights. ``_visible_to`` admits
    any tag attached to a file shared with you, but renaming or deleting one of
    those rewrites its owner's vocabulary everywhere they use it, so mutation is
    narrower: your own tags always, system tags for an admin only (they are the
    shared vocabulary every account's picker shows, which is why
    ``cleanup_unused_tags`` is admin-gated and skips them).

    A tag that exists but is not writable 404s rather than 403s — the same answer
    an unknown UUID gets, so probing this endpoint cannot enumerate other
    accounts' tags.
    """
    ids: list[int] = []
    for tag_uuid in tag_uuids:
        tag = get_by_uuid(db, Tag, tag_uuid, error_message="Tag not found")
        owned = tag.user_id == user_id
        system = tag.user_id is None
        if not (owned or (system and is_admin)):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found")
        ids.append(tag.id)
    return ids


def _share_target(share) -> TagShareTarget:
    """Project a grant onto the wire, naming the target rather than its id."""
    if share.target_user_id is not None:
        target = share.target_user
        name = getattr(target, "full_name", None) or getattr(target, "email", "") or "user"
        kind = "user"
    else:
        target = share.target_group
        name = getattr(target, "name", "") or "group"
        kind = "group"
    shared_by = getattr(share.shared_by_user, "full_name", None) or getattr(
        share.shared_by_user, "email", None
    )
    return TagShareTarget(uuid=share.uuid, target_type=kind, display_name=name, shared_by=shared_by)


def _apply(operation, *args, result_model=TagMutationResult, **kwargs):
    """Run a tag operation, translating its service errors into HTTP ones."""
    try:
        return result_model.model_validate(operation(*args, **kwargs))
    except TagNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InvalidTagNameError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Tag name is required",
        ) from exc


router = APIRouter()

router = APIRouter()


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
