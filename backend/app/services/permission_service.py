"""Central permission checking service for shared resources.

All access control decisions for collections and files route through this
service. It supports a three-level permission hierarchy (viewer < editor < owner)
and resolves access via direct ownership, direct user shares, and group shares.
"""

import logging
from typing import Any

from sqlalchemy import case
from sqlalchemy import func
from sqlalchemy import literal_column
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import union_all
from sqlalchemy.orm import Session

from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.core.tenancy import _Unscoped
from app.models.group import UserGroupMember
from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerProfile
from app.models.sharing import CollectionShare

logger = logging.getLogger(__name__)

# Permission hierarchy (higher number = more access)
PERMISSION_LEVELS = {"viewer": 1, "editor": 2, "owner": 3}
PERMISSION_NAMES = {1: "viewer", 2: "editor", 3: "owner"}


class PermissionService:
    """Centralized permission checking for collections and files."""

    @staticmethod
    def get_collection_permission(
        db: Session,
        collection_id: int,
        user_id: int,
        *,
        organization_id: OrgScope = UNSCOPED,
    ) -> str | None:
        """Get highest permission level for user on a collection.

        Checks in order:
        1. Direct ownership (collection.user_id == user_id) -> 'owner'
        2. Direct share (CollectionShare.target_user_id == user_id)
        3. Group share (CollectionShare.target_group_id in user's groups)

        Returns highest of all matching permissions, or None if no access.

        ``organization_id`` is a default-deny tenant gate (int = that org, None =
        personal/org-less, ``UNSCOPED`` = no gate / legacy): a collection outside
        the active scope resolves to None even via the sharing path.
        """
        # Check direct ownership first (fast path)
        collection = db.query(Collection).filter(Collection.id == collection_id).first()
        if not collection:
            return None
        # Tenant gate: out-of-scope collections are invisible to the sharing resolver.
        if (
            not isinstance(organization_id, _Unscoped)
            and collection.organization_id != organization_id
        ):
            return None
        if collection.user_id == user_id:
            return "owner"

        # Check direct user share and group shares in one query
        user_group_ids = (
            select(UserGroupMember.group_id)
            .where(UserGroupMember.user_id == user_id)
            .scalar_subquery()
        )

        max_perm = (
            db.query(
                func.max(
                    case(
                        (CollectionShare.permission == "editor", 2),
                        (CollectionShare.permission == "viewer", 1),
                        else_=0,
                    )
                )
            )
            .filter(
                CollectionShare.collection_id == collection_id,
                or_(
                    CollectionShare.target_user_id == user_id,
                    CollectionShare.target_group_id.in_(user_group_ids),
                ),
            )
            .scalar()
        )

        if max_perm and max_perm > 0:
            return PERMISSION_NAMES[max_perm]
        return None

    @staticmethod
    def get_file_permission(
        db: Session,
        file_id: int,
        user_id: int,
        *,
        organization_id: OrgScope = UNSCOPED,
    ) -> str | None:
        """Get highest permission for user on a file.

        Checks:
        1. Direct ownership (file.user_id == user_id) -> 'owner'
        2. File is in a shared collection that user has access to

        Returns highest permission, or None if no access.

        ``organization_id`` is a default-deny tenant gate: an int restricts to
        that org, ``None`` restricts to org-less (personal) files, and the
        ``UNSCOPED`` sentinel (default) applies no gate (legacy caller). A file
        outside the active scope resolves to None even via the sharing path, so
        cross-org shared files never leak. Community-edition invariance: callers
        pass UNSCOPED, so behavior is unchanged.
        """
        # Check direct ownership first (project user_id + organization_id, not full ORM object)
        owner_row = (
            db.query(MediaFile.user_id, MediaFile.organization_id)
            .filter(MediaFile.id == file_id)
            .first()
        )
        if not owner_row:
            return None
        # Tenant gate: out-of-scope files are invisible to the sharing resolver too.
        if not isinstance(organization_id, _Unscoped) and owner_row[1] != organization_id:
            return None
        if owner_row[0] == user_id:
            return "owner"

        # Check if file is in any collection shared with user
        user_group_ids = (
            select(UserGroupMember.group_id)
            .where(UserGroupMember.user_id == user_id)
            .scalar_subquery()
        )

        max_perm = (
            db.query(
                func.max(
                    case(
                        (CollectionShare.permission == "editor", 2),
                        (CollectionShare.permission == "viewer", 1),
                        else_=0,
                    )
                )
            )
            .join(
                CollectionMember,
                CollectionMember.collection_id == CollectionShare.collection_id,
            )
            .filter(
                CollectionMember.media_file_id == file_id,
                or_(
                    CollectionShare.target_user_id == user_id,
                    CollectionShare.target_group_id.in_(user_group_ids),
                ),
            )
            .scalar()
        )

        if max_perm and max_perm > 0:
            return PERMISSION_NAMES[max_perm]
        return None

    @staticmethod
    def check_collection_access(
        db: Session,
        collection_id: int,
        user_id: int,
        min_permission: str = "viewer",
    ) -> str:
        """Check user has minimum permission on collection, raise 403 if not.

        Returns the user's effective permission level.

        Raises:
            HTTPException: 403 if user lacks the required permission.
        """
        from fastapi import HTTPException

        permission = PermissionService.get_collection_permission(db, collection_id, user_id)
        if permission is None:
            raise HTTPException(
                status_code=403,
                detail="Not authorized to access this collection",
            )

        if PERMISSION_LEVELS[permission] < PERMISSION_LEVELS[min_permission]:
            raise HTTPException(
                status_code=403,
                detail=f"Requires {min_permission} permission on this collection",
            )
        return permission

    @staticmethod
    def check_file_access(
        db: Session,
        file_id: int,
        user_id: int,
        min_permission: str = "viewer",
    ) -> str:
        """Check user has minimum permission on file, raise 403 if not.

        Returns the user's effective permission level.

        Raises:
            HTTPException: 403 if user lacks the required permission.
        """
        from fastapi import HTTPException

        permission = PermissionService.get_file_permission(db, file_id, user_id)
        if permission is None:
            raise HTTPException(
                status_code=403,
                detail="Not authorized to access this file",
            )

        if PERMISSION_LEVELS[permission] < PERMISSION_LEVELS[min_permission]:
            raise HTTPException(
                status_code=403,
                detail=f"Requires {min_permission} permission on this file",
            )
        return permission

    @staticmethod
    def get_accessible_file_ids_subquery(
        db: Session, user_id: int, *, organization_id: OrgScope = UNSCOPED
    ):
        """Return a subquery of file IDs the user can access.

        Union of:
        - Files owned by user
        - Files in collections shared with user (directly or via groups)

        ``organization_id`` is a default-deny tenant gate applied to BOTH
        branches: an int restricts to same-org files; ``None`` (personal)
        restricts to org-less files (so an org-shared file never surfaces in a
        personal listing); the ``UNSCOPED`` sentinel (default) applies no gate
        (legacy caller). Community-edition invariance: callers pass UNSCOPED and
        files are org-less, so behavior is unchanged.
        """
        user_group_ids = (
            select(UserGroupMember.group_id)
            .where(UserGroupMember.user_id == user_id)
            .scalar_subquery()
        )

        org_pred = None
        if not isinstance(organization_id, _Unscoped):
            if organization_id is not None:
                org_pred = MediaFile.organization_id == organization_id
            else:
                org_pred = MediaFile.organization_id.is_(None)

        # Files owned by user (within tenant scope)
        owned_where = [MediaFile.user_id == user_id]
        if org_pred is not None:
            owned_where.append(org_pred)
        owned = select(MediaFile.id).where(*owned_where)

        # Files in shared collections (still gated to the active tenant scope).
        # The MediaFile join is only needed when an org predicate is active.
        share_where: list[Any] = [
            or_(
                CollectionShare.target_user_id == user_id,
                CollectionShare.target_group_id.in_(user_group_ids),
            )
        ]
        if org_pred is not None:
            shared_stmt = select(CollectionMember.media_file_id).join(
                MediaFile, MediaFile.id == CollectionMember.media_file_id
            )
            share_where.append(org_pred)
        else:
            shared_stmt = select(CollectionMember.media_file_id)

        shared = shared_stmt.join(
            CollectionShare,
            CollectionShare.collection_id == CollectionMember.collection_id,
        ).where(*share_where)

        return union_all(owned, shared).subquery()

    @staticmethod
    def get_accessible_collection_ids(db: Session, user_id: int) -> list[tuple[int, str]]:
        """Get all collections the user can access with their permission level.

        Returns list of (collection_id, permission) tuples.
        """
        # Owned collections
        owned: list[Any] = (
            db.query(Collection.id, literal_column("'owner'").label("permission"))
            .filter(Collection.user_id == user_id)
            .all()
        )

        # Shared collections
        user_group_ids = (
            select(UserGroupMember.group_id)
            .where(UserGroupMember.user_id == user_id)
            .scalar_subquery()
        )

        shared = (
            db.query(CollectionShare.collection_id, CollectionShare.permission)
            .filter(
                or_(
                    CollectionShare.target_user_id == user_id,
                    CollectionShare.target_group_id.in_(user_group_ids),
                )
            )
            .all()
        )

        # Merge: highest permission wins
        result: dict[int, str] = {}
        for cid, perm in owned + shared:
            if cid not in result or PERMISSION_LEVELS.get(perm, 0) > PERMISSION_LEVELS.get(
                result[cid], 0
            ):
                result[cid] = perm

        return [(cid, perm) for cid, perm in result.items()]

    @staticmethod
    def get_users_with_file_access(db: Session, file_id: int) -> list[int]:
        """Get all user IDs who have access to a file (for notifications).

        Returns list of user IDs including:
        - File owner
        - Users with direct share on collections containing this file
        - Users in groups with share on collections containing this file
        """
        media_file = db.query(MediaFile).filter(MediaFile.id == file_id).first()
        if not media_file:
            return []

        user_ids = {media_file.user_id}

        # Get all collection shares for collections containing this file
        shares = (
            db.query(CollectionShare)
            .join(
                CollectionMember,
                CollectionMember.collection_id == CollectionShare.collection_id,
            )
            .filter(CollectionMember.media_file_id == file_id)
            .all()
        )

        for share in shares:
            if share.target_type == "user" and share.target_user_id:
                user_ids.add(share.target_user_id)
            elif share.target_type == "group" and share.target_group_id:
                # Get all members of this group
                members = (
                    db.query(UserGroupMember.user_id)
                    .filter(UserGroupMember.group_id == share.target_group_id)
                    .all()
                )
                for (uid,) in members:
                    user_ids.add(uid)

        return list(user_ids)

    @staticmethod
    def get_accessible_profile_ids(db: Session, user_id: int) -> set[int]:
        """Return profile IDs the user can access (own + shared via collections).

        Union of:
        - Profiles owned by the user
        - Profiles linked to speakers in files in collections shared with the user
        """
        user_group_ids = (
            select(UserGroupMember.group_id)
            .where(UserGroupMember.user_id == user_id)
            .scalar_subquery()
        )

        # Own profiles
        owned = db.query(SpeakerProfile.id).filter(SpeakerProfile.user_id == user_id).all()

        # Shared profiles via collection chain
        shared = (
            db.query(SpeakerProfile.id)
            .join(Speaker, Speaker.profile_id == SpeakerProfile.id)
            .join(MediaFile, MediaFile.id == Speaker.media_file_id)
            .join(CollectionMember, CollectionMember.media_file_id == MediaFile.id)
            .join(
                CollectionShare,
                CollectionShare.collection_id == CollectionMember.collection_id,
            )
            .filter(
                or_(
                    CollectionShare.target_user_id == user_id,
                    CollectionShare.target_group_id.in_(user_group_ids),
                )
            )
            .distinct()
            .all()
        )

        return {row[0] for row in owned} | {row[0] for row in shared}

    @staticmethod
    def get_accessible_profile_ids_with_source(db: Session, user_id: int) -> list[tuple[int, bool]]:
        """Return (profile_id, is_own) tuples for accessible profiles.

        Used by API endpoints to label shared vs owned profiles.
        """
        user_group_ids = (
            select(UserGroupMember.group_id)
            .where(UserGroupMember.user_id == user_id)
            .scalar_subquery()
        )

        owned_ids = set(
            row[0]
            for row in db.query(SpeakerProfile.id).filter(SpeakerProfile.user_id == user_id).all()
        )

        shared_ids = set(
            row[0]
            for row in db.query(SpeakerProfile.id)
            .join(Speaker, Speaker.profile_id == SpeakerProfile.id)
            .join(MediaFile, MediaFile.id == Speaker.media_file_id)
            .join(CollectionMember, CollectionMember.media_file_id == MediaFile.id)
            .join(
                CollectionShare,
                CollectionShare.collection_id == CollectionMember.collection_id,
            )
            .filter(
                or_(
                    CollectionShare.target_user_id == user_id,
                    CollectionShare.target_group_id.in_(user_group_ids),
                )
            )
            .distinct()
            .all()
        )

        result = [(pid, True) for pid in owned_ids]
        result.extend((pid, False) for pid in shared_ids - owned_ids)
        return result
