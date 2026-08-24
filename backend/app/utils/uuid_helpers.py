"""
UUID Helper Utilities for Hybrid ID System

This module provides utilities for the hybrid ID approach:
- Internal: Fast integer IDs for database operations
- External: Secure UUIDs for API exposure

Performance Notes:
- UUID lookups use indexed columns for fast resolution
- Integer IDs used for all internal joins and foreign keys
- UUIDs only exposed in API layer (Pydantic schemas)
"""

from typing import Any
from uuid import UUID

from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.core.tenancy import _Unscoped
from app.models.media import Collection
from app.models.media import Comment
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerProfile
from app.models.prompt import SummaryPrompt
from app.models.user import User
from app.models.user_llm_settings import UserLLMSettings


def get_by_uuid[T](
    db: Session,
    model: type[T],
    uuid: UUID | str,
    error_message: str | None = None,
) -> T:
    """
    Get a database record by UUID.

    Args:
        db: Database session
        model: SQLAlchemy model class
        uuid: UUID to look up (UUID object or string)
        error_message: Custom error message for 404

    Returns:
        Model instance

    Raises:
        HTTPException: 404 if not found
    """
    # Convert string to UUID if needed
    if isinstance(uuid, str):
        try:
            uuid = UUID(uuid)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid UUID format: {uuid}",
            ) from None

    # Query by UUID
    instance = db.query(model).filter(model.uuid == uuid).first()  # type: ignore[attr-defined]

    if not instance:
        model_name = model.__name__
        message = error_message or f"{model_name} not found"
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=message,
        )

    return instance  # type: ignore[no-any-return]


def get_by_uuid_optional[T](
    db: Session,
    model: type[T],
    uuid: UUID | str | None,
) -> T | None:
    """
    Get a database record by UUID, returning None if not found.

    Args:
        db: Database session
        model: SQLAlchemy model class
        uuid: UUID to look up (UUID object, string, or None)

    Returns:
        Model instance or None
    """
    if uuid is None:
        return None

    # Convert string to UUID if needed
    if isinstance(uuid, str):
        try:
            uuid = UUID(uuid)
        except ValueError:
            return None

    return db.query(model).filter(model.uuid == uuid).first()  # type: ignore[attr-defined, no-any-return]


def uuid_to_id[T](db: Session, model: type[T], uuid: UUID | str) -> int:
    """
    Convert UUID to internal integer ID.

    Useful for constructing queries with foreign keys.

    Args:
        db: Database session
        model: SQLAlchemy model class
        uuid: UUID to look up

    Returns:
        Integer ID

    Raises:
        HTTPException: 404 if not found
    """
    instance = get_by_uuid(db, model, uuid)
    return int(instance.id)  # type: ignore[attr-defined]


def validate_uuids(uuids: list[str]) -> list[UUID]:
    """
    Validate and convert list of UUID strings.

    Args:
        uuids: List of UUID strings

    Returns:
        List of UUID objects

    Raises:
        HTTPException: 400 if any UUID is invalid
    """
    result = []
    for uuid_item in uuids:
        try:
            # Handle both UUID objects and strings
            if isinstance(uuid_item, UUID):
                result.append(uuid_item)
            else:
                result.append(UUID(uuid_item))
        except (ValueError, AttributeError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid UUID format: {uuid_item}",
            ) from None
    return result


# Convenience functions for common models
def get_user_by_uuid(db: Session, uuid: UUID | str) -> User:
    """Get user by UUID"""
    return get_by_uuid(db, User, uuid, error_message="User not found")


def get_file_by_uuid(db: Session, uuid: UUID | str) -> MediaFile:
    """Get media file by UUID"""
    return get_by_uuid(db, MediaFile, uuid, error_message="File not found")


def get_speaker_by_uuid(db: Session, uuid: UUID | str) -> Speaker:
    """Get speaker by UUID"""
    return get_by_uuid(db, Speaker, uuid, error_message="Speaker not found")


def get_speaker_profile_by_uuid(db: Session, uuid: UUID | str) -> SpeakerProfile:
    """Get speaker profile by UUID"""
    return get_by_uuid(db, SpeakerProfile, uuid, error_message="Speaker profile not found")


def get_collection_by_uuid(db: Session, uuid: UUID | str) -> Collection:
    """Get collection by UUID"""
    return get_by_uuid(db, Collection, uuid, error_message="Collection not found")


def get_comment_by_uuid(db: Session, uuid: UUID | str) -> Comment:
    """Get comment by UUID"""
    return get_by_uuid(db, Comment, uuid, error_message="Comment not found")


def get_prompt_by_uuid(db: Session, uuid: UUID | str) -> SummaryPrompt:
    """Get summary prompt by UUID"""
    return get_by_uuid(db, SummaryPrompt, uuid, error_message="Prompt not found")


def get_llm_config_by_uuid(db: Session, uuid: UUID | str) -> UserLLMSettings:
    """Get LLM configuration by UUID"""
    return get_by_uuid(db, UserLLMSettings, uuid, error_message="LLM configuration not found")


# Generic ownership gate
def require_resource_owner(
    resource: Any,
    user: User,
    *,
    forbidden_detail: str,
    owner_attr: str = "user_id",
    allow_admin: bool = False,
) -> None:
    """Raise 403 with the caller's detail when ``user`` does not own ``resource``.

    Behavior-preserving consolidation of the copy-pasted ``if resource.user_id !=
    current_user.id: raise HTTPException(403, ...)`` pattern across endpoints.

    Args:
        resource: ORM instance bearing the owner foreign-key attribute.
        user: Current authenticated user.
        forbidden_detail: Exact ``detail`` string for the 403 (per-site message).
        owner_attr: Attribute on ``resource`` holding the owner user id (e.g.
            ``"user_id"`` or ``"owner_id"``).
        allow_admin: When True, admins (``user.is_admin``) bypass the gate.

    Raises:
        HTTPException: 403 ``forbidden_detail`` when the user is neither the
            owner nor (when permitted) an admin.
    """
    if allow_admin and user.is_admin:
        return
    if getattr(resource, owner_attr) != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=forbidden_detail,
        )


def _resource_in_tenant_scope(resource: Any, organization_id: OrgScope) -> bool:
    """Default-deny tenant-scope check for a resource's ``organization_id``.

    Mirrors ``scope_to_context``: in org context only same-org rows pass; in
    personal scope (``organization_id is None``) only org-less rows pass. The
    ``UNSCOPED`` sentinel means no context was threaded (legacy caller) — no gate
    is applied, preserving pre-cloud behavior. Community-edition invariance:
    callers pass UNSCOPED (or None against org-less rows), so this is always True.
    """
    if isinstance(organization_id, _Unscoped):
        return True
    return getattr(resource, "organization_id", None) == organization_id


# Permission checking helpers
def get_file_by_uuid_with_permission(
    db: Session,
    uuid: UUID | str,
    user_id: int,
    allow_public: bool = False,
    is_admin: bool = False,
    *,
    organization_id: OrgScope = UNSCOPED,
) -> MediaFile:
    """
    Get media file by UUID with permission check.

    Checks admin bypass first, then direct ownership, then falls back to
    shared access via PermissionService (direct shares and group shares).

    Args:
        db: Database session
        uuid: File UUID
        user_id: Current user ID
        allow_public: Whether to allow public files
        is_admin: Whether the user has admin privileges (bypasses all checks)
        organization_id: Active org id, None for personal, or UNSCOPED (default,
            legacy = no gate). When an org id (or explicit None) is passed,
            cross-scope files are rejected even via the sharing path (default-deny).

    Returns:
        MediaFile instance

    Raises:
        HTTPException: 404 if not found, 403 if no permission
    """
    from app.services.permission_service import PermissionService
    from app.services.takedown_service import is_hidden_for

    file = get_file_by_uuid(db, uuid)

    # Admin bypass — admins can access any file (incl. quarantined, for review).
    if is_admin:
        return file

    # Abuse/DMCA takedown: a quarantined file is invisible to non-admins on EVERY
    # read surface (detail, stream, download, thumbnail). 404 (not 403) so a
    # taken-down file is indistinguishable from a missing one — even the owner
    # and public-share viewers are blocked.
    if is_hidden_for(file, is_admin=is_admin):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found",
        )

    # Public files are accessible regardless of tenant scope (intentional).
    if allow_public and file.is_public:
        return file

    # Tenant gate: a file outside the active scope is invisible (default-deny).
    # Returning 403 keeps parity with the existing no-permission path.
    if not _resource_in_tenant_scope(file, organization_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to access this file",
        )

    # Direct ownership (fast path)
    if file.user_id == user_id:
        return file

    # Check shared access via PermissionService (already in tenant scope above)
    permission = PermissionService.get_file_permission(db, file.id, user_id)
    if permission is not None:
        return file

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have permission to access this file",
    )


def get_collection_by_uuid_with_permission(
    db: Session,
    uuid: UUID | str,
    user_id: int,
    is_admin: bool = False,
    *,
    organization_id: OrgScope = UNSCOPED,
) -> Collection:
    """
    Get collection by UUID with permission check.

    Checks admin bypass first, then direct ownership, then falls back to
    shared access via PermissionService (direct shares and group shares).

    Args:
        db: Database session
        uuid: Collection UUID
        user_id: Current user ID
        is_admin: Whether the user has admin privileges (bypasses all checks)
        organization_id: Active org id, None for personal, or UNSCOPED (default,
            legacy = no gate) so community callers are unaffected.

    Returns:
        Collection instance

    Raises:
        HTTPException: 404 if not found, 403 if no permission
    """
    from app.services.permission_service import PermissionService

    collection = get_collection_by_uuid(db, uuid)

    # Admin bypass — admins can access any collection
    if is_admin:
        return collection

    # Tenant gate: a collection outside the active scope is invisible (default-deny).
    if not _resource_in_tenant_scope(collection, organization_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to access this collection",
        )

    # Direct ownership (fast path)
    if collection.user_id == user_id:
        return collection

    # Check shared access via PermissionService
    permission = PermissionService.get_collection_permission(db, collection.id, user_id)
    if permission is not None:
        return collection

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have permission to access this collection",
    )


# Sharing-aware permission helpers (return permission level)
def get_collection_by_uuid_with_sharing(
    db: Session,
    uuid: UUID | str,
    user_id: int,
    is_admin: bool = False,
    min_permission: str = "viewer",
    *,
    organization_id: OrgScope = UNSCOPED,
) -> tuple[Collection, str]:
    """
    Get collection by UUID with permission-aware access checking.

    Returns (collection, effective_permission) tuple. Admin users bypass
    permission checks and receive 'owner' as their effective permission.

    Args:
        db: Database session
        uuid: Collection UUID
        user_id: Current user ID
        is_admin: Whether the user has admin privileges
        min_permission: Minimum required permission level
        organization_id: Active org id, None for personal, or UNSCOPED (default,
            legacy = no gate) so community callers are unaffected.

    Returns:
        Tuple of (Collection, effective_permission_string)

    Raises:
        HTTPException: 404 if not found, 403 if insufficient permission
    """
    from app.services.permission_service import PERMISSION_LEVELS
    from app.services.permission_service import PermissionService

    collection = get_collection_by_uuid(db, uuid)

    # Admin bypass
    if is_admin:
        return collection, "owner"

    # Tenant gate: out-of-scope collections are not authorized (default-deny).
    if not _resource_in_tenant_scope(collection, organization_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access this collection",
        )

    # Check permission via PermissionService
    permission = PermissionService.get_collection_permission(db, collection.id, user_id)
    if permission is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access this collection",
        )

    if PERMISSION_LEVELS[permission] < PERMISSION_LEVELS[min_permission]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Requires {min_permission} permission on this collection",
        )

    return collection, permission
