"""Create, list and attach/detach — the everyday tag surface.

Shared helpers and the visibility rules live in ``_common``.
"""

from typing import Any
from typing import Literal

from fastapi import Body
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.tags._common import _resolve_tag
from app.api.endpoints.tags._common import logger
from app.api.endpoints.tags._common import router
from app.core.constants import TAG_SOURCE_MANUAL
from app.db.base import get_db
from app.models.media import FileTag
from app.models.media import Tag
from app.models.user import User
from app.schemas.media import Tag as TagSchema
from app.schemas.media import TagBase
from app.schemas.media import TagWithCount
from app.services.tag_collisions import list_tags_filtered
from app.services.tag_collisions import list_unused_tag_rows
from app.services.tag_service import on_tags_changed


@router.post("", response_model=TagSchema)
def create_tag(
    tag_data: TagBase,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Create a new tag owned by the current user.
    """
    tag = _resolve_tag(db, tag_data.name, current_user.id)
    db.commit()
    db.refresh(tag)
    # No file carries it yet, so there is nothing to reindex — but the new row
    # joins the caller's read-through tag list, so that key has to go.
    on_tags_changed(db, user_id=current_user.id)
    return tag


@router.get("", response_model=list[TagWithCount])
def list_tags(
    unused: bool = Query(False, description="Only tags no accessible file carries"),
    colliding: bool = Query(
        False, description="Only tags sharing a normalized name with another tag"
    ),
    scope: Literal["all", "mine", "system", "shared_with_me"] = Query(
        "all",
        description=(
            "Ownership scope. `all` is everything visible; the other three are the "
            "`ownership` values a tag can carry, so a scoped request returns only rows "
            "reporting that same ownership."
        ),
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """List the tags visible to the caller with usage counts, most used first.

    Visible = system tags (``user_id IS NULL``) + the caller's own tags + tags
    attached to a file the caller can access. Usage counts only count accessible
    files, so a shared tag never reveals how often its owner uses it.

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

    filtered = unused or colliding or scope != "all"
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
        unused=unused,
        colliding=colliding,
        scope=scope,
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


@router.delete("/cleanup", response_model=dict[str, Any])
def cleanup_unused_tags(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_active_user)
):
    """Delete every user-owned tag no file anywhere carries (admin only).

    Deliberately **deployment-wide**, unlike ``GET /tags/unused``, which is
    scoped to what the caller can see: this deletes rows, and a tag that merely
    looks unused to one admin may be carrying someone else's files. System tags
    (``user_id IS NULL``) are exempt — they are the shared vocabulary every
    user's picker shows, and being unattached is their normal state, so sweeping
    them would empty the picker for everyone.
    """
    # Only allow admin users to perform this operation
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can clean up unused tags",
        )

    try:
        used_tag_ids = select(FileTag.tag_id).where(FileTag.tag_id.is_not(None))

        delete_query = db.query(Tag).filter(~Tag.id.in_(used_tag_ids), Tag.user_id.is_not(None))

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
        logger.exception(f"Error in cleanup_unused_tags: {e}")
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error cleaning up unused tags: {str(e)}",
        ) from e


@router.post("/files/{file_uuid}/tags", response_model=TagSchema)
def add_tag_to_file(
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

    # Atomically resolve or create the tag (race-condition safe). The tag belongs
    # to the caller, not the file owner: tagging a file shared with you adds the
    # word to YOUR vocabulary, and the owner still sees it because the tag is now
    # attached to a file they can access.
    # Note: file access already verified by get_file_by_uuid_with_permission above
    tag = _resolve_tag(db, tag_base.name, current_user.id)
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

    # Resolve the tag through the file's own attachments. Names are only unique
    # per owner now, so a bare `Tag.name == tag_name` lookup could pick another
    # user's identically-named tag and silently detach nothing.
    tag = (
        db.query(Tag)
        .join(FileTag, FileTag.tag_id == Tag.id)
        .filter(FileTag.media_file_id == file_id, Tag.name == tag_name)
        .first()
    )
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
