"""Post-authentication steps shared by every login method.

Stamping ``last_login_at``, the password-expiry flag, the MFA verification /
enrolment gate and session issuance (access + refresh token, auth cookies).
Called by the password login in ``login.py`` and by the PKI, OIDC, SAML, proxy and
MFA-completion paths.
"""

import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.endpoints.auth.mfa_tokens import MFA_SCOPE_ENROLL
from app.api.endpoints.auth.mfa_tokens import _create_mfa_token
from app.api.endpoints.auth.mfa_tokens import _is_mfa_enabled
from app.api.endpoints.auth.mfa_tokens import _is_mfa_required
from app.api.endpoints.auth.mfa_tokens import _user_can_setup_mfa
from app.auth.audit import audit_logger
from app.auth.audit_events import AuditEventType
from app.auth.audit_events import AuditOutcome
from app.auth.direct_auth import create_access_token as direct_create_token
from app.auth.password_policy import get_days_until_expiration
from app.auth.password_policy import is_password_expired
from app.auth.token_service import token_service
from app.auth.utils import local_password_allowed
from app.core.auth_settings import DynamicAuthSettings
from app.core.auth_types import AUTH_TYPE_LOCAL
from app.core.auth_types import AUTH_TYPE_OIDC
from app.core.auth_types import AUTH_TYPE_PKI
from app.core.config import settings
from app.models.user import User
from app.models.user_mfa import UserMFA

logger = logging.getLogger(__name__)


def record_successful_login(db: Session, user: User) -> None:
    """Stamp ``last_login_at`` for a caller that has just been given a session.

    **Every successful authentication path must call this** — local/LDAP login,
    PKI, OIDC, and MFA completion. Nothing wrote the column, so the admin UI
    reported ``null`` for every user forever and no inactive-account control
    (FedRAMP AC-2(3), the usual 30/60/90-day disable) had any data to act on.

    Stamped where a full session is issued rather than where the first factor
    passes: an MFA challenge that is never answered is not a login.

    Args:
        db: Database session
        user: The authenticated user row
    """
    try:
        user.last_login_at = datetime.now(UTC)  # type: ignore[assignment]
        db.commit()
    except Exception:
        # Bookkeeping must never cost the caller the session they just earned.
        logger.exception("Could not stamp last_login_at for user_id=%s", getattr(user, "id", None))
        db.rollback()


def _apply_password_expiry(
    db: Session,
    user: User,
    auth_method: str,
    client_ip: str,
    user_agent: str,
    auth_settings: DynamicAuthSettings,
) -> None:
    """Flag an expired local password so the dependency gate forces a change.

    ``password_policy.is_password_expired`` existed with zero call sites, so
    ``PASSWORD_MAX_AGE_DAYS`` was reported by the admin account-status page and
    enforced nowhere. Rather than invent a second mechanism, an expired password
    sets ``must_change_password`` and flows into the same
    ``get_current_active_user`` gate the admin force-change flag uses.

    Only accounts that actually hold a local password are considered:
    ``password_changed_at`` is meaningless for an LDAP/OIDC/PKI identity, and
    treating it as expired would lock those users out of an app whose password
    they cannot change here.

    Args:
        db: Database session
        user: The authenticated user row
        auth_method: The method actually used for this login ("local", "ldap", …)
        client_ip: Client IP address, for the audit record
        user_agent: Client user agent, for the audit record
        auth_settings: The request's resolved auth config (DB > .env > default). The
            max-age reported to the caller must be the one that was applied; reading
            ``settings.PASSWORD_MAX_AGE_DAYS`` from .env told the user their password
            expired after N days while the enforced policy used a different N.
    """
    if auth_method != AUTH_TYPE_LOCAL:
        # The credential just verified was not a local password.
        return

    allowed, reason = local_password_allowed(
        str(user.auth_type) if user.auth_type else None,
        bool(getattr(user, "allow_local_fallback", False)),
    )
    if not allowed:
        logger.debug("Skipping password expiry for %s: %s", user.email, reason)
        return

    password_changed_at = getattr(user, "password_changed_at", None)
    if password_changed_at is None:
        # ``is_password_expired`` treats "no recorded change time" as expired, which is
        # the right default for a policy primitive but the wrong one HERE: nothing on
        # the account-creation paths (initial_data, users.py, registration.py) ever
        # stamped the column, so almost every pre-existing local user carries NULL.
        # Forcing all of them through a password change on their next login would be a
        # self-inflicted outage, not a control. Say so loudly instead — same reasoning
        # as the password-history degradation warning in password_policy.
        logger.warning(
            "Password age unknown for user_id=%s (password_changed_at is NULL); expiry "
            "not enforced for this login. Backfill the column to enable IA-5(1).",
            user.id,
        )
        return

    if not is_password_expired(password_changed_at):
        return

    if not user.must_change_password:
        user.must_change_password = True  # type: ignore[assignment]
        db.commit()

    audit_logger.log(
        event_type=AuditEventType.AUTH_PASSWORD_EXPIRED,
        outcome=AuditOutcome.SUCCESS,
        user_id=user.id,
        username=str(user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        error_code="PASSWORD_EXPIRED",
        details={
            "max_age_days": auth_settings.password_max_age_days,
            "days_until_expiration": get_days_until_expiration(password_changed_at),
        },
    )
    logger.info("Password expired for user %s; a change is now required", user.email)


def _check_mfa_requirement(
    db: Session,
    user: User,
    user_uuid_str: str,
    user_role: str,
    actual_auth_method: str = "",
) -> JSONResponse | None:
    """Check if MFA is required for user and return MFA response if needed.

    Args:
        db: Database session
        user: User model object
        user_uuid_str: User UUID string
        user_role: User's role
        actual_auth_method: How the user actually authenticated. When a PKI/OIDC
            user authenticates via password fallback, actual_auth_method will be "local"
            and MFA should still apply.

    Returns:
        JSONResponse with an MFA half-token if MFA verification or enrolment is
        required, None if the caller may be issued a full session.
    """
    # Skip MFA check if MFA is disabled (check DB first, then .env)
    if not _is_mfa_enabled(db):
        return None

    # Skip MFA for PKI and OIDC users ONLY if they authenticated via their native method.
    # If they used local password fallback, MFA must still apply.
    if user.auth_type in [AUTH_TYPE_PKI, AUTH_TYPE_OIDC] and actual_auth_method != AUTH_TYPE_LOCAL:
        return None

    user_mfa = db.query(UserMFA).filter(UserMFA.user_id == user.id).first()

    if user_mfa and user_mfa.totp_enabled:
        # User has MFA enabled - return MFA token instead of access token
        mfa_token = _create_mfa_token(user_uuid_str, user_role)
        logger.info(f"MFA verification required for user: {str(user.email)}")
        return JSONResponse(
            content={
                "mfa_required": True,
                "mfa_token": mfa_token,
                "message": "MFA verification required",
            }
        )

    if _is_mfa_required(db, user):
        # Deployment requires MFA and this user has not enrolled. Enrolment used to be
        # enforced only by the SPA, so any API/CLI client that ignored the hint received
        # a full, unrestricted session on an MFA_REQUIRED deployment.
        if not _user_can_setup_mfa(user) and actual_auth_method != AUTH_TYPE_LOCAL:
            # An external IdP owns this user's second factor and there is no local TOTP
            # to enrol in — same reasoning as the PKI/OIDC bypass above.
            return None

        # Same short-lived, single-use half-token as the verification branch, but scoped
        # to enrolment: it carries the "mfa" type claim (so every access-token consumer
        # rejects it) and only authorizes /mfa/setup + /mfa/verify-setup.
        mfa_token = _create_mfa_token(user_uuid_str, user_role, scope=MFA_SCOPE_ENROLL)
        logger.info(f"MFA enrollment required before access for user: {str(user.email)}")
        return JSONResponse(
            content={
                "mfa_required": True,
                "mfa_enrollment_required": True,
                "mfa_token": mfa_token,
                "message": "MFA enrollment is required before access is granted",
            }
        )

    return None


def _generate_login_tokens(
    request: Request,
    db: Session,
    user: User,
    user_uuid_str: str,
    user_role: str,
    user_agent: str,
    client_ip: str,
    auth_method: str = "",
) -> JSONResponse:
    """Generate access and refresh tokens for successful login.

    Args:
        request: FastAPI request object (its scheme decides the cookie Secure flag
            under ALLOW_INSECURE_COOKIES — see `auth/cookies.py:_secure_for_request`)
        db: Database session
        user: User model object
        user_uuid_str: User UUID string
        user_role: User's role
        user_agent: Client user agent
        client_ip: Client IP address
        auth_method: How the user actually authenticated ("local", "ldap", …)

    Returns:
        JSONResponse with access_token, refresh_token, and token metadata
    """
    # Generate the JWT access token with role information
    access_token_expires = timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    token_data = {"sub": user_uuid_str, "type": "access"}
    if user_role:
        token_data["role"] = user_role

    access_token = direct_create_token(data=token_data, expires_delta=access_token_expires)

    # Generate refresh token (FedRAMP AC-12)
    refresh_token, _ = token_service.create_refresh_token(
        db=db,
        user_id=user.id,
        user_uuid=user_uuid_str,
        role=user_role,
        user_agent=user_agent,
        ip_address=client_ip,
    )

    # Stamp last_login_at now that a full session exists (FedRAMP AC-2(3)).
    record_successful_login(db, user)

    # Log successful login
    audit_logger.log_login_success(
        user_id=user.id,
        username=str(user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        # The method actually used, not one inferred from LDAP_ENABLED + auth_type: a
        # PKI/OIDC user falling back to a local password was audited as "ldap".
        auth_method=auth_method or str(user.auth_type),
    )

    logger.info(f"Login successful for user: {str(user.email)}")
    response = JSONResponse(
        content={
            "access_token": access_token,
            "token_type": "bearer",
            "refresh_token": refresh_token,
            "expires_in": settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        }
    )

    # Set httpOnly cookies for browser-based authentication (C2 security hardening)
    from app.auth.cookies import set_auth_cookies

    set_auth_cookies(response, access_token, refresh_token, request)
    return response
