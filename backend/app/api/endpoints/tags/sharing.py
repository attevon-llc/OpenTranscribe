"""Granting a tag to specific users and groups (v386).

Shared helpers and the visibility rules live in ``_common``.
"""

from uuid import UUID

from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.deps_context import refuse_under_support_grant
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.tags._common import _share_target
from app.api.endpoints.tags._common import _writable_tag_ids
from app.api.endpoints.tags._common import router
from app.auth.audit import TENANT_SCOPE_ORG
from app.auth.audit import TENANT_SCOPE_PERSONAL
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.audit import request_org_id
from app.db.base import get_db
from app.middleware.audit import get_request_context
from app.models.media import Tag
from app.models.sharing import TagShare
from app.models.user import User
from app.schemas.media import TagShareCreate
from app.schemas.media import TagShareTarget
from app.services.tag_service import on_tags_changed
from app.services.tag_sharing import TagShareError
from app.services.tag_sharing import list_shares
from app.services.tag_sharing import revoke_share
from app.services.tag_sharing import share_tag
from app.utils.uuid_helpers import get_by_uuid


def _audit_tag_share(
    request: Request,
    actor: User,
    tag: Tag,
    event_type: AuditEventType,
    *,
    target_user_id: int | None,
    target_group_id: int | None,
) -> None:
    """Write the share / unshare audit event for a tag.

    ⚠️ Never put the tag's NAME in the event. A tag name is user text and can be sensitive (a
    client, a diagnosis, a case), and an audit log is read by people who may not see the tag.
    The record says WHO shared WHICH tag (its uuid) with WHOM, which is what an audit needs;
    collections already worked this way (issue #1307).
    """
    req_ctx = get_request_context(request)
    audit_logger.log(
        event_type=event_type,
        outcome=AuditOutcome.SUCCESS,
        user_id=actor.id,
        username=str(actor.email),
        source_ip=req_ctx["source_ip"],
        user_agent=req_ctx["user_agent"],
        organization_id=request_org_id(request),
        target_user_id=target_user_id,
        resource_type="tag",
        resource_uuid=str(tag.uuid),
        tenant_scope=TENANT_SCOPE_ORG if tag.organization_id is not None else TENANT_SCOPE_PERSONAL,
        details={"target_group_id": target_group_id},
    )


@router.get("/{tag_uuid}/shares", response_model=list[TagShareTarget])
def list_tag_shares(
    tag_uuid: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Who this tag is shared with. Owner (or admin, for a system tag) only.

    Reuses the writable gate: who you have given a tag to is the owner's
    business, not every recipient's.
    """
    tag_id = _writable_tag_ids(
        db,
        [tag_uuid],
        user_id=current_user.id,
        is_admin=current_user.is_admin,
        organization_id=ctx.org_id,
        is_org_admin=ctx.is_org_admin,
    )[0]
    return [_share_target(share) for share in list_shares(db, tag_id)]


@router.post(
    "/{tag_uuid}/shares",
    response_model=TagShareTarget,
    dependencies=[Depends(refuse_under_support_grant)],
)
def create_tag_share(
    tag_uuid: UUID,
    payload: TagShareCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Share a tag with one user or one group.

    The middle tier between "mine alone" and "published to the whole
    deployment": the recipient can see the tag, filter by it and apply it, so
    they use your word instead of coining a duplicate. Renaming, merging and
    deleting stay with you.
    """
    tag_id = _writable_tag_ids(
        db,
        [tag_uuid],
        user_id=current_user.id,
        is_admin=current_user.is_admin,
        organization_id=ctx.org_id,
        is_org_admin=ctx.is_org_admin,
    )[0]
    tag = db.query(Tag).filter(Tag.id == tag_id).one()

    target_user_id = None
    target_group_id = None
    if payload.target_user_uuid is not None:
        target_user_id = get_by_uuid(
            db, User, payload.target_user_uuid, error_message="User not found"
        ).id
    if payload.target_group_uuid is not None:
        from app.models.group import UserGroup

        target_group_id = get_by_uuid(
            db, UserGroup, payload.target_group_uuid, error_message="Group not found"
        ).id

    try:
        share = share_tag(
            db,
            tag,
            shared_by_id=current_user.id,
            target_user_id=target_user_id,
            target_group_id=target_group_id,
        )
    except TagShareError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    on_tags_changed(db, user_id=current_user.id)

    _audit_tag_share(
        request,
        current_user,
        tag,
        AuditEventType.RESOURCE_SHARE,
        target_user_id=target_user_id,
        target_group_id=target_group_id,
    )

    return _share_target(share)


@router.delete(
    "/{tag_uuid}/shares/{share_uuid}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(refuse_under_support_grant)],
)
def revoke_tag_share(
    tag_uuid: UUID,
    share_uuid: UUID,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Revoke one grant.

    The tag and every association survive: a recipient who applied it to their
    own files keeps those files tagged, and still sees the word on their own
    media through the file arm of ``visible_to``. Only reaching it by name in
    the picker goes away.
    """
    tag_id = _writable_tag_ids(
        db,
        [tag_uuid],
        user_id=current_user.id,
        is_admin=current_user.is_admin,
        organization_id=ctx.org_id,
        is_org_admin=ctx.is_org_admin,
    )[0]
    # Captured before revoke_share() deletes the row -- the audit event needs to
    # say WHO the grant was to, and that information is gone once it's gone.
    share = (
        db.query(TagShare).filter(TagShare.tag_id == tag_id, TagShare.uuid == share_uuid).first()
    )
    if share is None or not revoke_share(db, tag_id, share_uuid):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Share not found")
    on_tags_changed(db, user_id=current_user.id)

    tag = db.query(Tag).filter(Tag.id == tag_id).one()
    _audit_tag_share(
        request,
        current_user,
        tag,
        AuditEventType.RESOURCE_UNSHARE,
        target_user_id=share.target_user_id,
        target_group_id=share.target_group_id,
    )
