"""Email verification — the reader ``require_email_verification`` never had.

``require_email_verification`` has been a declared auth-config key
(``schemas/auth_config.py``, ``services/auth_config_service.py``) with **no
consumer anywhere**: the admin UI rendered the switch, the value was stored, and
nothing ever read it. This module is the feature.

Scope: it gates **local password login only**. An account whose identity lives
in LDAP/OIDC/PKI has its address asserted by the provider
(``ExternalIdentity.email_verified``, consumed by ``app/auth/external_sync.py``)
— a separate flag with separate semantics. Blocking those logins here would
second-guess the IdP.
"""

import hashlib
import logging
import secrets
import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from app.auth.constants import AUTH_TYPE_LOCAL
from app.auth.roles import ROLE_SUPER_ADMIN
from app.core.auth_settings import get_auth_settings
from app.core.config import settings
from app.models.invitation import EmailVerificationToken
from app.models.user import User
from app.services.email_service import email_service

logger = logging.getLogger(__name__)

TOKEN_EXPIRY_HOURS = 24
MAX_TOKENS_PER_HOUR = 3

#: One message for every outcome of a verification attempt, valid or not.
GENERIC_INVALID = "This verification link is invalid or has expired."


def _hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def email_verification_required(db: Session) -> bool:
    """Whether this deployment requires a verified address for local login.

    DB ``auth_config`` wins over ``.env`` wins over the coded default, like every
    other auth setting.
    """
    return bool(get_auth_settings(db).get_bool("require_email_verification", False))


def issue_verification_token(db: Session, user: User, ip_address: str = "") -> None:
    """Create a verification token for *user* and email the link.

    Silently does nothing for an already-verified account or a non-local one,
    and rate-limits per user so the endpoint cannot be turned into a mailer.
    """
    if user.email_verified or user.auth_type != AUTH_TYPE_LOCAL:
        return

    cutoff = datetime.now(UTC) - timedelta(hours=1)
    recent = (
        db.query(EmailVerificationToken)
        .filter(
            EmailVerificationToken.user_id == user.id,
            EmailVerificationToken.created_at > cutoff,
        )
        .count()
    )
    if recent >= MAX_TOKENS_PER_HOUR:
        logger.warning("Email-verification rate limit reached for user %s", user.id)
        return

    raw_token = secrets.token_urlsafe(32)
    db.add(
        EmailVerificationToken(
            user_id=user.id,
            token_hash=_hash(raw_token),
            expires_at=datetime.now(UTC) + timedelta(hours=TOKEN_EXPIRY_HOURS),
            ip_address=ip_address or None,
        )
    )
    db.commit()

    verify_url = f"{settings.FRONTEND_URL}/verify-email?token={raw_token}"
    email_service.send_email_verification(str(user.email), verify_url, TOKEN_EXPIRY_HOURS)
    logger.info("Email-verification token issued for user %s", user.id)


def verify_email(db: Session, raw_token: str) -> tuple[bool, str | None]:
    """Redeem a verification token.

    Returns:
        ``(True, None)`` on success, ``(False, GENERIC_INVALID)`` otherwise —
        one message for unknown, used and expired alike.
    """
    if not raw_token:
        return False, GENERIC_INVALID

    record = (
        db.query(EmailVerificationToken)
        .filter(EmailVerificationToken.token_hash == _hash(raw_token))
        .first()
    )
    now = datetime.now(UTC)
    if record is None or record.used_at is not None:
        return False, GENERIC_INVALID

    expires_at = record.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= now:
        return False, GENERIC_INVALID

    user = db.query(User).filter(User.id == record.user_id).first()
    if user is None:
        return False, GENERIC_INVALID

    user.email_verified = True
    user.email_verified_at = now
    record.used_at = now
    db.commit()
    logger.info("Email verified for user %s", user.id)
    return True, None


def resend_verification(db: Session, email: str, ip_address: str = "") -> None:
    """Send a fresh verification link, without ever revealing whether it went.

    The endpoint's response is identical for a registered address, an unknown
    one, and an already-verified one — otherwise it is an account-existence
    oracle that needs no session.
    """
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        logger.debug("Verification resend requested for an unknown address")
        return
    issue_verification_token(db, user, ip_address)


def assert_email_verified_for_local_login(db: Session, user_uuid: str) -> None:
    """Refuse a local-password login when the address is unverified.

    Called from the local authentication path only, so LDAP/OIDC/PKI logins are
    unaffected. An active ``super_admin`` is exempt for the same reason it is
    exempt from ``local_enabled``: auth configuration is super_admin-gated, so
    the account that can turn this setting off must never be locked out by it.

    Args:
        db: Database session.
        user_uuid: UUID of the account that just passed credential verification.

    Raises:
        HTTPException: 403 when verification is required and missing.
    """
    if not email_verification_required(db):
        return

    try:
        parsed = uuid_pkg.UUID(user_uuid)
    except (ValueError, AttributeError, TypeError):
        return

    user = db.query(User).filter(User.uuid == parsed).first()
    if user is None or user.email_verified:
        return
    # An externally-owned identity reaching this path is a pki/keycloak local
    # fallback. Its address was asserted by the IdP (ExternalIdentity.
    # email_verified); re-verifying it here would second-guess the provider.
    if user.auth_type != AUTH_TYPE_LOCAL:
        return
    if user.is_active and user.role == ROLE_SUPER_ADMIN:
        return

    # Issued here so the user has a way forward without an admin: the login
    # attempt itself re-sends the link (rate-limited above).
    issue_verification_token(db, user)
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Your email address has not been verified. Check your inbox for the link.",
    )
