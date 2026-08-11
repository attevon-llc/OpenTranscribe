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
from app.schemas.media import TagCollisionCluster as TagCollisionClusterSchema
from app.schemas.media import TagFileList
from app.schemas.media import TaggedFile
from app.schemas.media import TagMutationResult
from app.schemas.media import TagOnSelection
from app.schemas.media import TagShareTarget
from app.services.formatting_service import FormattingService
from app.services.tag_collisions import files_for_tag
from app.services.tag_collisions import find_tag_collisions
from app.services.tag_collisions import tags_on_files
from app.services.tag_operations import TagNotFoundError
from app.services.tag_service import InvalidTagNameError
from app.services.tag_service import accessible_file_ids_subquery
from app.services.tag_service import resolve_or_create_tag
from app.services.tag_service import tag_ownership
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


@router.get("/collisions", response_model=list[TagCollisionClusterSchema])
def list_tag_collisions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Group duplicate tags into clusters, each with a preselected survivor.

    Grouping is exact equality on the stored normalization, so repeated requests
    over unchanged data return the same clusters in the same order; fuzzy near
    matches come back as a separately ranked suggestion list and are never
    cluster members. The pass calls ``refresh_stored_normalization`` first, so
    this GET **does write** — deliberately, and by name.
    """
    clusters = find_tag_collisions(db, user_id=current_user.id, organization_id=ctx.org_id)
    return [TagCollisionClusterSchema.model_validate(cluster) for cluster in clusters]


@router.get("/for-files", response_model=list[TagOnSelection])
def list_tags_on_files(
    file_uuids: list[UUID] = Query(..., min_length=1),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """The tags a set of selected files already carries.

    The bulk apply surface has to show what a selection *has* before offering to
    change it, and ``GET /api/files`` deliberately carries no per-file tags
    (#326) — so nothing else can answer this.

    ``file_count`` is how many of the selected files carry the tag, which is
    what lets one selected file offer a full chip editor while several offer
    add-only: removing a tag that sits on three of five files is ambiguous in a
    way adding never is.

    Registered before ``/{tag_uuid}`` so the literal path is not swallowed.
    """
    from app.models.media import MediaFile

    file_ids = [
        get_by_uuid(db, MediaFile, file_uuid, error_message="File not found").id
        for file_uuid in file_uuids
    ]
    selection_size = len(set(file_ids))
    rows = tags_on_files(db, file_ids, user_id=current_user.id, organization_id=ctx.org_id)
    return [
        TagOnSelection(
            uuid=tag.uuid,
            name=tag.name,
            source=tag.source,
            ownership=tag_ownership(tag, current_user.id),
            file_count=count,
            selection_size=selection_size,
        )
        for tag, count in rows
    ]


@router.get("/{tag_uuid}/files", response_model=TagFileList)
def list_files_for_tag(
    tag_uuid: UUID,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """List the accessible files carrying this tag — what the tag *touches*.

    Gated on ``_visible_to`` rather than the writable scope: seeing which of
    your own files a tag sits on is a read, and a tag reaches you precisely
    because it is on a file you can access.

    Registered before ``PATCH /{tag_uuid}`` for the usual FastAPI ordering
    reason, and after the fixed-path routes so ``/impact`` and friends are not
    swallowed by ``/{tag_uuid}``.
    """
    tag = get_by_uuid(db, Tag, tag_uuid, error_message="Tag not found")
    visible = db.query(Tag).filter(Tag.id == tag.id, _visible_to(db, current_user.id, ctx.org_id))
    if visible.first() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found")

    files, total = files_for_tag(
        db, tag.id, user_id=current_user.id, organization_id=ctx.org_id, limit=limit
    )
    return TagFileList(
        total=total,
        files=[
            TaggedFile(
                uuid=media_file.uuid,
                display_title=media_file.title or media_file.filename or str(media_file.uuid),
                status=media_file.status.value
                if hasattr(media_file.status, "value")
                else media_file.status,
                formatted_duration=FormattingService.format_duration(media_file.duration),
            )
            for media_file in files
        ],
    )
