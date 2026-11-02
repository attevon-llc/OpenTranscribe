"""Self-service account routes: ``GET /users/me`` and ``PUT /users/me``.

Included into ``users.router`` right after ``list_users`` so ``/me`` registers
before ``GET /users/{user_uuid}`` would capture it.
"""

import logging
from datetime import UTC
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import status
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.auth.lifecycle_gates import _get_client_info
from app.auth.account_linking import emails_agree
from app.auth.audit import audit_logger
from app.auth.audit_events import AuditEventType
from app.auth.audit_events import AuditOutcome
from app.auth.email_verification import email_verification_required
from app.auth.email_verification import issue_verification_token
from app.auth.password_history import add_password_to_history
from app.auth.password_history import check_password_against_history
from app.auth.password_policy import password_min_age_remaining
from app.auth.password_policy import password_policy
from app.core.security import get_password_hash
from app.core.security import verify_password
from app.db.base import get_db
from app.models.user import User
from app.schemas.user import User as UserSchema
from app.schemas.user import UserUpdate
from app.services.account_security_service import assert_password_auth_possible
from app.services.account_security_service import audit_password_change
from app.services.account_security_service import enforce_password_policy
from app.services.account_security_service import notify_email_changed
from app.services.account_security_service import reissue_current_session
from app.services.account_security_service import revoke_all_sessions

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/me", response_model=UserSchema)
def get_current_user_info(current_user: User = Depends(get_current_active_user)) -> Any:
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
    remaining = password_min_age_remaining(user.password_last_self_changed_at)
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


def _reset_mailbox_verification(current_user: User, mailbox_changed: bool) -> None:
    """Clear the verified flag when the address itself changed (issue #909).

    ``email_verified`` is proof THIS deployment mailed the address on the row and
    someone holding it came back — a property of the ADDRESS, not the account, so
    it cannot survive the address changing. Extracted so the caller's branch count
    doesn't grow with every credential-grade field this endpoint handles.
    """
    if not mailbox_changed:
        return
    current_user.email_verified = False
    current_user.email_verified_at = None


def _maybe_issue_reverification(
    db: Session, current_user: User, mailbox_changed: bool, client_ip: str
) -> None:
    """Mail a fresh verification link for the new address, when required.

    Reuses ``auth/email_verification.py`` wholesale: token creation, the 3/hour
    cap, hashing, delivery, and ``EmailDeliveryError`` absorption all live there.
    No-ops for a non-local account and for an already-verified row, so it MUST
    run after the caller has committed the flag reset above. This does not lock
    the caller out: the verification gate only fires at LOGIN, and the caller
    already handed them a fresh session via ``reissue_current_session``; an
    active super_admin is fully exempt from the gate.
    """
    if mailbox_changed and email_verification_required(db):
        issue_verification_token(db, current_user, client_ip)


@router.put("/me", response_model=UserSchema)
def update_current_user(
    request: Request,
    response: Response,
    user_update: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Any:
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
    # Byte-exact — deliberately NOT relaxed to `emails_agree`. This flag gates the
    # uniqueness check, the current-password requirement, and session revocation,
    # so widening it would let a case-only rewrite through without a password.
    email_changed = bool(user_update.email and user_update.email != current_user.email)
    # Narrower than `email_changed`: a case/whitespace-only resubmission is still
    # the SAME mailbox, so it must not un-verify an address that was never actually
    # changed (issue #909).
    mailbox_changed = email_changed and not emails_agree(str(user_update.email), old_email)
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
        enforce_password_policy(new_password, current_user, db)

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
        # The minimum-age clock: only the holder's own change starts it.
        update_data["password_last_self_changed_at"] = update_data["password_changed_at"]

        # Store password in history after successful change
        add_password_to_history(db, current_user.id, new_hash)
        logger.info(f"Password changed for user {current_user.id}")

    for field, value in update_data.items():
        setattr(current_user, field, value)

    _reset_mailbox_verification(current_user, mailbox_changed)

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
            db, current_user, response, request, user_agent=user_agent, ip_address=client_ip
        )

    _maybe_issue_reverification(db, current_user, mailbox_changed, client_ip)

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
