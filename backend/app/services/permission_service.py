"""Central permission checking service for shared resources.

All access control decisions for collections and files route through this
service. It supports a three-level permission hierarchy (viewer < editor < owner)
and resolves access via direct ownership, direct user shares, and group shares.
"""

import logging
from typing import Any

from sqlalchemy import and_
from sqlalchemy import case
from sqlalchemy import func
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


#: Membership role that makes a user an owner of every collection of their org.
ORG_ADMIN_ROLE = "org:admin"


def org_scope_pred(column: Any, organization_id: OrgScope) -> Any:
    """NULL-safe tenant equality on ``column`` (``None`` for ``UNSCOPED`` = no gate).

    An int keeps rows of that org, ``None`` keeps org-less (personal) rows — the
    same default-deny rule as ``scope_to_context``. A plain ``column == None``
    would compile to ``= NULL`` and match nothing, hence the explicit ``IS NULL``.
    """
    if isinstance(organization_id, _Unscoped):
        return None
    if organization_id is None:
        return column.is_(None)
    return column == organization_id


def file_ids_in_scope(organization_id: OrgScope) -> Any:
    """``SELECT media_file.id`` for the files of one tenant (all files if UNSCOPED)."""
    stmt = select(MediaFile.id)
    pred = org_scope_pred(MediaFile.organization_id, organization_id)
    return stmt.where(pred) if pred is not None else stmt


class PermissionService:
    """Centralized permission checking for collections and files."""

    @staticmethod
    def collection_tenant_pred(user_id: int, organization_id: int | None):
        """The collections that are the caller's own in one tenant (issue #1051).

        In an organization that is every collection of the organization — they
        are shared by the org; in the personal workspace it is the caller's own
        personal collections. Explicitly shared collections are not included.
        """
        if organization_id is not None:
            return Collection.organization_id == organization_id
        return and_(Collection.user_id == user_id, Collection.organization_id.is_(None))

    @staticmethod
    def get_collection_permission(
        db: Session,
        collection_id: int,
        user_id: int,
        *,
        organization_id: OrgScope = UNSCOPED,
    ) -> str | None:
        """Get highest permission level for user on a collection.

        Decided by ``collection_visibility.collection_permission``, the one statement
        for it: an organization collection through membership of its org ('owner' for
        the creator or an org admin, 'editor' for every other member, v422), a personal
        collection through ownership, and either through a direct or group share.

        Returns highest of all matching permissions, or None if no access.

        ``organization_id`` is a default-deny tenant gate (int = that org, None =
        personal/org-less, ``UNSCOPED`` = no gate / legacy): a collection outside
        the active scope resolves to None even via the sharing path.
        """
        collection = db.query(Collection).filter(Collection.id == collection_id).first()
        if not collection:
            return None
        # Tenant gate: out-of-scope collections are invisible to the sharing resolver.
        if (
            not isinstance(organization_id, _Unscoped)
            and collection.organization_id != organization_id
        ):
            return None
        from app.services.collection_visibility import collection_permission

        return collection_permission(db, collection_id, user_id)

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
        3. File is in an organization collection the user is a member of and may see
           -> 'viewer' (read route only; see ``collection_visibility``)

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

        # A member of an organization collection they may see reads the files in it.
        from app.services.collection_visibility import file_readable_via_org_collection

        if file_readable_via_org_collection(db, file_id, user_id):
            return "viewer"
        return None

    @staticmethod
    def check_collection_access(
        db: Session,
        collection_id: int,
        user_id: int,
        min_permission: str = "viewer",
        *,
        organization_id: OrgScope = UNSCOPED,
    ) -> str:
        """Check user has minimum permission on collection, raise 403 if not.

        Returns the user's effective permission level. ``organization_id`` is the
        same default-deny tenant gate as ``get_collection_permission`` (UNSCOPED = none).

        Raises:
            HTTPException: 403 if user lacks the required permission.
        """
        from fastapi import HTTPException

        permission = PermissionService.get_collection_permission(
            db, collection_id, user_id, organization_id=organization_id
        )
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
        *,
        organization_id: OrgScope = UNSCOPED,
    ) -> str:
        """Check user has minimum permission on file, raise 403 if not.

        Returns the user's effective permission level. ``organization_id`` is the
        same default-deny tenant gate as ``get_file_permission`` (UNSCOPED = none).

        Raises:
            HTTPException: 403 if user lacks the required permission.
        """
        from fastapi import HTTPException

        permission = PermissionService.get_file_permission(
            db, file_id, user_id, organization_id=organization_id
        )
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
        - Files in organization collections the user is a member of and may see

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

        from app.services.collection_visibility import org_collection_file_ids_stmt

        via_org_collection = org_collection_file_ids_stmt(user_id, organization_id)
        return union_all(owned, shared, via_org_collection).subquery()

    @staticmethod
    def get_accessible_collection_ids(db: Session, user_id: int) -> list[tuple[int, str]]:
        """Get all collections the user can access with their permission level.

        Returns list of (collection_id, permission) tuples: the caller's personal
        collections, every collection of each organization they belong to (v422,
        see ``collection_visibility``), and explicit shares. Not
        tenant-gated — callers apply their own tenant predicate.
        """
        from app.services.collection_visibility import collection_permission_ranks

        return list(collection_permission_ranks(db, user_id).items())

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
    def get_accessible_profile_ids(
        db: Session, user_id: int, *, organization_id: OrgScope
    ) -> set[int]:
        """Return profile IDs the user can access (own + shared via collections).

        Union of:
        - Profiles owned by the user
        - Profiles linked to speakers in files in collections shared with the user

        ``organization_id`` is REQUIRED (#1027) because this set is handed to the
        profile kNN as ``accessible_profile_ids``, which then drops its ``user_id``
        term: without a gate here the SQL layer would admit another tenant's profile
        and only the OpenSearch org clause would stand between it and the caller. An
        int keeps only that org's profiles, ``None`` (personal) only org-less ones,
        on BOTH branches — the collection-share join included. ``UNSCOPED`` is for
        an ownership/ACL check that deliberately spans scopes, and must be spelled
        out by the caller.
        """
        user_group_ids = (
            select(UserGroupMember.group_id)
            .where(UserGroupMember.user_id == user_id)
            .scalar_subquery()
        )

        org_pred = org_scope_pred(SpeakerProfile.organization_id, organization_id)

        # Own profiles
        owned_query = db.query(SpeakerProfile.id).filter(SpeakerProfile.user_id == user_id)
        if org_pred is not None:
            owned_query = owned_query.filter(org_pred)
        owned = owned_query.all()

        # Shared profiles via collection chain
        shared_query = (
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
        )
        if org_pred is not None:
            shared_query = shared_query.filter(org_pred)
        shared = shared_query.distinct().all()

        return {row[0] for row in owned} | {row[0] for row in shared}

    @staticmethod
    def get_matchable_profile_ids(
        db: Session, user_id: int, *, organization_id: OrgScope
    ) -> set[int]:
        """Profiles ``user_id`` may MATCH a voiceprint against (privacy policy, #1280).

        ``get_accessible_profile_ids`` answers "which profiles may this person see": names
        and avatars are user assertions and stay visible. This answers the narrower
        question the kNN needs, "whose voiceprints may this person's audio be compared to".
        It drops every profile the caller does not own whose owner scope has voiceprints
        switched off, or whose ``share`` consent hook says no (restrict-only: sharing a
        collection never widens what the owner's own policy forbids). The caller's own
        profiles always stay; their own policy is enforced at the entry gates.

        Every voiceprint-matching call site uses this and not the access set; handing the
        access set to the kNN is how a shared profile of an opted-out owner would match.
        """
        from app.services.privacy.policy import filter_shareable_profiles

        accessible = PermissionService.get_accessible_profile_ids(
            db, user_id, organization_id=organization_id
        )
        return set(filter_shareable_profiles(db, user_id, accessible))

    @staticmethod
    def get_accessible_profile_ids_for_file(db: Session, user_id: int, file_id: int) -> set[int]:
        """``get_matchable_profile_ids`` in the tenant scope of the file being matched.

        The pipeline's speaker matching runs for one file, and its tenant is that
        FILE's ``organization_id`` — read from the row, never from caller state — the
        same rule ``SpeakerMatchingService`` applies to its own kNN clauses. It is the
        matching variant by definition, so shared profiles the owner's privacy policy
        withholds are already removed.
        """
        file_org = db.query(MediaFile.organization_id).filter(MediaFile.id == file_id).scalar()
        return PermissionService.get_matchable_profile_ids(
            db, user_id, organization_id=int(file_org) if file_org is not None else None
        )

    @staticmethod
    def get_accessible_profile_ids_with_source(
        db: Session, user_id: int, *, organization_id: OrgScope
    ) -> list[tuple[int, bool]]:
        """Return (profile_id, is_own) tuples for accessible profiles.

        Used by API endpoints to label shared vs owned profiles. ``organization_id``
        is REQUIRED and gates both branches exactly as in
        :meth:`get_accessible_profile_ids` (int = that org, ``None`` = org-less
        profiles only, ``UNSCOPED`` = no gate).
        """
        user_group_ids = (
            select(UserGroupMember.group_id)
            .where(UserGroupMember.user_id == user_id)
            .scalar_subquery()
        )
        org_pred = org_scope_pred(SpeakerProfile.organization_id, organization_id)

        owned_query = db.query(SpeakerProfile.id).filter(SpeakerProfile.user_id == user_id)
        if org_pred is not None:
            owned_query = owned_query.filter(org_pred)
        owned_ids = {row[0] for row in owned_query.all()}

        shared_query = (
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
        )
        if org_pred is not None:
            shared_query = shared_query.filter(org_pred)
        shared_ids = {row[0] for row in shared_query.distinct().all()}

        result = [(pid, True) for pid in owned_ids]
        result.extend((pid, False) for pid in shared_ids - owned_ids)
        return result
