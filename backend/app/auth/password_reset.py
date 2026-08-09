"""Password reset logic with anti-enumeration and rate limiting."""

import hashlib
import logging
import secrets
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from sqlalchemy.orm import Session

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.constants import AUTH_TYPE_LOCAL
from app.auth.password_history import add_password_to_history
from app.auth.password_history import check_password_against_history
from app.auth.password_policy import validate_password
from app.auth.token_service import token_service
from app.core.config import settings
from app.core.security import get_password_hash
from app.models.password_reset import PasswordResetToken
from app.models.user import User
from app.services.email_service import EmailDeliveryError
from app.services.email_service import email_service

logger = logging.getLogger(__name__)

TOKEN_EXPIRY_HOURS = 1
MAX_TOKENS_PER_HOUR = 3


def _audit_reset_request(
    outcome: AuditOutcome,
    error_code: str | None,
    ip_address: str,
    user: User | None = None,
) -> None:
    """Record one password-reset request attempt.

    This module emitted **no** audit events at all, which is a conspicuous hole: a
    password reset is the first thing a reviewer looks for when investigating an
    account takeover, and it was the one credential change that left no trace in the
    audit log.

    Anti-enumeration: called on **every** exit path of ``request_password_reset``,
    including the unknown-address and non-local-account paths, with the same call
    shape and the same amount of work. The distinguishing information lives in the
    record's ``outcome``/``error_code`` — server-side, in an admin-only log — and
    never in anything the requester can observe. Deliberately does **not** carry the
    submitted address for an unknown account: the record is about the attempt, and
    writing an arbitrary attacker-supplied string into the audit index as a
    ``username`` is a log-injection surface with no investigative value.

    Args:
        outcome: SUCCESS when a token was issued and mailed, FAILURE otherwise.
        error_code: Short machine-readable reason, or None on success.
        ip_address: Requesting client's IP.
        user: The resolved account, when one resolved.
    """
    audit_logger.log(
        event_type=AuditEventType.AUTH_PASSWORD_RESET_REQUEST,
        outcome=outcome,
        user_id=int(user.id) if user is not None else None,
        username=str(user.email) if user is not None else None,
        source_ip=ip_address,
        error_code=error_code,
    )


def request_password_reset(db: Session, email: str, ip_address: str) -> None:
    """Request a password reset token.

    Anti-enumeration: always returns without error regardless of whether
    the email exists. Only local auth users can reset passwords.

    Args:
        db: Database session.
        email: Email address to send the reset link to.
        ip_address: Client IP address for audit logging.
    """
    user = db.query(User).filter(User.email == email).first()

    if not user or user.auth_type != AUTH_TYPE_LOCAL:
        logger.debug("Password reset requested for non-existent or non-local user")
        _audit_reset_request(
            AuditOutcome.FAILURE,
            "NO_LOCAL_ACCOUNT",
            ip_address,
            user if user is not None and user.auth_type != AUTH_TYPE_LOCAL else None,
        )
        return

    if not user.is_active:
        logger.debug("Password reset requested for inactive user")
        _audit_reset_request(AuditOutcome.FAILURE, "INACTIVE_ACCOUNT", ip_address, user)
        return

    # Rate limit: max tokens per hour per user
    cutoff = datetime.now(UTC) - timedelta(hours=1)
    recent_count = (
        db.query(PasswordResetToken)
        .filter(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.created_at > cutoff,
        )
        .count()
    )

    if recent_count >= MAX_TOKENS_PER_HOUR:
        logger.warning(f"Password reset rate limit reached for user {user.id}")
        _audit_reset_request(AuditOutcome.FAILURE, "RATE_LIMITED", ip_address, user)
        return

    # Generate token
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    reset_token = PasswordResetToken(
        user_id=user.id,
        token_hash=token_hash,
        expires_at=datetime.now(UTC) + timedelta(hours=TOKEN_EXPIRY_HOURS),
        ip_address=ip_address,
    )
    db.add(reset_token)
    db.commit()

    reset_url = f"{settings.FRONTEND_URL}/reset-password?token={raw_token}"
    # Every early return above is silent, so a delivery failure must be silent
    # too: this call is only reached for an address that exists, and letting the
    # exception escape would turn a mail outage into an account-existence oracle
    # (existing address -> 500, unknown address -> 200).
    try:
        email_service.send_password_reset(str(user.email), reset_url)
    except EmailDeliveryError:
        logger.error("Password reset for user %s was generated but could not be delivered", user.id)
        _audit_reset_request(AuditOutcome.PARTIAL, "DELIVERY_FAILED", ip_address, user)
        return
    _audit_reset_request(AuditOutcome.SUCCESS, None, ip_address, user)
    logger.info(f"Password reset token generated for user {user.id}")


def _audit_reset_complete(
    outcome: AuditOutcome,
    error_code: str | None,
    ip_address: str | None,
    user: User | None = None,
) -> None:
    """Record one password-reset redemption attempt.

    The reset token is never included, in any form: it is a bearer credential until
    it is redeemed, and an audit index is not the place to put one.

    Args:
        outcome: SUCCESS when the password actually changed, FAILURE otherwise.
        error_code: Short machine-readable reason, or None on success.
        ip_address: Requesting client's IP, when the caller supplied one.
        user: The account the token belonged to, when the token resolved.
    """
    audit_logger.log(
        event_type=AuditEventType.AUTH_PASSWORD_RESET_COMPLETE,
        outcome=outcome,
        user_id=int(user.id) if user is not None else None,
        username=str(user.email) if user is not None else None,
        source_ip=ip_address,
        error_code=error_code,
    )


def confirm_password_reset(
    db: Session, raw_token: str, new_password: str, ip_address: str | None = None
) -> tuple[bool, list[str]]:
    """Validate a reset token and change the user's password.

    Args:
        db: Database session.
        raw_token: The raw token from the reset URL.
        new_password: The new password to set.
        ip_address: Client IP for the audit record. Optional so existing callers keep
            working; the event is still written without it.

    Returns:
        Tuple of (success, errors). On success errors is empty.
        On failure, errors contains human-readable messages.
    """
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    record = (
        db.query(PasswordResetToken)
        .filter(
            PasswordResetToken.token_hash == token_hash,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > datetime.now(UTC),
        )
        .first()
    )

    if not record:
        _audit_reset_complete(AuditOutcome.FAILURE, "INVALID_OR_EXPIRED_TOKEN", ip_address)
        return False, ["Invalid or expired reset token"]

    user = db.query(User).filter(User.id == record.user_id).first()
    if not user:
        _audit_reset_complete(AuditOutcome.FAILURE, "INVALID_OR_EXPIRED_TOKEN", ip_address)
        return False, ["Invalid or expired reset token"]

    # Validate new password against policy.
    #
    # NO `if settings.PASSWORD_POLICY_ENABLED:` guard here. That read the **.env**
    # value, while the policy itself is DB-backed (admin UI, DB > .env > default) and
    # ``validate_password`` already returns a passing result when the policy is off.
    # The redundant guard therefore did not gate the policy, it *bypassed* it: a
    # deployment that enabled the policy in the admin UI while .env still said false
    # got no password validation at all — on the reset path only, while every other
    # path enforced it. The one place a weak password is most likely to be chosen.
    result = validate_password(new_password, email=str(user.email))
    if not result.is_valid:
        _audit_reset_complete(AuditOutcome.FAILURE, "POLICY_REJECTED", ip_address, user)
        return False, result.errors

    # Check password history (FedRAMP IA-5)
    if not check_password_against_history(db, int(user.id), new_password):
        _audit_reset_complete(AuditOutcome.FAILURE, "PASSWORD_REUSED", ip_address, user)
        return False, ["Password was used recently. Please choose a different password."]

    # Update password
    password_hash = get_password_hash(new_password)
    user.hashed_password = password_hash
    user.password_changed_at = datetime.now(UTC)
    user.must_change_password = False

    # Record in password history
    add_password_to_history(db, int(user.id), password_hash)

    # Mark token as used and invalidate other tokens for this user
    record.used_at = datetime.now(UTC)
    db.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == user.id,
        PasswordResetToken.id != record.id,
        PasswordResetToken.used_at.is_(None),
    ).update({"used_at": datetime.now(UTC)})

    # Invalidate all existing sessions (FedRAMP AC-12).
    #
    # This must be part of the same transaction as the password change, and it
    # must be allowed to fail the whole reset. Previously this called
    # revoke_all_user_tokens(), which commits on success and calls db.rollback()
    # on ANY error — so a Redis outage or a failed commit silently reverted the
    # new password hash, the history row and the used-token markers, and we still
    # returned success (issue #324). The user was told their password had changed
    # when it had not, and their existing sessions were left live — the precise
    # outcome AC-12 exists to prevent, at the moment it matters most, since a
    # reset is often triggered by a suspected compromise.
    try:
        token_service.revoke_all_user_tokens_in_transaction(db, int(user.id))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception(
            "Password reset aborted for user %s: could not revoke existing sessions", user.id
        )
        _audit_reset_complete(AuditOutcome.FAILURE, "SESSION_REVOCATION_FAILED", ip_address, user)
        return False, ["Could not complete the password reset. Please try again."]

    _audit_reset_complete(AuditOutcome.SUCCESS, None, ip_address, user)
    logger.info(f"Password reset completed for user {user.id}")
    return True, []
