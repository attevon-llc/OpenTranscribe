"""API endpoints for user groups (CRUD + membership management)."""

import logging
from datetime import UTC
from datetime import datetime

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.orm import joinedload

from app.api.endpoints.auth import get_current_active_user
from app.core.constants import NOTIFICATION_TYPE_GROUP_MEMBER_ADDED
from app.core.constants import NOTIFICATION_TYPE_GROUP_MEMBER_REMOVED
from app.db.base import get_db
from app.models.group import UserGroup
from app.models.group import UserGroupMember
from app.models.media import CollectionMember
from app.models.sharing import CollectionShare
from app.models.user import User
from app.schemas.group import Group as GroupSchema
from app.schemas.group import GroupCreate
from app.schemas.group import GroupDetail
from app.schemas.group import GroupMember
from app.schemas.group import GroupMemberAdd
from app.schemas.group import GroupMemberUpdate
from app.schemas.group import GroupUpdate
from app.schemas.user import UserBrief
from app.tasks.search_indexing_task import update_file_access_index
from app.utils.uuid_helpers import get_by_uuid
from app.utils.uuid_helpers import require_resource_owner
from app.utils.websocket_notify import send_ws_event

logger = logging.getLogger(__name__)

router = APIRouter()

#: Substituted for a NULL ``created_at`` / ``joined_at`` / ``updated_at``.
#:
#: ``user_group.created_at``, ``user_group.updated_at`` and ``user_group_member.joined_at``
#: are all ``nullable`` in Postgres: a ``server_default`` fills a column only for an INSERT
#: that omits it, so a raw-SQL insert naming the column, a migration backfill or an explicit
#: ``UPDATE ... SET created_at = NULL`` leaves NULL behind. The response schemas
#: (``Group.created_at``, ``GroupDetail.updated_at``, ``GroupMember.joined_at``) require a
#: non-null ``datetime``, so a NULL has to become *some* timestamp or the whole listing
#: fails validation with a 500. The epoch is used rather than ``datetime.now(UTC)``
#: precisely because it is obviously not a real value: "now" would silently claim the row
#: was just created and would sort a timestamp-less group to the top of every list.
UNKNOWN_TIMESTAMP = datetime(1970, 1, 1, tzinfo=UTC)


def _get_membership(db: Session, group_id: int, user_id: int) -> UserGroupMember | None:
    """Get a user's membership record in a group."""
    result: UserGroupMember | None = (
        db.query(UserGroupMember)
        .filter(
            UserGroupMember.group_id == group_id,
            UserGroupMember.user_id == user_id,
        )
        .first()
    )
    return result


def _require_group_admin(db: Session, group: UserGroup, user_id: int) -> UserGroupMember:
    """Require that the user is an owner or admin of the group.

    Returns the membership record.

    Raises:
        HTTPException: 403 if user lacks owner/admin role.
    """
    membership = _get_membership(db, group.id, user_id)
    if not membership or membership.role not in ("owner", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires owner or admin role in this group",
        )
    return membership


def _reindex_group_shared_files(db: Session, group_id: int) -> None:
    """Reindex files in collections shared with this group."""
    shared_collection_ids = [
        cs.collection_id
        for cs in db.query(CollectionShare.collection_id)
        .filter(CollectionShare.target_group_id == group_id)
        .all()
    ]
    if not shared_collection_ids:
        return
    file_ids = [
        cm.media_file_id
        for cm in db.query(CollectionMember.media_file_id)
        .filter(CollectionMember.collection_id.in_(shared_collection_ids))
        .distinct()
        .all()
    ]
    if file_ids:
        update_file_access_index.delay(file_ids)


def _build_group_response(db: Session, group: UserGroup, current_user_id: int) -> GroupSchema:
    """Build a Group response schema with computed fields."""
    member_count = db.query(UserGroupMember).filter(UserGroupMember.group_id == group.id).count()
    my_membership = _get_membership(db, group.id, current_user_id)
    my_role = my_membership.role if my_membership else None

    owner_brief = UserBrief(
        uuid=group.owner.uuid,
        full_name=group.owner.full_name,
        email=group.owner.email,
    )

    return GroupSchema(
        uuid=group.uuid,
        name=group.name,
        description=group.description,
        member_count=member_count,
        my_role=my_role or "member",
        owner=owner_brief,
        created_at=group.created_at or UNKNOWN_TIMESTAMP,
    )


@router.get("", response_model=list[GroupSchema])
def list_groups(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """List groups the current user belongs to (owned + member)."""
    groups = (
        db.query(UserGroup)
        .join(UserGroupMember, UserGroupMember.group_id == UserGroup.id)
        .options(joinedload(UserGroup.owner))
        .filter(UserGroupMember.user_id == current_user.id)
        .all()
    )

    if not groups:
        return []

    group_ids = [g.id for g in groups]

    # Batch: member counts per group
    count_rows = (
        db.query(UserGroupMember.group_id, func.count(UserGroupMember.id))
        .filter(UserGroupMember.group_id.in_(group_ids))
        .group_by(UserGroupMember.group_id)
        .all()
    )
    counts = {gid: cnt for gid, cnt in count_rows}

    # Batch: current user's role per group
    role_rows = (
        db.query(UserGroupMember.group_id, UserGroupMember.role)
        .filter(
            UserGroupMember.group_id.in_(group_ids),
            UserGroupMember.user_id == current_user.id,
        )
        .all()
    )
    roles = {gid: role for gid, role in role_rows}

    results = []
    for group in groups:
        owner_brief = UserBrief(
            uuid=group.owner.uuid,
            full_name=group.owner.full_name,
            email=group.owner.email,
        )
        results.append(
            GroupSchema(
                uuid=group.uuid,
                name=group.name,
                description=group.description,
                member_count=counts.get(group.id, 0),
                my_role=roles.get(group.id, "member"),
                owner=owner_brief,
                created_at=group.created_at or UNKNOWN_TIMESTAMP,
            )
        )

    return results


@router.post("", response_model=GroupSchema, status_code=status.HTTP_201_CREATED)
def create_group(
    group_in: GroupCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Create a new group. Creator is automatically added as owner."""
    # Check for duplicate name for this user
    existing = (
        db.query(UserGroup)
        .filter(
            UserGroup.owner_id == current_user.id,
            UserGroup.name == group_in.name,
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Group with name '{group_in.name}' already exists",
        )

    group = UserGroup(
        name=group_in.name,
        description=group_in.description,
        owner_id=current_user.id,
    )
    db.add(group)
    db.flush()

    # Auto-add creator as owner member
    owner_member = UserGroupMember(
        group_id=group.id,
        user_id=current_user.id,
        role="owner",
    )
    db.add(owner_member)
    db.commit()
    db.refresh(group)

    # Load owner relationship for response
    db.refresh(group, ["owner"])

    return _build_group_response(db, group, current_user.id)


@router.get("/{group_uuid}", response_model=GroupDetail)
def get_group(
    group_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Get group detail with member list. Must be a member."""
    group = get_by_uuid(db, UserGroup, group_uuid, "Group not found")

    # Verify user is a member
    my_membership = _get_membership(db, group.id, current_user.id)
    if not my_membership:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not a member of this group",
        )

    # Load members with user info
    members_db = (
        db.query(UserGroupMember)
        .options(joinedload(UserGroupMember.user))
        .filter(UserGroupMember.group_id == group.id)
        .all()
    )

    members = []
    for m in members_db:
        members.append(
            GroupMember(
                uuid=m.uuid,
                user_uuid=m.user.uuid,
                email=m.user.email,
                full_name=m.user.full_name,
                role=m.role,
                joined_at=m.joined_at or UNKNOWN_TIMESTAMP,
            )
        )

    member_count = len(members)
    owner_brief = UserBrief(
        uuid=group.owner.uuid,
        full_name=group.owner.full_name,
        email=group.owner.email,
    )

    return GroupDetail(
        uuid=group.uuid,
        name=group.name,
        description=group.description,
        member_count=member_count,
        my_role=my_membership.role,
        owner=owner_brief,
        created_at=group.created_at or UNKNOWN_TIMESTAMP,
        updated_at=group.updated_at or UNKNOWN_TIMESTAMP,
        members=members,
    )


@router.put("/{group_uuid}", response_model=GroupSchema)
def update_group(
    group_uuid: str,
    group_update: GroupUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Update group name/description. Requires owner or admin role."""
    group = get_by_uuid(db, UserGroup, group_uuid, "Group not found")
    _require_group_admin(db, group, current_user.id)

    update_data = group_update.model_dump(exclude_unset=True)

    # Check name uniqueness if changing name
    if "name" in update_data and update_data["name"] != group.name:
        existing = (
            db.query(UserGroup)
            .filter(
                UserGroup.owner_id == group.owner_id,
                UserGroup.name == update_data["name"],
                UserGroup.id != group.id,
            )
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Group with name '{update_data['name']}' already exists",
            )

    for field, value in update_data.items():
        setattr(group, field, value)

    db.commit()
    db.refresh(group)

    return _build_group_response(db, group, current_user.id)


@router.delete("/{group_uuid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_group(
    group_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Delete a group. Only the group owner can delete it."""
    group = get_by_uuid(db, UserGroup, group_uuid, "Group not found")

    require_resource_owner(
        group,
        current_user,
        forbidden_detail="Only the group owner can delete this group",
        owner_attr="owner_id",
    )

    # Reindex files BEFORE deletion (cascade will remove shares)
    _reindex_group_shared_files(db, group.id)

    db.delete(group)
    db.commit()

    return None


@router.post(
    "/{group_uuid}/members",
    response_model=GroupMember,
    status_code=status.HTTP_201_CREATED,
)
def add_member(
    group_uuid: str,
    member_add: GroupMemberAdd,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Add a member to a group. Requires owner or admin role."""
    group = get_by_uuid(db, UserGroup, group_uuid, "Group not found")
    _require_group_admin(db, group, current_user.id)

    # Resolve target user
    target_user = get_by_uuid(db, User, str(member_add.user_uuid), "User not found")

    # Check not already a member
    existing = _get_membership(db, group.id, target_user.id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User is already a member of this group",
        )

    member = UserGroupMember(
        group_id=group.id,
        user_id=target_user.id,
        role=member_add.role,
    )
    db.add(member)
    db.commit()
    db.refresh(member)

    # Reindex files in collections shared with this group
    _reindex_group_shared_files(db, group.id)

    # Notify the new member
    send_ws_event(
        target_user.id,
        NOTIFICATION_TYPE_GROUP_MEMBER_ADDED,
        {
            "group_uuid": str(group.uuid),
            "group_name": group.name,
            "role": member.role,
            "message": f"You have been added to group '{group.name}'",
        },
    )

    return GroupMember(
        uuid=member.uuid,
        user_uuid=target_user.uuid,
        email=target_user.email,
        full_name=target_user.full_name,
        role=member.role,
        joined_at=member.joined_at or UNKNOWN_TIMESTAMP,
    )


@router.put("/{group_uuid}/members/{user_uuid}", response_model=GroupMember)
def update_member_role(
    group_uuid: str,
    user_uuid: str,
    member_update: GroupMemberUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Update a member's role. Requires owner or admin role."""
    group = get_by_uuid(db, UserGroup, group_uuid, "Group not found")
    caller_membership = _require_group_admin(db, group, current_user.id)

    target_user = get_by_uuid(db, User, user_uuid, "User not found")
    target_membership = _get_membership(db, group.id, target_user.id)

    if not target_membership:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User is not a member of this group",
        )

    # Cannot change the owner's role
    if target_membership.role == "owner":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot change the group owner's role",
        )

    # Admins cannot change other admins' roles (only owner can)
    if caller_membership.role == "admin" and (
        target_membership.role == "admin" or member_update.role == "admin"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the group owner can change admin roles",
        )

    target_membership.role = member_update.role
    db.commit()
    db.refresh(target_membership)

    # Reindex files in collections shared with this group
    _reindex_group_shared_files(db, group.id)

    return GroupMember(
        uuid=target_membership.uuid,
        user_uuid=target_user.uuid,
        email=target_user.email,
        full_name=target_user.full_name,
        role=target_membership.role,
        joined_at=target_membership.joined_at or UNKNOWN_TIMESTAMP,
    )


@router.delete(
    "/{group_uuid}/members/{user_uuid}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def remove_member(
    group_uuid: str,
    user_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Remove a member from a group.

    Owner/admin can remove others. Any member can remove themselves (leave).
    The group owner cannot leave -- they must delete the group instead.
    """
    group = get_by_uuid(db, UserGroup, group_uuid, "Group not found")
    target_user = get_by_uuid(db, User, user_uuid, "User not found")

    target_membership = _get_membership(db, group.id, target_user.id)
    if not target_membership:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User is not a member of this group",
        )

    is_self_remove = target_user.id == current_user.id

    # Owner cannot leave
    if target_membership.role == "owner":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Group owner cannot leave. Delete the group instead.",
        )

    # If not self-remove, require owner/admin role
    if not is_self_remove:
        _require_group_admin(db, group, current_user.id)

    removed_user_id = target_user.id

    db.delete(target_membership)
    db.commit()

    # Reindex files in collections shared with this group
    _reindex_group_shared_files(db, group.id)

    # Notify the removed member (skip if self-removal)
    if not is_self_remove:
        send_ws_event(
            removed_user_id,
            NOTIFICATION_TYPE_GROUP_MEMBER_REMOVED,
            {
                "group_uuid": str(group.uuid),
                "group_name": group.name,
                "message": f"You have been removed from group '{group.name}'",
            },
        )

    return None
