import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Body
from fastapi import Depends
from fastapi import HTTPException
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
from app.models.media import MediaFile
from app.models.media import Tag
from app.models.user import User
from app.schemas.media import Tag as TagSchema
from app.schemas.media import TagBase
from app.schemas.media import TagWithCount
from app.services.tag_service import InvalidTagNameError
from app.services.tag_service import resolve_or_create_tag

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
    return tag


@router.get("", response_model=list[TagWithCount])
def list_tags(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """
    List all available tags for the current user with usage counts, sorted by most used.

    Read-through cached in Redis (``cache:tags:{user_id}``) behind
    ``READ_CACHE_ENABLED``. The endpoint takes no parameters, so a single
    per-user key is exact FOR PERSONAL SCOPE. Org-context requests bypass the
    cache entirely: the key is scope-blind and ``invalidate_tags`` deletes only
    the exact per-user key, so caching org-scoped results would either leak
    across scopes or go stale. Every tag/file-tag mutation path busts this key
    (see the redaction audit in the Phase-8 commit body), so reads are always
    fresh.
    """
    from app.core.config import settings as app_settings
    from app.services.redis_cache_service import TTL_TAGS
    from app.services.redis_cache_service import redis_cache

    cache_key = f"cache:tags:{current_user.id}"
    use_cache = app_settings.READ_CACHE_ENABLED and ctx.org_id is None

    if use_cache:
        cached = redis_cache.get(cache_key)
        if cached is not None:
            return cached

    try:
        from sqlalchemy import select

        from app.services.permission_service import PermissionService

        # Get tags with usage counts for files accessible by this user
        # (owned + shared, tenant-gated via ctx.org_id)
        accessible_sq = PermissionService.get_accessible_file_ids_subquery(
            db, current_user.id, organization_id=ctx.org_id
        )
        tag_counts = (
            db.query(Tag, func.count(FileTag.id).label("usage_count"))
            .outerjoin(FileTag)
            .outerjoin(MediaFile)
            .filter((MediaFile.id.in_(select(accessible_sq))) | (MediaFile.id.is_(None)))
            .group_by(Tag.id)
            .order_by(func.count(FileTag.id).desc(), Tag.name)
            .all()
        )

        # Convert to TagWithCount objects
        tags_with_counts = []
        for tag, count in tag_counts:
            tags_with_counts.append(
                TagWithCount(uuid=tag.uuid, name=tag.name, source=tag.source, usage_count=count)
            )

        if use_cache:
            # Cache the post-Pydantic dicts (never ORM objects).
            payload = [t.model_dump(mode="json") for t in tags_with_counts]
            redis_cache.set(cache_key, payload, ttl=TTL_TAGS)

        return tags_with_counts
    except Exception as e:
        logger.error(f"Error in list_tags: {e}")
        # If there's an error, return an empty list
        return []


@router.get("/unused", response_model=list[TagSchema])
def list_unused_tags(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_active_user)
):
    """
    List unused tags that are not associated with any files
    """
    try:
        # Find all tags that are not used in any files
        # We use a subquery to find tag IDs that are in use
        used_tag_ids = db.query(FileTag.tag_id).distinct().subquery()

        # Then find all tags not in that list
        unused_tags = db.query(Tag).filter(~Tag.id.in_(used_tag_ids)).all()  # type: ignore[arg-type]

        return unused_tags
    except Exception as e:
        logger.error(f"Error in list_unused_tags: {e}")
        return []


@router.delete("/cleanup", response_model=dict[str, Any])
def cleanup_unused_tags(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_active_user)
):
    """
    Delete all unused tags to clean up the database
    (Admin users only)
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

        # Invalidate caches
        try:
            from app.services.redis_cache_service import redis_cache

            redis_cache.invalidate_tags(current_user.id)
            redis_cache.invalidate_user_files(current_user.id)
        except Exception as e:
            logger.debug(f"Cache invalidation failed (non-critical): {e}")

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

        # Invalidate caches
        try:
            from app.services.redis_cache_service import redis_cache

            redis_cache.invalidate_tags(current_user.id)
            redis_cache.invalidate_user_files(current_user.id)
        except Exception as e:
            logger.debug(f"Cache invalidation failed (non-critical): {e}")

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
