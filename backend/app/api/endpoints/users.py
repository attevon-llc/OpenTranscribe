import logging
from datetime import UTC
from datetime import datetime

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi import status
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.auth import get_current_admin_user
from app.api.endpoints.auth.dependencies import _get_client_info
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.constants import AUTH_TYPE_LOCAL
from app.auth.constants import EXTERNAL_AUTH_NO_PASSWORD
from app.auth.constants import VALID_AUTH_TYPES
from app.auth.password_history import add_password_to_history
from app.auth.password_history import check_password_against_history
from app.auth.password_policy import password_min_age_remaining
from app.auth.password_policy import password_policy
from app.auth.roles import ROLE_SUPER_ADMIN
from app.auth.roles import ROLE_USER
from app.auth.roles import VALID_ROLES
from app.auth.roles import role_implies_superuser
from app.auth.utils import local_password_allowed
from app.core.security import get_password_hash
from app.core.security import verify_password
from app.db.base import get_db
from app.models.user import User
from app.schemas.user import User as UserSchema
from app.schemas.user import UserCreate
from app.schemas.user import UserSearchResult
from app.schemas.user import UserUpdate
from app.services.account_security_service import DeletedUser
from app.services.account_security_service import assert_local_fallback_settable
from app.services.account_security_service import assert_password_auth_possible
from app.services.account_security_service import audit_account_status_change
from app.services.account_security_service import audit_expiration_change
from app.services.account_security_service import audit_password_change
from app.services.account_security_service import audit_role_change
from app.services.account_security_service import audit_user_deleted
from app.services.account_security_service import enforce_password_policy
from app.services.account_security_service import notify_email_changed
from app.services.account_security_service import reissue_current_session
from app.services.account_security_service import revoke_all_sessions
from app.utils.uuid_helpers import get_user_by_uuid

logger = logging.getLogger(__name__)

router = APIRouter()


def create_user(user_data: UserCreate, db: Session) -> User:
    """Create a new user (admin provisioning path).

    Called from ``admin.create_admin_user``. It is the direct-provisioning
    counterpart to the invitation flow (``auth/invitations.py``), which is the
    preferred path because it never has an admin choose someone else's password.

    Three gaps this used to have, all of which made "disable self-registration"
    unusable in practice (v375):

    * ``auth_type`` could not be set, so every admin-created account was
      ``local`` — unable to log in at all where local passwords are off.
    * No ``password_changed_at``, no password-history row: password expiry and
      reuse prevention (FedRAMP IA-5) both start from missing data.
    * A local account got a password the *admin* chose and no forced change, so
      the admin permanently knew a working credential for someone else's
      account. ``must_change_password`` is now set on that path.

    Args:
        user_data: Validated create payload. ``password`` is present only for
            ``auth_type == "local"`` (enforced by the schema).
        db: Database session.

    Returns:
        The created user.

    Raises:
        HTTPException: 400 if the email is taken or the role is invalid.
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

    auth_type = user_data.auth_type or AUTH_TYPE_LOCAL
    if auth_type not in VALID_AUTH_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid auth_type: {auth_type}",
        )

    # local_password_allowed is the single source of truth for whether an account
    # may hold a local password. A freshly created account never has
    # allow_local_fallback, so pki/oidc/ldap all land on the placeholder.
    holds_local_password, _reason = local_password_allowed(auth_type, False)
    now = datetime.now(UTC)

    if holds_local_password:
        if not user_data.password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Password is required for local accounts",
            )
        password_hash = get_password_hash(user_data.password)
    else:
        password_hash = EXTERNAL_AUTH_NO_PASSWORD

    new_user = User(
        email=user_data.email,
        hashed_password=password_hash,
        full_name=user_data.full_name,
        is_active=user_data.is_active if user_data.is_active is not None else True,
        role=role,
        is_superuser=role_implies_superuser(role),
        auth_type=auth_type,
        password_changed_at=now if holds_local_password else None,
        # The admin knows this password. It must not stay the account's password.
        must_change_password=holds_local_password,
    )

    db.add(new_user)
    db.flush()

    if holds_local_password:
        # Without this row the initial password is invisible to reuse checks.
        add_password_to_history(db, int(new_user.id), password_hash)

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
    """The caller's own account record.

    The SPA's identity call after a successful login, and the canonical "who am I"
    probe for a script or agent holding a session cookie — a 200 here confirms both a
    valid session and an account that passes the lifecycle gates. Any active user;
    it returns only the caller's own row, so there is no privilege check to make.

    Distinct from ``GET /api/auth/session``, which is the SPA's *anonymous-safe*
    probe and answers 200 with no user. This one 401s when unauthenticated.
    ``get_current_active_user`` also rejects an account that is inactive, expired, or
    flagged ``must_change_password``, so this is not merely a token-decode.
    """
    return current_user


def _assert_password_old_enough_to_change(user: User) -> None:
    """Refuse a voluntary password change made too soon after the last one.

    Split out of ``update_current_user`` so the branch count there stays readable
    and so the refusal message has one owner.

    Args:
        user: The account whose password is being changed.

    Raises:
        HTTPException: 400 while the minimum age has not elapsed.
    """
    remaining = password_min_age_remaining(user.password_changed_at)
    if remaining is None:
        return

    hours = max(1, -(-int(remaining.total_seconds()) // 3600))
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=(
            f"Password was changed too recently. It must be kept for at least "
            f"{password_policy.min_age_hours} hours before it can be changed again "
            f"(about {hours} more to go). Ask an administrator to reset it if you "
            f"believe it has been compromised."
        ),
    )


@router.put("/me", response_model=UserSchema)
def update_current_user(
    request: Request,
    response: Response,
    user_update: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """Update the caller's own profile.

    A password or email change revokes every session (AC-12) and then re-issues
    this one, so the caller stays signed in on the device that made the change and
    is signed out everywhere else. A password change also clears
    ``must_change_password`` — this is the only non-email path that does.
    """
    client_ip, user_agent = _get_client_info(request)
    update_data = user_update.model_dump(exclude_unset=True)

    # A password change and an email change are both credential-grade operations,
    # so each needs the current password. Pull it out once — it is not a model field.
    current_password = update_data.pop("current_password", None)

    def _require_current_password() -> None:
        if not current_password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Current password is required for this change",
            )
        if not verify_password(current_password, str(current_user.hashed_password)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Current password is incorrect",
            )

    # Check if email is being changed and is already taken
    old_email = str(current_user.email)
    email_changed = bool(user_update.email and user_update.email != current_user.email)
    if email_changed:
        existing_user = db.query(User).filter(User.email == user_update.email).first()
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered",
            )
        # Changing the address and then requesting a password reset is a complete
        # account takeover. Proving possession of the current password stops a
        # hijacked session from doing it silently.
        assert_password_auth_possible(current_user)
        _require_current_password()

    # Update fields — strip privileged fields that only admins may change.
    # Without this, any user could promote themselves via PUT /users/me.
    privileged_fields = {"is_active", "is_superuser", "role", "auth_type", "allow_local_fallback"}
    for field in privileged_fields:
        update_data.pop(field, None)

    # Hash password if it's provided
    password_changed = "password" in update_data
    if password_changed:
        assert_password_auth_possible(current_user)
        _require_current_password()

        new_password = update_data.pop("password")

        # Policy was previously enforced only by UserCreate's validator, so a
        # self-service change could set a password the policy forbids.
        enforce_password_policy(new_password, current_user)

        # Minimum password age (FedRAMP IA-5(1)(d)). Without it the bounded history
        # is self-defeating: nothing rate-limits this endpoint, so a user could run
        # `password_history_count` throwaway changes back to back, flush the row
        # holding their original out of the retained window, and set the original
        # again — the reuse control defeated with no privilege at all.
        #
        # Applied here and NOT on the admin paths (`update_user` below,
        # `admin.reset_user_password`, the emailed reset) on purpose: this is a
        # restriction on *voluntary* change, and an administrator recovering an
        # account must never be blocked by the age of a password they are replacing.
        # `must_change_password` is exempt for the same reason — a user held for a
        # forced change is being told to change a password that was, by definition,
        # just set. Refusing them here would be a lockout with no exit.
        if not current_user.must_change_password:
            _assert_password_old_enough_to_change(current_user)

        # Check password history (FedRAMP IA-5)
        if not check_password_against_history(db, current_user.id, new_password):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Password has been used recently. Please choose a different password. "
                f"(Cannot reuse last {password_policy.history_count} passwords)",
            )

        new_hash = get_password_hash(new_password)
        update_data["hashed_password"] = new_hash
        update_data["password_changed_at"] = datetime.now(UTC)

        # Store password in history after successful change
        add_password_to_history(db, current_user.id, new_hash)
        logger.info(f"Password changed for user {current_user.id}")

    for field, value in update_data.items():
        setattr(current_user, field, value)

    if password_changed:
        # THE thing that ends a forced-change hold. Three paths set this flag
        # (admin create, admin force-change, password expiry at login) and until
        # now exactly one cleared it — the emailed reset — so a deployment with no
        # mail transport had no exit at all: change the password, get held again,
        # and after PASSWORD_HISTORY_COUNT attempts run out of reusable passwords.
        current_user.must_change_password = False

    # Both changes invalidate every other session: an attacker holding one keeps
    # it through the victim's password change otherwise. In-transaction so a
    # commit failure below rolls the revocation back with it.
    if password_changed or email_changed:
        revoke_all_sessions(
            db,
            current_user,
            reason="password change" if password_changed else "email change",
        )

    db.commit()
    db.refresh(current_user)

    if password_changed or email_changed:
        # The revocation above is total and includes THIS session. Hand the caller
        # a fresh one rather than signing them out of the flow they just completed.
        reissue_current_session(
            db, current_user, response, user_agent=user_agent, ip_address=client_ip
        )

    if password_changed:
        audit_password_change(current_user, current_user, client_ip, user_agent)
    if email_changed:
        audit_logger.log(
            event_type=AuditEventType.ADMIN_USER_UPDATE,
            outcome=AuditOutcome.SUCCESS,
            user_id=current_user.id,
            username=str(current_user.email),
            source_ip=client_ip,
            user_agent=user_agent,
            details={"action": "email_change", "old_email": old_email},
        )
        notify_email_changed(old_email, str(current_user.email))

    return current_user


@router.get("/search", response_model=list[UserSearchResult])
def search_users(
    q: str = Query(..., min_length=2, max_length=100, description="Search query (min 2 chars)"),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
):
    """Search users by name or email for sharing autocomplete.

    Returns up to 20 results, excluding the caller.

    **Tenant-gated.** This is the only plain-``user``-tier route that reads other
    accounts' identities, and it carried no tenant scope at all — not even the
    ``UNSCOPED`` sentinel, because no ``RequestContext`` reached it. In an org
    deployment that made it a cross-tenant directory: any authenticated member of
    any tenant could page every *other* tenant's active accounts — email plus
    full name — out of it, 20 at a time, from a two-character query, with no
    admin privilege and nothing in the response to say the rows came from
    somewhere else.

    The gate mirrors ``scope_to_context``: in org context only members of THAT
    org are candidates; in personal scope only accounts with no org membership
    are. Community-edition invariance holds exactly — the membership table is
    empty there, so every account is in personal scope and the result set is
    unchanged.
    """
    from sqlalchemy import exists
    from sqlalchemy import or_

    from app.models.organization import OrganizationMembership

    # Correlated EXISTS against the outer `user` row, so the tenant gate is one
    # subquery rather than a join that could duplicate rows per membership.
    caller_org_member = exists().where(
        OrganizationMembership.user_id == User.id,
        OrganizationMembership.organization_id == ctx.org_id,
    )
    any_org_member = exists().where(OrganizationMembership.user_id == User.id)
    tenant_gate = caller_org_member if ctx.is_org_context else ~any_org_member

    pattern = f"%{q}%"
    users = (
        db.query(User)
        .filter(
            User.id != ctx.user.id,
            User.is_active == True,  # noqa: E712
            tenant_gate,
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
    request: Request,
    user_uuid: str,
    user_update: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """
    Update user by UUID (admin only)
    """
    client_ip, user_agent = _get_client_info(request)
    # Uses helper that validates UUID format and returns 400 for invalid UUIDs
    user = get_user_by_uuid(db, user_uuid)
    old_role = str(user.role)
    old_expires_at = str(user.account_expires_at) if user.account_expires_at else None
    was_active = bool(user.is_active)

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
        _assert_not_last_super_admin(db, user, update_data["role"])

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
        enforce_password_policy(new_password, user)

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


def _audit_expiration_if_changed(
    user: User, actor: User, old_expires_at: str | None, client_ip: str, user_agent: str
) -> None:
    """Split out of ``update_user`` to keep that function's branch count readable."""
    new_expires_at = str(user.account_expires_at) if user.account_expires_at else None
    if new_expires_at != old_expires_at:
        audit_expiration_change(user, actor, old_expires_at, new_expires_at, client_ip, user_agent)


def _assert_not_last_super_admin(db: Session, user: User, new_role: str) -> None:
    """Refuse a change that would leave the deployment with no super_admin.

    Auth configuration, role changes and the audit log are all super_admin-gated,
    so demoting the last one locks everyone out of them permanently — there is no
    recovery path short of editing the database by hand.
    """
    if str(user.role) != ROLE_SUPER_ADMIN or new_role == ROLE_SUPER_ADMIN:
        return

    remaining = (
        db.query(User)
        .filter(User.role == ROLE_SUPER_ADMIN, User.id != user.id, User.is_active.is_(True))
        .count()
    )
    if remaining == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot demote the last super_admin — promote another account first.",
        )


@router.delete("/{user_uuid}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    request: Request,
    user_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
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
        _assert_not_last_super_admin(db, user, ROLE_USER)

    # Capture what the audit record needs before the row is gone.
    deleted_snapshot = DeletedUser.of(user)

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

    # ADMIN_USER_DELETE existed as an event type with no emitter anywhere.
    audit_user_deleted(deleted_snapshot, current_user, client_ip, user_agent)

    return None
