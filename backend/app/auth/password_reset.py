"""Password reset logic with anti-enumeration and rate limiting."""

import hashlib
import logging
import secrets
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from sqlalchemy.orm import Session

from app.auth.constants import AUTH_TYPE_LOCAL
from app.auth.password_history import add_password_to_history
from app.auth.password_history import check_password_against_history
from app.auth.password_policy import validate_password
from app.auth.token_service import token_service
from app.core.config import settings
from app.core.security import get_password_hash
from app.models.password_reset import PasswordResetToken
from app.models.user import User
from app.services.email_service import email_service

logger = logging.getLogger(__name__)

TOKEN_EXPIRY_HOURS = 1
MAX_TOKENS_PER_HOUR = 3


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
        return

    if not user.is_active:
        logger.debug("Password reset requested for inactive user")
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
    email_service.send_password_reset(str(user.email), reset_url)
    logger.info(f"Password reset token generated for user {user.id}")


def confirm_password_reset(
    db: Session, raw_token: str, new_password: str
) -> tuple[bool, list[str]]:
    """Validate a reset token and change the user's password.

    Args:
        db: Database session.
        raw_token: The raw token from the reset URL.
        new_password: The new password to set.

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
        return False, ["Invalid or expired reset token"]

    user = db.query(User).filter(User.id == record.user_id).first()
    if not user:
        return False, ["Invalid or expired reset token"]

    # Validate new password against policy
    if settings.PASSWORD_POLICY_ENABLED:
        result = validate_password(new_password, email=str(user.email))
        if not result.is_valid:
            return False, result.errors

    # Check password history (FedRAMP IA-5)
    if not check_password_against_history(db, int(user.id), new_password):
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

    # Invalidate all existing sessions (FedRAMP AC-12)
    token_service.revoke_all_user_tokens(db, int(user.id))

    db.commit()
    logger.info(f"Password reset completed for user {user.id}")
    return True, []
