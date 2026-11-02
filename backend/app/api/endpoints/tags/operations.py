"""The destructive half of the tag API: impact preview, promote, rename, merge, delete.

Every route resolves its tags through ``_writable_tag_ids``, so a tag the caller
may not change answers 404 exactly like an unknown one.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.deps_context import refuse_under_support_grant
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.tags._common import _mutation_response
from app.api.endpoints.tags._common import _translated_tag_errors
from app.api.endpoints.tags._common import _writable_tag_ids
from app.api.endpoints.tags._common import _write_rights
from app.api.endpoints.tags._common import router
from app.db.base import get_db
from app.models.user import User
from app.schemas.tag import TagImpact
from app.schemas.tag import TagMergeRequest
from app.schemas.tag import TagMutationResult
from app.schemas.tag import TagPromoteRequest
from app.schemas.tag import TagRenameRequest
from app.services.platform_access import TenancyMode
from app.services.request_context import RequestContext
from app.services.tag_operations import delete_tags
from app.services.tag_operations import merge_tags
from app.services.tag_operations import preview_tag_impact
from app.services.tag_operations import promote_tags_to_shared
from app.services.tag_operations import rename_tag


@router.get("/impact", response_model=TagImpact)
def get_tag_impact(
    tag_uuids: list[UUID] = Query(..., min_length=1, description="The tags to measure."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """How many files a rename, merge or delete of these tags would touch.

    Reports the files you can open and the deployment-wide total separately.
    """
    tag_ids = _writable_tag_ids(
        db,
        tag_uuids,
        user_id=current_user.id,
        can_write_system=ctx.bypass.deployment_scope_writable,
        organization_id=ctx.org_id,
        is_org_admin=ctx.is_org_admin,
    )
    report = preview_tag_impact(db, tag_ids, user_id=current_user.id, organization_id=ctx.org_id)
    return TagImpact.model_validate(report)


@router.post(
    "/promote",
    response_model=TagMutationResult,
    dependencies=[Depends(refuse_under_support_grant)],
)
def promote_tags_endpoint(
    payload: TagPromoteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Publish tags to the shared vocabulary every account sees.

    Same-named tags are folded into the published one: across every tenant in
    a single-tenant deployment, otherwise only within the tag's own tenant.
    """
    if not ctx.bypass.deployment_scope_writable:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admin users can promote tags to the shared vocabulary",
        )
    tag_ids = _writable_tag_ids(
        db,
        payload.tag_uuids,
        user_id=current_user.id,
        can_write_system=ctx.bypass.deployment_scope_writable,
        organization_id=ctx.org_id,
        is_org_admin=ctx.is_org_admin,
    )
    with _translated_tag_errors():
        outcome = promote_tags_to_shared(
            db,
            tag_ids,
            user_id=current_user.id,
            organization_id=ctx.org_id,
            fold_across_tenants=ctx.bypass.mode is TenancyMode.SINGLE,
        )
    return _mutation_response(outcome, _write_rights(ctx, current_user.id))


@router.patch(
    "/{tag_uuid}",
    response_model=TagMutationResult,
    dependencies=[Depends(refuse_under_support_grant)],
)
def rename_tag_endpoint(
    tag_uuid: UUID,
    payload: TagRenameRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Rename a tag.

    When the new name already belongs to another tag nothing changes and
    ``requires_confirmation`` is set, with that tag and the files affected;
    repeat with ``confirm_merge`` to fold this tag into it.
    """
    (tag_id,) = _writable_tag_ids(
        db,
        [tag_uuid],
        user_id=current_user.id,
        can_write_system=ctx.bypass.deployment_scope_writable,
        organization_id=ctx.org_id,
        is_org_admin=ctx.is_org_admin,
    )
    with _translated_tag_errors():
        outcome = rename_tag(
            db,
            tag_id,
            payload.name,
            confirm_merge=payload.confirm_merge,
            user_id=current_user.id,
            organization_id=ctx.org_id,
        )
    return _mutation_response(outcome, _write_rights(ctx, current_user.id))


@router.post(
    "/{tag_uuid}/merge",
    response_model=TagMutationResult,
    dependencies=[Depends(refuse_under_support_grant)],
)
def merge_tags_endpoint(
    tag_uuid: UUID,
    payload: TagMergeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Fold the source tags into this tag; their files and grants move to it."""
    (target_id,) = _writable_tag_ids(
        db,
        [tag_uuid],
        user_id=current_user.id,
        can_write_system=ctx.bypass.deployment_scope_writable,
        organization_id=ctx.org_id,
        is_org_admin=ctx.is_org_admin,
    )
    source_ids = _writable_tag_ids(
        db,
        payload.source_uuids,
        user_id=current_user.id,
        can_write_system=ctx.bypass.deployment_scope_writable,
        organization_id=ctx.org_id,
        is_org_admin=ctx.is_org_admin,
    )
    with _translated_tag_errors():
        outcome = merge_tags(
            db, target_id, source_ids, user_id=current_user.id, organization_id=ctx.org_id
        )
    return _mutation_response(outcome, _write_rights(ctx, current_user.id))


@router.delete(
    "",
    response_model=TagMutationResult,
    dependencies=[Depends(refuse_under_support_grant)],
)
def delete_tags_endpoint(
    tag_uuids: list[UUID] = Query(..., min_length=1, description="The tags to delete."),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Delete tags. Their files lose them and their share grants go too."""
    tag_ids = _writable_tag_ids(
        db,
        tag_uuids,
        user_id=current_user.id,
        can_write_system=ctx.bypass.deployment_scope_writable,
        organization_id=ctx.org_id,
        is_org_admin=ctx.is_org_admin,
    )
    with _translated_tag_errors():
        outcome = delete_tags(db, tag_ids, user_id=current_user.id, organization_id=ctx.org_id)
    return _mutation_response(outcome, _write_rights(ctx, current_user.id))
