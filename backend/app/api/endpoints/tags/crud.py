"""Create and list tags, sweep unused ones, and attach or detach a tag on one file."""

from __future__ import annotations

import logging
from typing import Any
from typing import Literal

from fastapi import Body
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.deps_context import refuse_under_support_grant
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.tags._common import _invalid_name
from app.api.endpoints.tags._common import _tag_not_found
from app.api.endpoints.tags._common import _tag_response
from app.api.endpoints.tags._common import _writable_uuids
from app.api.endpoints.tags._common import _write_rights
from app.api.endpoints.tags._common import router
from app.core.config import settings
from app.core.exceptions import DomainError
from app.db.base import get_db
from app.models.user import User
from app.schemas.tag import Tag as TagSchema
from app.schemas.tag import TagBase
from app.schemas.tag import TagWithCount
from app.services.redis_cache_service import TTL_TAGS
from app.services.redis_cache_service import redis_cache
from app.services.request_context import RequestContext
from app.services.tag_bulk import REQUIRED_PERMISSION
from app.services.tag_collisions import SCOPE_ALL
from app.services.tag_collisions import list_tags_filtered
from app.services.tag_collisions import list_unused_tag_rows
from app.services.tag_files import add_file_tag
from app.services.tag_files import attached_tag_named
from app.services.tag_files import remove_file_tag
from app.services.tag_operations import cleanup_unreferenced_tags
from app.services.tag_service import InvalidTagNameError
from app.services.tag_service import on_tags_changed
from app.services.tag_service import resolve_or_create_tag
from app.services.uuid_helpers import get_file_by_uuid_with_permission

logger = logging.getLogger(__name__)

CLEANUP_FAILED = "Could not clean up unused tags."


@router.post("", response_model=TagSchema, dependencies=[Depends(refuse_under_support_grant)])
def create_tag(
    tag_in: TagBase,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> TagSchema:
    """Create a tag in the request tenant, or return the one the name already resolves to.

    Case, ``-``/``_`` and spacing variants resolve onto the existing tag; a
    merely similar name creates a new one. Names are clamped to 50 characters.
    """
    try:
        tag = resolve_or_create_tag(
            db, tag_in.name, user_id=current_user.id, organization_id=ctx.org_id
        )
    except InvalidTagNameError as exc:
        raise _invalid_name() from exc
    db.commit()
    db.refresh(tag)
    on_tags_changed(db, user_id=current_user.id)
    return _tag_response(tag, _write_rights(ctx, current_user.id))


@router.get("", response_model=list[TagWithCount])
def list_tags(
    unused: bool = Query(False, description="Only tags on none of your accessible files."),
    colliding: bool = Query(
        False, description="Only tags whose name normalizes like another tag's."
    ),
    scope: Literal["all", "mine", "system", "shared_with_me"] = Query(
        "all", description="Narrow to one ownership kind."
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> list[TagWithCount] | list[dict[str, Any]]:
    """The tags you can see, most used first, with how many of your files carry each.

    The unfiltered personal list is served from a short-lived cache.
    """
    rights = _write_rights(ctx, current_user.id)
    cacheable = (
        settings.READ_CACHE_ENABLED
        and ctx.org_id is None
        and not unused
        and not colliding
        and scope == SCOPE_ALL
        # Rows carry the request's write rights; refused ones must not reach the user's cache.
        and not rights.refused
    )
    cache_key = f"cache:tags:{current_user.id}"
    if cacheable:
        cached: list[dict[str, Any]] | None = redis_cache.get(cache_key)
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
    writable = _writable_uuids(db, [entry.uuid for entry in entries], rights)
    rows = [TagWithCount.model_validate(entry, from_attributes=True) for entry in entries]
    for row in rows:
        row.can_write = row.uuid in writable
    if cacheable:
        redis_cache.set(cache_key, [row.model_dump(mode="json") for row in rows], ttl=TTL_TAGS)
    return rows


@router.get("/unused", response_model=list[TagSchema])
def list_unused_tags(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> list[TagSchema]:
    """Your tenant's tags and system tags that none of your accessible files carry."""
    rows = list_unused_tag_rows(db, user_id=current_user.id, organization_id=ctx.org_id)
    rights = _write_rights(ctx, current_user.id)
    return [_tag_response(tag, rights) for tag in rows]


@router.delete(
    "/cleanup",
    response_model=dict[str, Any],
    dependencies=[Depends(refuse_under_support_grant)],
)
def cleanup_unused_tags(
    scope: Literal["mine", "all_users"] = Query(
        "mine", description="Your own tags in this workspace, or every account's."
    ),
    confirm: bool = Query(False, description="Required for scope=all_users."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> dict[str, Any]:
    """Delete tags that no file carries. Admin only; system tags are always kept."""
    if not ctx.bypass.user_is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can clean up unused tags",
        )
    all_users = scope == "all_users"
    if all_users and not ctx.bypass.deployment_scope_writable:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Deployment-wide tag cleanup is not available to this account",
        )
    if all_users and not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Deployment-wide tag cleanup deletes other users' tags and cannot be "
                "undone. Retry with confirm=true, or use scope=mine."
            ),
        )
    try:
        deleted = cleanup_unreferenced_tags(
            db,
            acting_user_id=current_user.id,
            organization_id=ctx.org_id,
            all_users=all_users,
        )
    except (HTTPException, DomainError):
        raise
    except Exception as exc:
        logger.exception(f"Unused tag cleanup failed: {exc}")
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=CLEANUP_FAILED
        ) from exc
    return {
        "deleted_count": deleted,
        "scope": scope,
        "message": f"{deleted} unused tags deleted successfully",
    }


@router.post(
    "/files/{file_uuid}/tags",
    response_model=TagSchema,
    dependencies=[Depends(refuse_under_support_grant)],
)
def add_tag_to_file(
    file_uuid: str,
    tag_data: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> TagSchema:
    """Attach a tag to a file by name, creating the tag in the file's tenant if needed.

    Needs editor rights on the file. A file you cannot open is answered like an
    unknown uuid. Attaching a tag the file already carries changes nothing.
    """
    media_file = get_file_by_uuid_with_permission(
        db,
        file_uuid,
        current_user.id,
        bypass=ctx.bypass,
        organization_id=ctx.org_id,
        min_permission=REQUIRED_PERMISSION,
        conceal_inaccessible=True,
    )
    if "name" not in tag_data:
        raise _invalid_name()
    requested = TagBase(name=tag_data["name"])
    try:
        tag = resolve_or_create_tag(
            db,
            requested.name,
            user_id=current_user.id,
            organization_id=media_file.organization_id,
        )
    except InvalidTagNameError as exc:
        raise _invalid_name() from exc

    file_id = media_file.id
    added = add_file_tag(db, file_id, tag.id)
    response = _tag_response(tag, _write_rights(ctx, current_user.id))
    db.commit()
    if added:
        on_tags_changed(db, [file_id], user_id=current_user.id)
    return response


@router.delete(
    "/files/{file_uuid}/tags/{tag_name}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(refuse_under_support_grant)],
)
def remove_tag_from_file(
    file_uuid: str,
    tag_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> None:
    """Detach the tag with exactly this stored name from a file; the tag itself is kept.

    Needs editor rights on the file. A file you cannot open is answered like an
    unknown uuid.
    """
    media_file = get_file_by_uuid_with_permission(
        db,
        file_uuid,
        current_user.id,
        bypass=ctx.bypass,
        organization_id=ctx.org_id,
        min_permission=REQUIRED_PERMISSION,
        conceal_inaccessible=True,
    )
    file_id = media_file.id
    tag = attached_tag_named(db, file_id, tag_name)
    if tag is None:
        raise _tag_not_found()
    logger.info(f"Detaching tag {tag.uuid} from file {media_file.uuid}; the tag row is kept")
    remove_file_tag(db, file_id, tag.id)
    db.commit()
    on_tags_changed(db, [file_id], user_id=current_user.id)
