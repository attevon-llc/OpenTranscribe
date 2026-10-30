"""Admin account-management routes (FedRAMP AC-2): passwords, lockout, sessions, roles."""

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
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_active_superuser
from app.api.endpoints.auth import get_current_admin_user
from app.api.endpoints.auth.dependencies import _get_client_info
from app.auth.audit import audit_logger
from app.auth.audit_events import AuditEventType
from app.auth.audit_events import AuditOutcome
from app.auth.lockout import current_store_kind as lockout_current_store_kind
from app.auth.lockout import list_locked_accounts as lockout_list_locked_accounts
from app.auth.lockout import normalize_identifier as lockout_normalize_identifier
from app.auth.lockout import reset_lockout_state as lockout_reset_lockout_state
from app.auth.lockout import unlock_account as lockout_unlock_account
from app.auth.password_history import add_password_to_history
from app.auth.password_history import check_password_against_history
from app.auth.rate_limit import get_auth_rate_limit
from app.auth.rate_limit import limiter
from app.auth.roles import ROLE_SUPER_ADMIN
from app.auth.roles import VALID_ROLES
from app.auth.roles import role_implies_superuser
from app.core.security import get_password_hash
from app.db.base import get_db
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.schemas.admin import LockedAccount
from app.schemas.admin import LockedAccountsList
from app.schemas.admin import LockoutResetResponse
from app.schemas.user import AdminPasswordResetRequest
from app.services.account_security_service import assert_password_auth_possible
from app.services.account_security_service import audit_password_change
from app.services.account_security_service import audit_role_change
from app.services.account_security_service import enforce_password_policy
from app.services.account_security_service import revoke_all_sessions

# No basicConfig here — this module is imported via the API router before
# configure_logging() runs; a default root handler would double every log line.
logger = logging.getLogger(__name__)

router = APIRouter()


# ============== Account Management (FedRAMP AC-2) ==============


@router.post("/users/{user_uuid}/reset-password")
@limiter.limit(get_auth_rate_limit())
def admin_reset_user_password(
    request: Request,
    response: Response,
    user_uuid: str,
    request_body: AdminPasswordResetRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
):
    """Admin-initiated password reset. Requires super_admin role.

    Security: Password is passed in request body (not query parameter) to prevent
    exposure in server logs, browser history, and HTTP referrer headers.

    This path used to skip every control its sibling in ``users.py`` applies: it
    set a hash on ANY account regardless of ``auth_type`` (planting a local
    password on a directory-managed row), bypassed the password policy and the
    reuse history in both directions, and left the target's existing sessions
    alive — so an admin resetting a compromised account did not actually evict
    the attacker.
    """
    client_ip, user_agent = _get_client_info(request)
    user = db.query(User).filter(User.uuid == user_uuid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    assert_password_auth_possible(user)
    enforce_password_policy(request_body.new_password, user, db)

    if not check_password_against_history(db, user.id, request_body.new_password):
        raise HTTPException(
            status_code=400,
            detail="Password has been used recently. Please choose a different password.",
        )

    new_hash = get_password_hash(request_body.new_password)
    user.hashed_password = new_hash  # type: ignore[assignment]
    user.must_change_password = request_body.force_change  # type: ignore[assignment]
    user.password_changed_at = datetime.now(UTC)  # type: ignore[assignment]
    add_password_to_history(db, user.id, new_hash)
    revoke_all_sessions(db, user, reason="admin password reset")
    db.commit()

    audit_password_change(
        user, current_user, client_ip, user_agent, forced=request_body.force_change
    )

    return {"success": True}


@router.post("/users/{user_uuid}/unlock")
def admin_unlock_account(
    request: Request,
    user_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Admin unlock of a locked account — the true inverse of ``/lock``.

    Two different things can stop a user signing in, and this endpoint clears
    both:

    * the **failed-login lockout** (progressive, per-identifier, in Redis), and
    * ``is_active = False``, which is what ``/lock`` sets.

    It previously cleared only the first, so an account locked by an admin could
    not be unlocked by the paired endpoint at all — the only way back was
    ``PUT /users/{uuid}`` with ``is_active``. Two endpoints named lock/unlock
    that are not inverses is a trap, not an API.
    """
    client_ip, user_agent = _get_client_info(request)

    user = db.query(User).filter(User.uuid == user_uuid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Use the lockout manager to clear the failed-login lockout
    unlocked = lockout_unlock_account(str(user.email))

    was_disabled = not bool(user.is_active)
    if was_disabled:
        user.is_active = True  # type: ignore[assignment]
        db.commit()

    audit_logger.log(
        event_type=AuditEventType.AUTH_ACCOUNT_UNLOCK,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        outcome=AuditOutcome.SUCCESS,
        # issue #828: this is a SECOND AUTH_ACCOUNT_UNLOCK emitter (the other is
        # account_security_service.audit_account_status_change) and it reproduced the
        # exact pre-#443 shape — the target only in `details.target_user` (a UUID
        # string), invisible to `query_audit_logs`. Top-level, typed, like every other
        # administrative emitter.
        target_user_id=int(user.id),
        target_username=str(user.email),
        details={
            "target_user": user_uuid,
            "unlocked_by": "admin",
            "was_locked": unlocked,
            "was_disabled": was_disabled,
        },
    )

    return {"success": True, "was_locked": unlocked, "was_disabled": was_disabled}


@router.get("/locked-accounts", response_model=LockedAccountsList)
def admin_list_locked_accounts(
    cursor: str | None = Query(None, description="Opaque pagination cursor from a prior page."),
    limit: int = Query(100, ge=1, le=500),
    include_unlocked: bool = Query(
        False,
        description=(
            "Also include recent failed-login records that are not currently "
            "locked -- useful for diagnosing a near-threshold account."
        ),
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """List locked accounts (issue #570), cursor-paginated over the lockout store.

    A **read**, and deliberately not audited per request -- the write below
    (the reset) is. ``admin`` tier, matching the existing lock/unlock pair
    (J-A2): a reset whose target an admin cannot see would be worse than a
    listing a super_admin-only tier would otherwise require.
    """
    accounts, next_cursor, lockout_enabled = lockout_list_locked_accounts(
        cursor, limit, include_unlocked=include_unlocked
    )

    # One batched enrichment query over the page's identifiers, never a
    # per-row lookup (issue #570 §A.4.1).
    identifiers = [a["identifier"] for a in accounts]
    by_email: dict[str, User] = {}
    if identifiers:
        for u in db.query(User).filter(func.lower(User.email).in_(identifiers)).all():
            by_email[str(u.email).lower()] = u

    enriched = []
    for account in accounts:
        matched = by_email.get(account["identifier"])
        enriched.append(
            LockedAccount(
                identifier=account["identifier"],
                is_locked=account["is_locked"],
                failed_attempts=account["failed_attempts"],
                lockout_count=account["lockout_count"],
                locked_until=account["locked_until"],
                first_failed_attempt=account["first_failed_attempt"],
                last_failed_attempt=account["last_failed_attempt"],
                admin_unlocked_at=account["admin_unlocked_at"],
                user_uuid=str(matched.uuid) if matched is not None else None,
                full_name=str(matched.full_name) if matched is not None else None,
                is_active=bool(matched.is_active) if matched is not None else None,
                auth_type=str(matched.auth_type) if matched is not None else None,
            )
        )

    return LockedAccountsList(
        accounts=enriched,
        next_cursor=next_cursor,
        lockout_enabled=lockout_enabled,
        store_backend=lockout_current_store_kind(),
        truncated=next_cursor is not None,
    )


@router.post("/locked-accounts/{identifier}/reset", response_model=LockoutResetResponse)
def admin_reset_lockout_counter(
    identifier: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Reset an account's progressive lockout counter (issue #570).

    Distinct from ``POST /admin/users/{uuid}/unlock`` (T8): this clears the
    progressive ``lockout_count`` and ``failed_attempts`` but, per J-A1, leaves
    an in-progress ``locked_until`` intact -- the single "unlock now" verb stays
    the existing endpoint. 404 for an unknown identifier (nothing is written);
    409 when the compare-and-set budget is exhausted (also nothing written --
    never a silent partial write).
    """
    client_ip, user_agent = _get_client_info(request)
    normalized = lockout_normalize_identifier(identifier)

    outcome = lockout_reset_lockout_state(normalized, clear_lock=False)
    if not outcome["found"]:
        raise HTTPException(status_code=404, detail="No lockout record for this identifier")
    if not outcome["written"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Could not reset lockout state (concurrent write); retry",
        )

    matched = db.query(User).filter(func.lower(User.email) == normalized).first()

    audit_logger.log(
        event_type=AuditEventType.AUTH_LOCKOUT_COUNTER_RESET,
        outcome=AuditOutcome.SUCCESS,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        target_user_id=int(matched.id) if matched is not None else None,
        target_username=normalized,
        details={
            "previous_lockout_count": outcome["previous_lockout_count"],
            "was_locked": outcome["was_locked"],
        },
    )

    return LockoutResetResponse(
        success=True,
        identifier=normalized,
        previous_lockout_count=outcome["previous_lockout_count"],
        was_locked=outcome["was_locked"],
        unlocked=outcome["unlocked"],
    )


def _targets_a_super_admin_without_authority(user: User, current_user: User) -> bool:
    """Whether ``current_user`` (not super_admin) is acting on a super_admin ``user``.

    Shared by ``admin_lock_account`` and ``admin_terminate_user_sessions`` — both were
    gated only at the admin tier, which let a plain admin lock, or force-logout the
    sessions of, every super_admin account: a deployment-wide lockout reachable through
    two endpoints that were never meant to reach that far (issue #867 finding (e), same
    shape as ``_validate_user_deletion`` above and ``update_user``'s guard in
    ``users.py``).
    """
    return str(user.role) == ROLE_SUPER_ADMIN and str(current_user.role) != ROLE_SUPER_ADMIN


@router.post("/users/{user_uuid}/lock")
def admin_lock_account(
    request: Request,
    user_uuid: str,
    reason: str = Query("Admin action", description="Reason for locking the account"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Admin lock of user account. Only a super_admin may lock a super_admin."""
    client_ip, user_agent = _get_client_info(request)

    user = db.query(User).filter(User.uuid == user_uuid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if _targets_a_super_admin_without_authority(user, current_user):
        audit_logger.log(
            event_type=AuditEventType.AUTH_ACCOUNT_DISABLED,
            outcome=AuditOutcome.FAILURE,
            user_id=current_user.id,
            username=str(current_user.email),
            source_ip=client_ip,
            user_agent=user_agent,
            target_user_id=int(user.id),
            target_username=str(user.email),
            details={"target_user": user_uuid, "reason": reason, "action": "lock_denied"},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a super_admin can lock a super_admin account",
        )

    user.is_active = False  # type: ignore[assignment]
    # Locking an account that keeps a live refresh token is not a lock: token
    # rotation would carry the session past the lock for its full lifetime.
    revoke_all_sessions(db, user, reason="account locked by admin")
    db.commit()

    audit_logger.log(
        event_type=AuditEventType.AUTH_ACCOUNT_DISABLED,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        outcome=AuditOutcome.SUCCESS,
        target_user_id=int(user.id),
        target_username=str(user.email),
        details={
            "target_user": user_uuid,
            "reason": reason,
            "locked_by": "admin",
        },
    )

    return {"success": True}


@router.delete("/users/{user_uuid}/sessions")
def admin_terminate_user_sessions(
    request: Request,
    user_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Force logout user by terminating all sessions.

    Goes through ``revoke_all_sessions`` — i.e. ``token_service`` — like every
    other revocation path. It used to set ``RefreshToken.revoked_at`` inline,
    which made it the one termination that wrote **no Redis blacklist entry and
    no per-user revocation epoch**. That is a correctness bug before it is an
    audit one: the epoch is the only thing that reaches already-issued *access*
    tokens (they are stateless, so there is no row to revoke), so an admin force-
    logging-out a compromised account left it authenticated for the remaining
    access-token lifetime.
    """
    client_ip, user_agent = _get_client_info(request)

    user = db.query(User).filter(User.uuid == user_uuid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if _targets_a_super_admin_without_authority(user, current_user):
        audit_logger.log(
            event_type=AuditEventType.AUTH_LOGOUT_ALL,
            outcome=AuditOutcome.FAILURE,
            user_id=current_user.id,
            username=str(current_user.email),
            source_ip=client_ip,
            user_agent=user_agent,
            target_user_id=int(user.id),
            target_username=str(user.email),
            details={"target_user": user_uuid, "action": "terminate_sessions_denied"},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a super_admin can terminate a super_admin account's sessions",
        )

    count = revoke_all_sessions(db, user, reason="admin session termination")
    db.commit()

    audit_logger.log(
        event_type=AuditEventType.AUTH_LOGOUT_ALL,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        outcome=AuditOutcome.SUCCESS,
        details={
            "target_user": user_uuid,
            "sessions_terminated": count,
            "terminated_by": "admin",
        },
    )

    return {"success": True, "sessions_terminated": count}


@router.get("/users/{user_uuid}/sessions")
def admin_get_user_sessions(
    user_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """View all active sessions for a user."""
    user = db.query(User).filter(User.uuid == user_uuid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    sessions = (
        db.query(RefreshToken)
        .filter(
            RefreshToken.user_id == user.id,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > datetime.now(UTC),
        )
        .all()
    )

    return {
        "sessions": [
            {
                "id": str(s.jti),
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "expires_at": s.expires_at.isoformat() if s.expires_at else None,
                "ip_address": s.ip_address,
                "user_agent": s.user_agent,
            }
            for s in sessions
        ]
    }


@router.put("/users/{user_uuid}/role")
def admin_change_user_role(
    request: Request,
    user_uuid: str,
    new_role: str = Query(..., description="New role for the user (user, admin, super_admin)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
):
    """Change user role. Only super_admin can promote to super_admin."""
    client_ip, user_agent = _get_client_info(request)
    if new_role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail="Invalid role")

    user = db.query(User).filter(User.uuid == user_uuid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if str(user.uuid) == str(current_user.uuid):
        raise HTTPException(status_code=400, detail="Cannot change your own role")

    from app.api.endpoints.users import _assert_not_last_super_admin

    _assert_not_last_super_admin(db, user, new_role)

    old_role = user.role
    user.role = new_role  # type: ignore[assignment]
    # Keep is_superuser in sync with role (derived; v369 CHECK enforces it).
    user.is_superuser = role_implies_superuser(new_role)
    # A role change is usually a reaction to something; the target's existing
    # sessions must not outlive it.
    revoke_all_sessions(db, user, reason="role change")
    db.commit()

    audit_role_change(user, current_user, str(old_role), new_role, client_ip, user_agent)

    return {"success": True, "old_role": old_role, "new_role": new_role}
