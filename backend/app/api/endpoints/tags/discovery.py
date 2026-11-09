"""Read-only tag questions: name collisions, what a selection carries, what a tag is on."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import Depends
from fastapi import Query
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.tags._common import _tag_not_found
from app.api.endpoints.tags._common import _writable_uuids
from app.api.endpoints.tags._common import _write_rights
from app.api.endpoints.tags._common import router
from app.db.base import get_db
from app.models.user import User
from app.schemas.tag import Tag as TagSchema
from app.schemas.tag import TagCollisionCluster
from app.schemas.tag import TagFileList
from app.schemas.tag import TaggedFile
from app.schemas.tag import TagOnSelection
from app.services.file_availability import unavailable_list_exempt
from app.services.formatting_service import FormattingService
from app.services.hidden_media_service import HIDDEN_MODE_PATTERN
from app.services.request_context import RequestContext
from app.services.tag_collisions import files_for_tag
from app.services.tag_collisions import find_tag_collisions
from app.services.tag_collisions import tags_on_files
from app.services.tag_scope import tag_ownership
from app.services.tag_scope import visible_tag_id
from app.services.uuid_helpers import get_file_by_uuid_with_permission


@router.get("/collisions", response_model=list[TagCollisionCluster])
def list_tag_collisions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> list[TagCollisionCluster]:
    """Groups of tags whose names differ only by case, separators or spacing.

    Each group names the member a merge should keep, and lists similar but
    distinct names as suggestions. Repairs stale stored normalizations first.
    """
    clusters = find_tag_collisions(db, user_id=current_user.id, organization_id=ctx.org_id)
    responses = [TagCollisionCluster.model_validate(cluster) for cluster in clusters]
    listed: list[TagSchema] = [
        tag for response in responses for tag in [*response.members, *response.suggestions]
    ]
    writable = _writable_uuids(
        db, [tag.uuid for tag in listed], _write_rights(ctx, current_user.id)
    )
    for tag in listed:
        tag.can_write = tag.uuid in writable
    return responses


@router.get("/for-files", response_model=list[TagOnSelection])
def list_tags_on_files(
    file_uuids: list[UUID] = Query(
        ..., min_length=1, description="The selected files; repeat the parameter for each."
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> list[TagOnSelection]:
    """The tags a selection of files carries, and on how many of them.

    Any file in the selection you cannot open fails the whole request exactly
    like an unknown uuid.
    """
    selected = list(dict.fromkeys(file_uuids))
    file_ids = [
        get_file_by_uuid_with_permission(
            db,
            file_uuid,
            current_user.id,
            bypass=ctx.bypass,
            organization_id=ctx.org_id,
            conceal_inaccessible=True,
        ).id
        for file_uuid in selected
    ]
    rows = tags_on_files(db, file_ids, user_id=current_user.id, organization_id=ctx.org_id)
    rights = _write_rights(ctx, current_user.id)
    return [
        TagOnSelection(
            uuid=tag.uuid,
            name=tag.name,
            source=tag.source,
            ownership=tag_ownership(tag, current_user.id),
            can_write=rights.permits(tag),
            file_count=count,
            selection_size=len(file_ids),
        )
        for tag, count in rows
    ]


def _status_text(value: Any) -> str | None:
    """A status enum's wire value; a raw string passes through."""
    text: str | None = getattr(value, "value", value)
    return text


@router.get("/{tag_uuid}/files", response_model=TagFileList)
def list_files_for_tag(
    tag_uuid: UUID,
    limit: int = Query(50, ge=1, le=200, description="Most files to return."),
    hidden: str = Query(
        "exclude",
        pattern=HIDDEN_MODE_PATTERN,
        description="Your hidden files: 'exclude' (default), 'include', or 'only' (issue #1102)",
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
) -> TagFileList:
    """Your accessible files that carry a tag, newest first, with the true total."""
    tag_id = visible_tag_id(db, tag_uuid, current_user.id, ctx.org_id)
    if tag_id is None:
        raise _tag_not_found()
    files, total = files_for_tag(
        db,
        tag_id,
        user_id=current_user.id,
        organization_id=ctx.org_id,
        limit=limit,
        include_quarantined=unavailable_list_exempt(ctx.bypass),
        hidden=hidden,
    )
    return TagFileList(
        total=total,
        files=[
            TaggedFile(
                uuid=media_file.uuid,
                display_title=media_file.title or media_file.filename or str(media_file.uuid),
                status=_status_text(media_file.status),
                formatted_duration=FormattingService.format_duration(media_file.duration),
            )
            for media_file in files
        ],
    )
