"""Admin edit routes on another account: ``PUT`` and ``DELETE /users/{user_uuid}``.

Included into ``users.router`` after ``get_user``, preserving registration order.
"""

import logging
from datetime import UTC
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_admin_user
from app.api.endpoints.auth.lifecycle_gates import _get_client_info
from app.auth.account_linking import emails_agree
from app.auth.password_history import add_password_to_history
from app.auth.password_history import check_password_against_history
from app.auth.password_policy import password_policy
from app.auth.refusal_audit import audit_user_update_refusal
from app.auth.roles import ROLE_SUPER_ADMIN
from app.auth.roles import ROLE_USER
from app.auth.roles import VALID_ROLES
from app.auth.roles import role_implies_superuser
from app.core.security import get_password_hash
from app.db.base import get_db
from app.models.user import User
from app.schemas.user import User as UserSchema
from app.schemas.user import UserUpdate
from app.services.account_security_service import DeletedUser
from app.services.account_security_service import assert_local_fallback_settable
from app.services.account_security_service import assert_not_last_super_admin
from app.services.account_security_service import assert_not_last_super_admin_deactivation
from app.services.account_security_service import assert_password_auth_possible
from app.services.account_security_service import audit_account_status_change
from app.services.account_security_service import audit_expiration_change
from app.services.account_security_service import audit_password_change
from app.services.account_security_service import audit_role_change
from app.services.account_security_service import audit_user_deleted
from app.services.account_security_service import enforce_password_policy
from app.services.account_security_service import revoke_all_sessions
from app.services.user_deletion_service import delete_user_account
from app.services.uuid_helpers import get_user_by_uuid

logger = logging.getLogger(__name__)

router = APIRouter()


def _enforce_update_user_privilege_boundaries(
    user: User,
    current_user: User,
    update_data: dict[str, object],
    client_ip: str,
    user_agent: str,
) -> None:
    """Enforce ``update_user``'s two privilege boundaries, in place on ``update_data``.

    Split out of ``update_user`` to keep that function's branch count readable
    (ruff C901), matching the existing ``_validate_role_and_activation_changes``
    split just below it.

    1. A caller who is not ``super_admin`` may not write to a ``super_admin``
       account at all.
    2. This route never changes ``user.email``, for ANY caller — see
       ``update_user``'s docstring for the full rationale (issue #867). Resubmitting
       the current address (case/whitespace aside) is popped as a no-op rather than
       refused.

    Raises:
        HTTPException: 403, for either boundary.
    """
    if user.role == ROLE_SUPER_ADMIN and current_user.role != ROLE_SUPER_ADMIN:
        audit_user_update_refusal(
            "super_admin_target_denied", user, current_user, client_ip, user_agent
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a super_admin can modify a super_admin account",
        )

    if "email" not in update_data:
        return

    # update_data is typed dict[str, object] (it's shared with the caller's generic
    # setattr loop); the schema's EmailStr guarantees this value is a str at runtime.
    new_email = str(update_data["email"])
    if emails_agree(new_email, str(user.email)):
        # Same address, different case/whitespace: a no-op, not a change.
        update_data.pop("email")
        return

    audit_user_update_refusal("email_change_denied", user, current_user, client_ip, user_agent)
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            "This endpoint cannot change a user's email address. Use "
            "PUT /api/users/me for a self-service change (current password "
            "required), or PUT /api/admin/users/{uuid}/external-email "
            "(super_admin) to accept an identity provider's updated address "
            "for an already-linked account."
        ),
    )


@router.put("/{user_uuid}", response_model=UserSchema)
def update_user(
    request: Request,
    user_uuid: str,
    user_update: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> Any:
    """
    Update user by UUID (admin only).

    Two privilege boundaries this route enforces before touching anything else:

    1. **A caller who is not ``super_admin`` may not write to a ``super_admin``
       account at all** — mirrors ``delete_user``'s identical guard and
       ``admin.py``'s ``_validate_user_deletion``.
    2. **This route never changes ``user.email``, for ANY caller, including
       ``super_admin``.** ``auth/account_linking.py`` is the single implementation
       of "who may write a user's email", and it already names exactly two
       authorities: the account holder themselves, via ``PUT /users/me``
       (password-proven), and a super_admin accepting an identity provider's
       updated address for an already-linked account, via
       ``PUT /admin/users/{uuid}/external-email``. A third, unguarded writer here
       let a plain admin's edit (or a super_admin bypassing the password proof)
       repoint an account's login identity — the account-takeover shape #867 was
       written to close on the other two paths. Resubmitting the *current*
       address (case/whitespace aside) is treated as a no-op, not a refusal, so a
       form that round-trips what it read does not 403.
    """
    client_ip, user_agent = _get_client_info(request)
    # Uses helper that validates UUID format and returns 400 for invalid UUIDs
    user = get_user_by_uuid(db, user_uuid)
    update_data = user_update.model_dump(exclude_unset=True)
    _enforce_update_user_privilege_boundaries(
        user, current_user, update_data, client_ip, user_agent
    )

    old_role = str(user.role)
    old_expires_at = str(user.account_expires_at) if user.account_expires_at else None
    was_active = bool(user.is_active)

    # Update fields — strip privilege-escalation fields unless caller is super_admin.
    # Regular admins can update names, emails, etc. but cannot promote users.
    # allow_local_fallback is also a super_admin-only field (security-critical).
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
    _validate_role_and_activation_changes(db, user, update_data)

    # allow_local_fallback only means anything for accounts whose identity lives in
    # PKI or an OIDC provider. The UI hides the toggle elsewhere, but that is a
    # client-side check only, and on an LDAP row it was half of a password bypass.
    if update_data.get("allow_local_fallback"):
        assert_local_fallback_settable(str(update_data.get("auth_type") or user.auth_type))

    # Hash password if it's provided
    password_changed = "password" in update_data
    if password_changed:
        assert_password_auth_possible(user)

        new_password = update_data.pop("password")

        # Admins are not exempt from the policy — this path skipped it entirely.
        enforce_password_policy(new_password, user, db)

        # Check password history (FedRAMP IA-5) - admins must also comply.
        # The count in the message is the ENFORCED one — `password_policy.history_count`
        # resolves DB `auth_config` > .env > coded default, while the .env value this
        # used to interpolate is only the second of those three. A deployment that set
        # the DB value to 2 told the user "cannot reuse last 24".
        if not check_password_against_history(db, user.id, new_password):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Password has been used recently. Please choose a different password. "
                f"(Cannot reuse last {password_policy.history_count} passwords)",
            )

        new_hash = get_password_hash(new_password)
        update_data["hashed_password"] = new_hash
        update_data["password_changed_at"] = datetime.now(UTC)

        # Store password in history after successful change
        add_password_to_history(db, user.id, new_hash)

        # The admin now knows a working credential for someone else's account, so
        # it must not stay that account's password — same rule create_user applies.
        # Not applied when an admin edits their OWN row through this route: they
        # chose it themselves, and forcing a second change would be a loop.
        if user.id != current_user.id:
            update_data["must_change_password"] = True

        logger.info(f"Admin {current_user.id} changed password for user {user.id}")

    # Remove current_password from update_data as it's not a model field
    update_data.pop("current_password", None)

    for field, value in update_data.items():
        setattr(user, field, value)

    role_changed = "role" in update_data and str(user.role) != old_role
    deactivated = was_active and not bool(user.is_active)
    identity_changed = bool({"auth_type", "allow_local_fallback"} & set(update_data))

    # An admin demoting, disabling or re-crediting an account is usually reacting
    # to something; leaving that account's existing sessions alive defeats it.
    if password_changed or role_changed or deactivated or identity_changed:
        revoke_all_sessions(db, user, reason="admin account change")

    db.commit()
    db.refresh(user)

    if password_changed:
        audit_password_change(user, current_user, client_ip, user_agent)
    if role_changed:
        audit_role_change(user, current_user, old_role, str(user.role), client_ip, user_agent)
    if was_active != bool(user.is_active):
        audit_account_status_change(user, current_user, bool(user.is_active), client_ip, user_agent)
    if "account_expires_at" in update_data:
        _audit_expiration_if_changed(user, current_user, old_expires_at, client_ip, user_agent)

    return user


def _validate_role_and_activation_changes(
    db: Session, user: User, update_data: dict[str, object]
) -> None:
    """Validate a role change and/or deactivation on ``update_data`` in place.

    Split out of ``update_user`` to keep that function's branch count readable
    (ruff C901). Mutates ``update_data["is_superuser"]`` when the role changes,
    and raises if either change would leave the deployment with no active
    super_admin.
    """
    if "role" in update_data:
        if update_data["role"] not in VALID_ROLES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid role: {update_data['role']}",
            )
        update_data["is_superuser"] = role_implies_superuser(update_data["role"])
        assert_not_last_super_admin(db, user, update_data["role"])

    if update_data.get("is_active") is False:
        assert_not_last_super_admin_deactivation(db, user)


def _audit_expiration_if_changed(
    user: User, actor: User, old_expires_at: str | None, client_ip: str, user_agent: str
) -> None:
    """Split out of ``update_user`` to keep that function's branch count readable."""
    new_expires_at = str(user.account_expires_at) if user.account_expires_at else None
    if new_expires_at != old_expires_at:
        audit_expiration_change(user, actor, old_expires_at, new_expires_at, client_ip, user_agent)


@router.delete("/{user_uuid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    request: Request,
    user_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> None:
    """
    Delete user by UUID (admin only)
    """
    client_ip, user_agent = _get_client_info(request)
    # Uses helper that validates UUID format and returns 400 for invalid UUIDs
    user = get_user_by_uuid(db, user_uuid)

    # Prevent deleting self
    if user.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete own user account",
        )

    # Only a super_admin may delete a super_admin, and never the last one.
    if str(user.role) == ROLE_SUPER_ADMIN:
        if str(current_user.role) != ROLE_SUPER_ADMIN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only a super_admin can delete a super_admin account",
            )
        assert_not_last_super_admin(db, user, ROLE_USER)

    # Capture what the audit record needs before the row is gone.
    deleted_snapshot = DeletedUser.of(user)

    # The cascade shared with DELETE /admin/users/{uuid}: held-file refusal, purge
    # plan, the savepoint-wrapped passes, commit, then the external purge.
    residual_errors = delete_user_account(db, user)

    # ADMIN_USER_DELETE existed as an event type with no emitter anywhere.
    audit_user_deleted(deleted_snapshot, current_user, client_ip, user_agent, residual_errors)

    return None
