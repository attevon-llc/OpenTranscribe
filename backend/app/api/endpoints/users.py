import logging
from datetime import datetime
from datetime import timezone

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import status
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.auth import get_current_admin_user
from app.auth.ldap_auth import AUTH_TYPE_LOCAL
from app.auth.password_history import add_password_to_history
from app.auth.password_history import check_password_against_history
from app.auth.roles import ROLE_USER
from app.auth.roles import VALID_ROLES
from app.auth.roles import role_implies_superuser
from app.core.config import settings
from app.core.security import get_password_hash
from app.core.security import verify_password
from app.db.base import get_db
from app.models.user import User
from app.schemas.user import User as UserSchema
from app.schemas.user import UserCreate
from app.schemas.user import UserSearchResult
from app.schemas.user import UserUpdate
from app.utils.uuid_helpers import get_user_by_uuid

logger = logging.getLogger(__name__)

router = APIRouter()


def create_user(user_data: UserCreate, db: Session) -> User:
    """
    Create a new user

    This function is called from both the registration endpoint
    and the admin user creation endpoint
    """
    # Check if email already exists
    db_user = db.query(User).filter(User.email == user_data.email).first()
    if db_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Email already registered"
        )

    # role is the authorization source of truth; is_superuser is derived from it
    # (never taken from the client). Privilege of the *caller* is enforced by the
    # endpoint (see admin.create_admin_user); this helper only validates the value.
    role = user_data.role or ROLE_USER
    if role not in VALID_ROLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid role: {role}",
        )

    new_user = User(
        email=user_data.email,
        hashed_password=get_password_hash(user_data.password),
        full_name=user_data.full_name,
        is_active=user_data.is_active if user_data.is_active is not None else True,
        role=role,
        is_superuser=role_implies_superuser(role),
    )

    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    return new_user


@router.get("", response_model=list[UserSchema])
def list_users(
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """
    List users (admin only) with optional pagination.
    """
    users = db.query(User).order_by(User.id).offset(offset).limit(limit).all()
    return users


@router.get("/me", response_model=UserSchema)
def get_current_user_info(current_user: User = Depends(get_current_active_user)):
    """
    Get current user info
    """
    return current_user


@router.put("/me", response_model=UserSchema)
def update_current_user(
    user_update: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Update current user info
    """
    # Check if email is being changed and is already taken
    if user_update.email and user_update.email != current_user.email:
        existing_user = db.query(User).filter(User.email == user_update.email).first()
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered",
            )

    # Update fields — strip privileged fields that only admins may change.
    # Without this, any user could promote themselves via PUT /users/me.
    update_data = user_update.model_dump(exclude_unset=True)
    privileged_fields = {"is_active", "is_superuser", "role", "auth_type", "allow_local_fallback"}
    for field in privileged_fields:
        update_data.pop(field, None)

    # Hash password if it's provided
    if "password" in update_data:
        # Allow password changes for local users OR users with allow_local_fallback
        can_change_password = current_user.auth_type == AUTH_TYPE_LOCAL or getattr(
            current_user, "allow_local_fallback", False
        )
        if not can_change_password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot change password for non-local users",
            )

        # Require current password verification before allowing password change
        current_password = update_data.pop("current_password", None)
        if not current_password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Current password is required to change password",
            )
        if not verify_password(current_password, str(current_user.hashed_password)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Current password is incorrect",
            )

        new_password = update_data.pop("password")

        # Check password history (FedRAMP IA-5)
        if not check_password_against_history(db, current_user.id, new_password):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Password has been used recently. Please choose a different password. "
                f"(Cannot reuse last {settings.PASSWORD_HISTORY_COUNT} passwords)",
            )

        new_hash = get_password_hash(new_password)
        update_data["hashed_password"] = new_hash
        update_data["password_changed_at"] = datetime.now(timezone.utc)

        # Store password in history after successful change
        add_password_to_history(db, current_user.id, new_hash)
        logger.info(f"Password changed for user {current_user.id}")
    else:
        # Remove current_password if no password change is being made
        update_data.pop("current_password", None)

    for field, value in update_data.items():
        setattr(current_user, field, value)

    db.commit()
    db.refresh(current_user)

    return current_user


@router.get("/search", response_model=list[UserSearchResult])
def search_users(
    q: str = Query(..., min_length=2, max_length=100, description="Search query (min 2 chars)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Search users by name or email for sharing autocomplete.

    Returns up to 20 results, excluding the current user.
    """
    from sqlalchemy import or_

    pattern = f"%{q}%"
    users = (
        db.query(User)
        .filter(
            User.id != current_user.id,
            User.is_active == True,  # noqa: E712
            or_(
                User.email.ilike(pattern),
                User.full_name.ilike(pattern),
            ),
        )
        .order_by(User.full_name, User.email)
        .limit(20)
        .all()
    )

    return [UserSearchResult(uuid=u.uuid, full_name=u.full_name, email=u.email) for u in users]


@router.get("/{user_uuid}", response_model=UserSchema)
def get_user(
    user_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """
    Get user by UUID (admin only)
    """
    # Uses helper that validates UUID format and returns 400 for invalid UUIDs
    return get_user_by_uuid(db, user_uuid)


@router.put("/{user_uuid}", response_model=UserSchema)
def update_user(
    user_uuid: str,
    user_update: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """
    Update user by UUID (admin only)
    """
    # Uses helper that validates UUID format and returns 400 for invalid UUIDs
    user = get_user_by_uuid(db, user_uuid)

    # Update fields — strip privilege-escalation fields unless caller is super_admin.
    # Regular admins can update names, emails, etc. but cannot promote users.
    # allow_local_fallback is also a super_admin-only field (security-critical).
    update_data = user_update.model_dump(exclude_unset=True)
    if current_user.role != "super_admin":
        privileged_fields = {
            "is_active",
            "is_superuser",
            "role",
            "auth_type",
            "allow_local_fallback",
        }
        stripped = [f for f in privileged_fields if f in update_data]
        for field in stripped:
            update_data.pop(field)
        if stripped:
            logger.warning(
                f"Admin {current_user.id} attempted to set privileged fields "
                f"{stripped} on user {user.id} — stripped"
            )

    # is_superuser is derived from role and is never settable directly. If a
    # super_admin changes the role, recompute is_superuser to keep the invariant
    # (enforced by the v369 DB CHECK constraint) intact.
    update_data.pop("is_superuser", None)
    if "role" in update_data:
        if update_data["role"] not in VALID_ROLES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid role: {update_data['role']}",
            )
        update_data["is_superuser"] = role_implies_superuser(update_data["role"])

    # Hash password if it's provided
    if "password" in update_data:
        # Allow password changes for local users OR users with allow_local_fallback
        can_change_password = user.auth_type == AUTH_TYPE_LOCAL or getattr(
            user, "allow_local_fallback", False
        )
        if not can_change_password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot change password for non-local users",
            )

        new_password = update_data.pop("password")

        # Check password history (FedRAMP IA-5) - admins must also comply
        if not check_password_against_history(db, user.id, new_password):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Password has been used recently. Please choose a different password. "
                f"(Cannot reuse last {settings.PASSWORD_HISTORY_COUNT} passwords)",
            )

        new_hash = get_password_hash(new_password)
        update_data["hashed_password"] = new_hash
        update_data["password_changed_at"] = datetime.now(timezone.utc)

        # Store password in history after successful change
        add_password_to_history(db, user.id, new_hash)
        logger.info(f"Admin {current_user.id} changed password for user {user.id}")

    # Remove current_password from update_data as it's not a model field
    update_data.pop("current_password", None)

    for field, value in update_data.items():
        setattr(user, field, value)

    db.commit()
    db.refresh(user)

    return user


@router.delete("/{user_uuid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """
    Delete user by UUID (admin only)
    """
    # Uses helper that validates UUID format and returns 400 for invalid UUIDs
    user = get_user_by_uuid(db, user_uuid)

    # Prevent deleting self
    if user.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete own user account",
        )

    # Use the comprehensive cleanup from the admin endpoint to avoid orphaned records.
    from app.api.endpoints.admin import _delete_user_media_files
    from app.api.endpoints.admin import _delete_user_owned_records
    from app.api.endpoints.admin import _delete_user_speakers

    user_id = user.id
    _delete_user_owned_records(db, user_id)
    _delete_user_speakers(db, user_id)
    _delete_user_media_files(db, user_id)

    db.delete(user)
    db.commit()

    return None
