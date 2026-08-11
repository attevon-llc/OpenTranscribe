"""Read-only questions about tags: duplicates, what a selection carries, what a tag touches.

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
from app.api.endpoints.tags._common import _visible_to
from app.api.endpoints.tags._common import router
from app.db.base import get_db
from app.models.media import Tag
from app.models.user import User
from app.schemas.media import TagCollisionCluster as TagCollisionClusterSchema
from app.schemas.media import TagFileList
from app.schemas.media import TaggedFile
from app.schemas.media import TagOnSelection
from app.services.formatting_service import FormattingService
from app.services.tag_collisions import files_for_tag
from app.services.tag_collisions import find_tag_collisions
from app.services.tag_collisions import tags_on_files
from app.services.tag_service import tag_ownership
from app.utils.uuid_helpers import get_by_uuid


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
