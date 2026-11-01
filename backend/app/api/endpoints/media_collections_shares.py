"""Collection sharing routes: list, create, update and revoke the shares of a collection.

Owner-only management, the organization gate on share targets, share notifications, the
search access reindex and the share audit events. Handlers are plain ``def`` on purpose: see
the ``media_collections`` module docstring (issue #284 A2.5).
"""

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status
from sqlalchemy import exists
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload

from app.api.deps_context import get_current_context
from app.api.deps_context import refuse_under_support_grant
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.media_collections_access import UNKNOWN_TIMESTAMP
from app.api.endpoints.media_collections_access import _tenant_scope
from app.auth.audit import audit_logger
from app.auth.audit_events import AuditEventType
from app.auth.audit_events import AuditOutcome
from app.core.constants import NOTIFICATION_TYPE_COLLECTION_SHARE_REVOKED
from app.core.constants import NOTIFICATION_TYPE_COLLECTION_SHARE_UPDATED
from app.core.constants import NOTIFICATION_TYPE_COLLECTION_SHARED
from app.db.base import get_db
from app.middleware.audit import get_request_context
from app.models.collection import Collection
from app.models.collection import CollectionMember
from app.models.group import UserGroup
from app.models.group import UserGroupMember
from app.models.organization import OrganizationMembership
from app.models.sharing import CollectionShare
from app.models.user import User
from app.schemas.sharing import Share
from app.schemas.sharing import ShareCreate
from app.schemas.sharing import ShareUpdate
from app.schemas.user import UserBrief
from app.services.permission_service import PermissionService
from app.services.request_context import RequestContext
from app.services.uuid_helpers import get_by_uuid
from app.services.uuid_helpers import get_collection_by_uuid_with_permission
from app.tasks.search_file_field_tasks import update_file_access_index
from app.utils.websocket_notify import send_ws_event

router = APIRouter()


def _get_share_target_user_ids(
    db: Session, share: CollectionShare, collection: Collection
) -> list[int]:
    """Return the user IDs affected by a share.

    For user-targeted shares this is a single-element list.
    For group-targeted shares this is all group members.

    For an organization collection, only members of that organization are
    returned: a group can outlive (or predate) a member's time in the
    organization, and such a user must not be told about the org's collection.
    """
    if share.target_type == "user" and share.target_user_id:
        query = db.query(User.id).filter(User.id == share.target_user_id)
    elif share.target_type == "group" and share.target_group_id:
        query = db.query(UserGroupMember.user_id).filter(
            UserGroupMember.group_id == share.target_group_id
        )
    else:
        return []
    if collection.organization_id is not None:
        user_col = User.id if share.target_type == "user" else UserGroupMember.user_id
        query = query.filter(
            exists().where(
                OrganizationMembership.user_id == user_col,
                OrganizationMembership.organization_id == collection.organization_id,
            )
        )
    return [int(row[0]) for row in query.all()]


def _notify_share_event(
    db: Session,
    share: CollectionShare,
    collection: Collection,
    notification_type: str,
    extra_data: dict | None = None,
) -> None:
    """Send a WebSocket notification for a sharing event to all affected users."""
    target_ids = _get_share_target_user_ids(db, share, collection)
    data: dict = {
        "collection_uuid": str(collection.uuid),
        "collection_name": collection.name,
        "share_uuid": str(share.uuid),
        "permission": share.permission,
        "message": _share_message(notification_type, collection.name),
    }
    if extra_data:
        data.update(extra_data)

    for uid in target_ids:
        send_ws_event(uid, notification_type, data)


def _share_message(notification_type: str, collection_name: str) -> str:
    """Build a human-readable message for a share notification."""
    if notification_type == NOTIFICATION_TYPE_COLLECTION_SHARED:
        return f"Collection '{collection_name}' has been shared with you"
    if notification_type == NOTIFICATION_TYPE_COLLECTION_SHARE_REVOKED:
        return f"Your access to collection '{collection_name}' has been revoked"
    if notification_type == NOTIFICATION_TYPE_COLLECTION_SHARE_UPDATED:
        return f"Your permissions on collection '{collection_name}' have been updated"
    return "Collection sharing update"


def _build_share_response(db: Session, share: CollectionShare) -> Share:
    """Build a Share response from a CollectionShare record."""
    shared_by_brief = UserBrief(
        uuid=share.shared_by.uuid,
        full_name=share.shared_by.full_name,
        email=share.shared_by.email,
    )

    if share.target_type == "user" and share.target_user:
        target_uuid = share.target_user.uuid
        target_name = share.target_user.full_name or share.target_user.email
        target_email = share.target_user.email
        member_count = None
    elif share.target_type == "group" and share.target_group:
        target_uuid = share.target_group.uuid
        target_name = share.target_group.name
        target_email = None
        member_count = (
            db.query(UserGroupMember)
            .filter(UserGroupMember.group_id == share.target_group.id)
            .count()
        )
    else:
        raise HTTPException(status_code=500, detail="Invalid share target")

    return Share(
        uuid=share.uuid,
        target_type=share.target_type,
        target_uuid=target_uuid,
        target_name=target_name,
        target_email=target_email,
        member_count=member_count,
        permission=share.permission,
        shared_by=shared_by_brief,
        created_at=share.created_at or UNKNOWN_TIMESTAMP,
    )


def _require_collection_owner(db: Session, collection: Collection, user_id: int) -> None:
    """Require that the user owns the collection.

    Only an owner may manage shares: the owner of a personal collection, or the
    creator / an org admin of an organization collection (v422). Users who
    received "editor" permission via a share — or as an ordinary org member —
    cannot re-share.
    """
    if PermissionService.get_collection_permission(db, collection.id, user_id) != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the collection owner can manage sharing",
        )


@router.get("/{collection_uuid}/shares", response_model=list[Share])
def list_collection_shares(
    collection_uuid: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """List all shares on a collection. Requires direct collection ownership. Tenant-gated (#262d)."""
    collection = get_collection_by_uuid_with_permission(
        db, collection_uuid, current_user.id, organization_id=ctx.org_id
    )
    _require_collection_owner(db, collection, current_user.id)

    shares = (
        db.query(CollectionShare)
        .options(
            joinedload(CollectionShare.shared_by),
            joinedload(CollectionShare.target_user),
            joinedload(CollectionShare.target_group),
        )
        .filter(CollectionShare.collection_id == collection.id)
        .order_by(CollectionShare.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )

    return [_build_share_response(db, share) for share in shares]


def _user_share_target_id(
    db: Session, collection: Collection, share_in: ShareCreate, current_user: User
) -> int:
    """The user a user-targeted share goes to; refuses the sharer, an outsider, a duplicate."""
    target_user = get_by_uuid(db, User, str(share_in.target_uuid), "User not found")

    # Cannot share with yourself
    if target_user.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot share a collection with yourself",
        )

    # Tenant gate: an org-scoped collection may only be shared with active
    # members of that organization — cross-org shares would otherwise leak
    # org content to outsiders. Personal collections (org NULL) keep the
    # existing behavior (shareable with any user).
    if collection.organization_id is not None:
        from app.models.organization import OrganizationMembership

        membership = (
            db.query(OrganizationMembership)
            .filter(
                OrganizationMembership.organization_id == collection.organization_id,
                OrganizationMembership.user_id == target_user.id,
            )
            .first()
        )
        if membership is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Cannot share an organization collection with a user who is "
                    "not a member of that organization"
                ),
            )

    # Check for existing share
    existing = (
        db.query(CollectionShare)
        .filter(
            CollectionShare.collection_id == collection.id,
            CollectionShare.target_user_id == target_user.id,
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Collection is already shared with this user",
        )

    return target_user.id


def _group_share_target_id(
    db: Session, collection: Collection, share_in: ShareCreate, current_user: User
) -> int:
    """The group a group-targeted share goes to.

    Refuses a sharer outside the group, a group with a member outside the collection's
    organization, and a duplicate share.
    """
    target_group = get_by_uuid(db, UserGroup, str(share_in.target_uuid), "Group not found")

    # Verify the sharer is a member of the target group
    is_member = (
        db.query(UserGroupMember)
        .filter(
            UserGroupMember.group_id == target_group.id,
            UserGroupMember.user_id == current_user.id,
        )
        .first()
    )
    if not is_member:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must be a member of the group to share with it",
        )

    # Tenant gate (issue #262d): groups can span organizations, so a
    # group-targeted share of an org-stamped collection requires EVERY
    # group member to hold membership in that org — otherwise the share
    # grants the non-member(s) a view onto the tenant's content.
    if collection.organization_id is not None:
        from app.models.organization import OrganizationMembership

        group_member_ids = {
            int(row[0])
            for row in db.query(UserGroupMember.user_id)
            .filter(UserGroupMember.group_id == target_group.id)
            .all()
        }
        org_member_ids = {
            int(row[0])
            for row in db.query(OrganizationMembership.user_id)
            .filter(OrganizationMembership.organization_id == collection.organization_id)
            .all()
        }
        outside = group_member_ids - org_member_ids
        if outside:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Cannot share an organization collection with this group: "
                    f"{len(outside)} group member(s) are not members of that "
                    "organization"
                ),
            )

    # Check for existing share
    existing = (
        db.query(CollectionShare)
        .filter(
            CollectionShare.collection_id == collection.id,
            CollectionShare.target_group_id == target_group.id,
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Collection is already shared with this group",
        )

    return target_group.id


@router.post(
    "/{collection_uuid}/shares",
    response_model=Share,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(refuse_under_support_grant)],
)
def create_collection_share(
    collection_uuid: str,
    share_in: ShareCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Share a collection with a user or group. Requires direct collection ownership. Tenant-gated (#262d)."""
    collection = get_collection_by_uuid_with_permission(
        db, collection_uuid, current_user.id, organization_id=ctx.org_id
    )
    _require_collection_owner(db, collection, current_user.id)

    target_user_id = None
    target_group_id = None

    if share_in.target_type == "user":
        target_user_id = _user_share_target_id(db, collection, share_in, current_user)

    elif share_in.target_type == "group":
        target_group_id = _group_share_target_id(db, collection, share_in, current_user)

    share = CollectionShare(
        collection_id=collection.id,
        shared_by_id=current_user.id,
        target_type=share_in.target_type,
        target_user_id=target_user_id,
        target_group_id=target_group_id,
        permission=share_in.permission,
    )
    db.add(share)
    db.commit()
    db.refresh(share)

    # Reload with relationships
    reloaded_share = (
        db.query(CollectionShare)
        .options(
            joinedload(CollectionShare.shared_by),
            joinedload(CollectionShare.target_user),
            joinedload(CollectionShare.target_group),
        )
        .filter(CollectionShare.id == share.id)
        .first()
    )
    assert reloaded_share is not None  # just committed above
    share = reloaded_share

    # Reindex OpenSearch accessible_user_ids for files in this collection
    file_ids = [
        cm.media_file_id
        for cm in db.query(CollectionMember.media_file_id)
        .filter(CollectionMember.collection_id == collection.id)
        .all()
    ]
    if file_ids:
        update_file_access_index.delay(file_ids)

    # Notify affected user(s) about the new share
    _notify_share_event(db, share, collection, NOTIFICATION_TYPE_COLLECTION_SHARED)

    req_ctx = get_request_context(request)
    audit_logger.log(
        event_type=AuditEventType.RESOURCE_SHARE,
        outcome=AuditOutcome.SUCCESS,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=req_ctx["source_ip"],
        user_agent=req_ctx["user_agent"],
        organization_id=collection.organization_id,
        target_user_id=target_user_id,
        resource_type="collection",
        resource_uuid=str(collection.uuid),
        tenant_scope=_tenant_scope(collection),
        details={
            "target_group_id": target_group_id,
            "permission": share_in.permission,
        },
    )

    return _build_share_response(db, share)


@router.put(
    "/{collection_uuid}/shares/{share_uuid}",
    response_model=Share,
    dependencies=[Depends(refuse_under_support_grant)],
)
def update_collection_share(
    collection_uuid: str,
    share_uuid: str,
    share_update: ShareUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Update a share's permission level. Requires direct collection ownership. Tenant-gated (#262d)."""
    collection = get_collection_by_uuid_with_permission(
        db, collection_uuid, current_user.id, organization_id=ctx.org_id
    )
    _require_collection_owner(db, collection, current_user.id)

    share = get_by_uuid(db, CollectionShare, share_uuid, "Share not found")

    # Verify share belongs to this collection
    if share.collection_id != collection.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Share not found on this collection",
        )

    previous_permission = share.permission
    share.permission = share_update.permission
    db.commit()
    db.refresh(share)

    # Reload with relationships
    reloaded_share = (
        db.query(CollectionShare)
        .options(
            joinedload(CollectionShare.shared_by),
            joinedload(CollectionShare.target_user),
            joinedload(CollectionShare.target_group),
        )
        .filter(CollectionShare.id == share.id)
        .first()
    )
    assert reloaded_share is not None  # just committed above
    share = reloaded_share

    # Reindex files since permission level changed
    file_ids = [
        cm.media_file_id
        for cm in db.query(CollectionMember.media_file_id)
        .filter(CollectionMember.collection_id == collection.id)
        .all()
    ]
    if file_ids:
        update_file_access_index.delay(file_ids)

    # Notify affected user(s) about the permission change
    _notify_share_event(db, share, collection, NOTIFICATION_TYPE_COLLECTION_SHARE_UPDATED)

    req_ctx = get_request_context(request)
    audit_logger.log(
        event_type=AuditEventType.RESOURCE_SHARE,
        outcome=AuditOutcome.SUCCESS,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=req_ctx["source_ip"],
        user_agent=req_ctx["user_agent"],
        organization_id=collection.organization_id,
        target_user_id=share.target_user_id,
        resource_type="collection",
        resource_uuid=str(collection.uuid),
        tenant_scope=_tenant_scope(collection),
        details={
            "target_group_id": share.target_group_id,
            "action": "permission_update",
            "previous_permission": previous_permission,
            "permission": share.permission,
        },
    )

    return _build_share_response(db, share)


@router.delete(
    "/{collection_uuid}/shares/{share_uuid}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(refuse_under_support_grant)],
)
def delete_collection_share(
    collection_uuid: str,
    share_uuid: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Revoke a share on a collection. Requires direct collection ownership. Tenant-gated (#262d)."""
    collection = get_collection_by_uuid_with_permission(
        db, collection_uuid, current_user.id, organization_id=ctx.org_id
    )
    _require_collection_owner(db, collection, current_user.id)

    share = get_by_uuid(db, CollectionShare, share_uuid, "Share not found")

    # Verify share belongs to this collection
    if share.collection_id != collection.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Share not found on this collection",
        )

    # Capture notification/audit data before deletion -- gone from the row after.
    target_user_ids = _get_share_target_user_ids(db, share, collection)
    revoked_target_user_id = share.target_user_id
    revoked_target_group_id = share.target_group_id
    revoked_permission = share.permission
    revoke_data: dict = {
        "collection_uuid": str(collection.uuid),
        "collection_name": collection.name,
        "share_uuid": share_uuid,
        "permission": share.permission,
        "message": _share_message(NOTIFICATION_TYPE_COLLECTION_SHARE_REVOKED, collection.name),
    }

    db.delete(share)
    db.commit()

    # Reindex OpenSearch accessible_user_ids for files in this collection
    file_ids = [
        cm.media_file_id
        for cm in db.query(CollectionMember.media_file_id)
        .filter(CollectionMember.collection_id == collection.id)
        .all()
    ]
    if file_ids:
        update_file_access_index.delay(file_ids)

    req_ctx = get_request_context(request)
    audit_logger.log(
        event_type=AuditEventType.RESOURCE_UNSHARE,
        outcome=AuditOutcome.SUCCESS,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=req_ctx["source_ip"],
        user_agent=req_ctx["user_agent"],
        organization_id=collection.organization_id,
        target_user_id=revoked_target_user_id,
        resource_type="collection",
        resource_uuid=str(collection.uuid),
        tenant_scope=_tenant_scope(collection),
        details={
            "target_group_id": revoked_target_group_id,
            "permission": revoked_permission,
        },
    )

    # Notify affected user(s) about the revocation
    for uid in target_user_ids:
        send_ws_event(uid, NOTIFICATION_TYPE_COLLECTION_SHARE_REVOKED, revoke_data)

    return None
