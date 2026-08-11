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
from app.services.tag_operations import cleanup_unreferenced_tags
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
    scope: Literal["mine", "all_users"] = Query(
        "mine",
        description=(
            "Whose unused tags to delete. `mine` is the caller's own tags — the "
            "default, because it is the only scope the caller can inspect first. "
            "`all_users` sweeps every account and requires `confirm=true`."
        ),
    ),
    confirm: bool = Query(
        False, description="Required acknowledgement for `scope=all_users`. Ignored otherwise."
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Delete owned tags no file anywhere carries (admin only).

    **Defaults to the caller's own tags.** This used to be unconditionally
    deployment-wide while its inspection sibling ``GET /tags/unused`` is
    caller-scoped, so an admin who read the list and then called this deleted
    rows they were never shown — irreversibly, with no impact preview and no
    parameter that could have warned them. The wide sweep is still available, but
    only by naming it (``scope=all_users``) *and* acknowledging it
    (``confirm=true``), the same shape as ``POST /org-admin/gdpr/erase-organization``.

    "Unused" is measured against every ``file_tag`` row in the deployment, not
    the caller's accessible files, so a tag still attached to a file the caller
    can no longer see survives: ``GET /tags/unused`` may therefore list a tag
    this endpoint declines to delete. That gap is deliberate — the alternative is
    stripping a tag off another account's file. System tags (``user_id IS NULL``)
    are exempt in both scopes; being unattached is their normal state.

    Args:
        scope: ``mine`` (default) or ``all_users``.
        confirm: Must be true for ``scope=all_users``.

    Returns:
        ``deleted_count``, the ``scope`` that was applied, and a message.

    Raises:
        HTTPException: 403 for a non-admin caller, 400 for ``all_users`` without
        ``confirm``, 500 if the sweep fails.
    """
    # Only allow admin users to perform this operation
    if not current_user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can clean up unused tags",
        )

    if scope == "all_users" and not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Deployment-wide tag cleanup deletes other users' tags and cannot be "
                "undone. Retry with confirm=true, or use scope=mine."
            ),
        )

    try:
        count = cleanup_unreferenced_tags(
            db, acting_user_id=current_user.id, all_users=scope == "all_users"
        )
        if count:
            logger.info(f"Deleted {count} unused tags (scope={scope})")

        return {
            "deleted_count": count,
            "scope": scope,
            "message": f"{count} unused tags deleted successfully",
        }
    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
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
    """Attach one tag to a media file, creating the tag if the caller lacks it.

    Consumed by the file-detail and gallery tag pickers; also the single-tag entry
    point for a script or agent labelling an imported library (bulk paths must use
    the batched resolver instead — see ``prepare_upload.add_tags_to_file``).

    Authorized by ``get_file_by_uuid_with_permission``, which applies the sharing,
    takedown and tenant (``ctx.org_id``) gates in one place — write access to the
    *file* is the whole requirement, because tagging does not touch anyone else's
    vocabulary.

    The body is a raw ``dict`` rather than a schema, so a missing ``name`` is
    rejected as 422 by hand. The resolved tag belongs to the **caller**, not the file
    owner: tagging a shared file adds the word to your own vocabulary and the owner
    still sees it because it is now attached to a file they can access.
    ``_resolve_tag`` is race-safe and resolves normalized-exact against the caller's
    own vocabulary plus the system one; it does **not** pass ``file_id``, so it does
    not take ``resolve_or_create_tag``'s "already on this file" branch. The
    duplicate-suppression here is by tag *id* on ``FileTag``, which makes a repeat
    post by the same user idempotent (no second row, no second ``on_tags_changed``)
    but lets two users attach same-named rows of their own to one shared file.
    """
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
