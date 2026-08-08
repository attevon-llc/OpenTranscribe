import logging
from typing import Any
from typing import Literal
from uuid import UUID

from fastapi import APIRouter
from fastapi import Body
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.core.constants import TAG_SOURCE_MANUAL
from app.db.base import get_db
from app.models.media import FileTag
from app.models.media import Tag
from app.models.user import User
from app.schemas.media import Tag as TagSchema
from app.schemas.media import TagBase
from app.schemas.media import TagCollisionCluster as TagCollisionClusterSchema
from app.schemas.media import TagImpact
from app.schemas.media import TagMergeRequest
from app.schemas.media import TagMutationResult
from app.schemas.media import TagRenameRequest
from app.schemas.media import TagReviewRequest
from app.schemas.media import TagReviewResult
from app.schemas.media import TagWithCount
from app.services.tag_collisions import find_tag_collisions
from app.services.tag_collisions import list_tags_filtered
from app.services.tag_collisions import list_unused_tag_rows
from app.services.tag_operations import TagNotFoundError
from app.services.tag_operations import delete_tags
from app.services.tag_operations import merge_tags
from app.services.tag_operations import preview_tag_impact
from app.services.tag_operations import rename_tag
from app.services.tag_review import accept_tags
from app.services.tag_review import preview_tag_review
from app.services.tag_review import reject_tags
from app.services.tag_service import InvalidTagNameError
from app.services.tag_service import on_tags_changed
from app.services.tag_service import resolve_or_create_tag
from app.utils.uuid_helpers import get_by_uuid

logger = logging.getLogger(__name__)


def _resolve_tag(db: Session, name: str) -> Tag:
    """Resolve a user-supplied name to a tag, mapping a blank name to a 422.

    Resolution is normalized-exact (``app/services/tag_service.py``). A near
    match is never applied here — a person typed this name, so a fuzzy hit may
    only ever be offered as a suggestion.
    """
    try:
        return resolve_or_create_tag(db, name, source=TAG_SOURCE_MANUAL)
    except InvalidTagNameError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Tag name is required",
        ) from exc


def _tag_ids(db: Session, tag_uuids: list[UUID]) -> list[int]:
    """Resolve public tag UUIDs to internal ids, 404ing on the first unknown one."""
    return [
        get_by_uuid(db, Tag, tag_uuid, error_message="Tag not found").id for tag_uuid in tag_uuids
    ]


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


@router.post("", response_model=TagSchema)
def create_tag(
    tag_data: TagBase,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Create a new tag
    """
    tag = _resolve_tag(db, tag_data.name)
    db.commit()
    db.refresh(tag)
    # No file carries it yet, so there is nothing to reindex — but the new row
    # belongs in every user's tag list, not just this one's.
    on_tags_changed(db, user_id=current_user.id)
    return tag


@router.get("", response_model=list[TagWithCount])
def list_tags(
    awaiting_review: bool = Query(
        False, description="Only tags the auto-labeler created and nobody has reviewed"
    ),
    unused: bool = Query(False, description="Only tags no accessible file carries"),
    colliding: bool = Query(
        False, description="Only tags sharing a normalized name with another tag"
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """List tags with usage counts, most used first, optionally narrowed.

    Filtering, counting, and clustering live in ``services/tag_collisions.py``;
    the filters combine (AND). Errors propagate — this used to return ``[]`` on
    any exception, rendering a broken query as an install with no tags.

    Read-through cached in Redis (``cache:tags:{user_id}``) behind
    ``READ_CACHE_ENABLED`` **only for the unfiltered personal-scope request**:
    the key carries neither the filters nor the scope, so caching either variant
    would serve one request's answer to another. Every tag/file-tag mutation
    path busts this key, so reads are always fresh.
    """
    from app.core.config import settings as app_settings
    from app.services.redis_cache_service import TTL_TAGS
    from app.services.redis_cache_service import redis_cache

    filtered = awaiting_review or unused or colliding
    cache_key = f"cache:tags:{current_user.id}"
    use_cache = app_settings.READ_CACHE_ENABLED and ctx.org_id is None and not filtered

    if use_cache:
        cached = redis_cache.get(cache_key)
        if cached is not None:
            return cached

    entries = list_tags_filtered(
        db,
        user_id=current_user.id,
        organization_id=ctx.org_id,
        awaiting_review=awaiting_review,
        unused=unused,
        colliding=colliding,
    )
    tags_with_counts = [TagWithCount.model_validate(entry) for entry in entries]

    if use_cache:
        # Cache the post-Pydantic dicts (never ORM objects).
        redis_cache.set(
            cache_key, [t.model_dump(mode="json") for t in tags_with_counts], ttl=TTL_TAGS
        )

    return tags_with_counts


@router.get("/unused", response_model=list[TagSchema])
def list_unused_tags(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """List tags no file the caller can see is carrying.

    Scoped to the caller's accessible files, exactly like ``usage_count`` on the
    tag list — the two used to disagree because this one counted usage globally.
    Errors propagate rather than degrading to an empty list.
    """
    return list_unused_tag_rows(db, user_id=current_user.id, organization_id=ctx.org_id)


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


@router.delete("/cleanup", response_model=dict[str, Any])
def cleanup_unused_tags(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_active_user)
):
    """Delete every tag no file anywhere carries (admin only).

    Deliberately **global**, unlike ``GET /tags/unused``, which is scoped to what
    the caller can see: this deletes rows, and a tag that merely looks unused to
    one admin may be carrying someone else's files.
    """
    # Only allow admin users to perform this operation
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can clean up unused tags",
        )

    try:
        # Find all tags that are not used in any files
        used_tag_ids = db.query(FileTag.tag_id).distinct().subquery()

        # Delete all tags not in use
        delete_query = db.query(Tag).filter(~Tag.id.in_(used_tag_ids))  # type: ignore[arg-type]

        # Get the count for the response
        count = delete_query.count()

        # Delete the tags
        if count > 0:
            delete_query.delete(synchronize_session=False)
            db.commit()
            logger.info(f"Deleted {count} unused tags")

        return {
            "deleted_count": count,
            "message": f"{count} unused tags deleted successfully",
        }
    except Exception as e:
        logger.error(f"Error in cleanup_unused_tags: {e}")
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error cleaning up unused tags: {str(e)}",
        ) from e


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
        db, _tag_ids(db, tag_uuids), user_id=current_user.id, organization_id=ctx.org_id
    )
    return TagImpact.model_validate(report)


@router.get("/review-impact", response_model=TagReviewResult)
def preview_tag_review_endpoint(
    tag_uuids: list[UUID] = Query(..., min_length=1),
    action: Literal["accept", "reject"] = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Report what accepting or rejecting these tags would do, applying nothing.

    A reject reports its association split — how many the auto-labeler created
    and would lose, and how many hand-applied ones would stay — because either
    number alone hides what the other decides.
    """
    return _apply(
        preview_tag_review,
        db,
        _tag_ids(db, tag_uuids),
        action=action,
        user_id=current_user.id,
        organization_id=ctx.org_id,
        result_model=TagReviewResult,
    )


@router.post("/accept", response_model=TagReviewResult)
def accept_tags_endpoint(
    payload: TagReviewRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Endorse auto-labeled tags so they stop awaiting review.

    Associations are untouched. Tags that were not the auto-labeler's come back
    as ``not_applicable`` rather than failing the call.
    """
    return _apply(
        accept_tags,
        db,
        _tag_ids(db, payload.tag_uuids),
        user_id=current_user.id,
        organization_id=ctx.org_id,
        result_model=TagReviewResult,
    )


@router.post("/reject", response_model=TagReviewResult)
def reject_tags_endpoint(
    payload: TagReviewRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Remove the associations the auto-labeler created for these tags.

    The tag row itself goes only when no association is left, so a tag people
    have applied by hand survives the reject with that work intact.
    """
    return _apply(
        reject_tags,
        db,
        _tag_ids(db, payload.tag_uuids),
        user_id=current_user.id,
        organization_id=ctx.org_id,
        result_model=TagReviewResult,
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
    tag_id = get_by_uuid(db, Tag, tag_uuid, error_message="Tag not found").id
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
    target_id = get_by_uuid(db, Tag, tag_uuid, error_message="Tag not found").id
    return _apply(
        merge_tags,
        db,
        target_id,
        _tag_ids(db, payload.source_uuids),
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
        _tag_ids(db, tag_uuids),
        user_id=current_user.id,
        organization_id=ctx.org_id,
    )


@router.post("/files/{file_uuid}/tags", response_model=TagSchema)
async def add_tag_to_file(
    request: Request,
    file_uuid: str,
    tag_data: dict = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    from app.utils.uuid_helpers import get_file_by_uuid_with_permission

    # Get file by UUID and verify permission (tenant-gated via ctx.org_id)
    media_file = get_file_by_uuid_with_permission(
        db, file_uuid, current_user.id, is_admin=current_user.is_admin, organization_id=ctx.org_id
    )
    file_id = media_file.id  # Get internal ID for database operations

    # Log detailed information about the request for debugging
    logger.info(f"Received add_tag_to_file request for file_uuid={file_uuid}, tag_data={tag_data}")

    # Handle the raw dictionary and extract the name
    if not tag_data or "name" not in tag_data:
        logger.error(f"Invalid tag data received: {tag_data}")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Tag name is required",
        )

    # Convert to proper TagBase object
    tag_base = TagBase(name=tag_data["name"])

    # Atomically resolve or create the tag (race-condition safe)
    # Note: ownership already verified by get_file_by_uuid_with_permission above
    tag = _resolve_tag(db, tag_base.name)
    logger.info(f"Using tag: {tag.id}:{tag.name} for file_id={file_id}")

    # Check if file already has this tag
    existing_tag = (
        db.query(FileTag).filter(FileTag.media_file_id == file_id, FileTag.tag_id == tag.id).first()
    )

    if not existing_tag:
        # Add tag to file
        file_tag = FileTag(media_file_id=file_id, tag_id=tag.id, source=TAG_SOURCE_MANUAL)
        db.add(file_tag)
        db.commit()

        on_tags_changed(db, [file_id], user_id=current_user.id)

    return tag


@router.delete("/files/{file_uuid}/tags/{tag_name}", status_code=status.HTTP_204_NO_CONTENT)
def remove_tag_from_file(
    file_uuid: str,
    tag_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """
    Remove a tag from a media file
    """
    # Get file by UUID with permission check (tenant-gated via ctx.org_id)
    from app.utils.uuid_helpers import get_file_by_uuid_with_permission

    media_file = get_file_by_uuid_with_permission(
        db, file_uuid, current_user.id, is_admin=current_user.is_admin, organization_id=ctx.org_id
    )
    file_id = media_file.id

    # Find the tag
    tag = db.query(Tag).filter(Tag.name == tag_name).first()
    if not tag:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tag not found")

    # Remove the association
    file_tag = (
        db.query(FileTag).filter(FileTag.media_file_id == file_id, FileTag.tag_id == tag.id).first()
    )

    if file_tag:
        db.delete(file_tag)
        db.commit()

        # Detach is a tag change like any other: the file's indexed tag array
        # has to lose this name or search keeps matching it.
        on_tags_changed(db, [file_id], user_id=current_user.id)

    # Also check if this tag is now unused and should be removed
    # Count how many files still use this tag
    tag_use_count = db.query(func.count(FileTag.tag_id)).filter(FileTag.tag_id == tag.id).scalar()

    # If the tag is no longer used by any files, we can optionally delete it
    # We're choosing to keep unused tags in the database for now, as they may be useful for auto-completion
    # and tag suggestions. This ensures a good UX where users don't have to recreate common tags.

    # Log the tag usage status for monitoring
    if tag_use_count == 0:
        logger.info(
            f"Tag '{tag_name}' (ID: {tag.id}) is now unused but still preserved in the database"
        )

    return None
