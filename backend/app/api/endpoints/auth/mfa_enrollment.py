"""MFA enrolment authorization and post-second-factor session issuance.

Layered strictly **above** :mod:`mfa_tokens`, which owns the half-token primitives
(minting, scope/replay verification, the Redis claim). Imports only ever run in that
direction — putting either half in the other module would make the two cycle.

What lives here:

- :func:`get_user_for_enrollment` — the narrow dependency for ``/mfa/setup`` and
  ``/mfa/verify-setup``, the only two routes an enrolment half-token may reach.
- :func:`issue_session_response` — the single place a session is minted once a second
  factor has been proven, shared by ``/mfa/verify`` and forced-enrolment completion.
"""

import logging
from dataclasses import dataclass
from datetime import UTC
from datetime import timedelta
from uuid import UUID

from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from fastapi.responses import JSONResponse
from jose import JWTError
from jose import jwt
from sqlalchemy.orm import Session

from app.api.endpoints.auth.dependencies import get_current_user
from app.api.endpoints.auth.dependencies import oauth2_scheme
from app.api.endpoints.auth.mfa_tokens import MFA_SCOPE_ENROLL
from app.api.endpoints.auth.mfa_tokens import _claim_mfa_token
from app.api.endpoints.auth.mfa_tokens import _user_can_setup_mfa
from app.api.endpoints.auth.mfa_tokens import _verify_mfa_token
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.constants import TOKEN_TYPE_ACCESS
from app.auth.constants import TOKEN_TYPE_MFA
from app.auth.direct_auth import create_access_token as direct_create_token
from app.auth.token_service import token_service
from app.core.config import settings
from app.db.base import get_db
from app.models.user import User
from app.models.user_mfa import UserMFA

logger = logging.getLogger(__name__)


def issue_session_response(
    db: Session,
    user: User,
    user_uuid_str: str,
    user_role: str,
    client_ip: str,
    user_agent: str,
    extra_content: dict | None = None,
) -> JSONResponse:
    """Mint the access/refresh pair for a caller that has passed the second factor.

    The single place a session is issued *after* MFA, so ``/mfa/verify`` and the forced
    enrolment completion at ``/mfa/verify-setup`` cannot drift apart.

    Args:
        db: Database session
        user: User model object
        user_uuid_str: User UUID string
        user_role: Role to embed in the tokens
        client_ip: Client IP address
        user_agent: Client user agent
        extra_content: Additional keys to merge into the JSON body (e.g. backup codes)

    Returns:
        JSONResponse with access_token, refresh_token, token metadata, and auth cookies
    """
    access_token_expires = timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    token_data = {"sub": user_uuid_str, "role": user_role, "type": TOKEN_TYPE_ACCESS}
    access_token = direct_create_token(data=token_data, expires_delta=access_token_expires)

    refresh_token, _ = token_service.create_refresh_token(
        db=db,
        user_id=user.id,
        user_uuid=user_uuid_str,
        role=user_role,
        user_agent=user_agent,
        ip_address=client_ip,
    )

    content = dict(extra_content or {})
    content.update(
        {
            "access_token": access_token,
            "token_type": "bearer",
            "refresh_token": refresh_token,
            "expires_in": settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        }
    )
    response = JSONResponse(content=content)

    # Set httpOnly cookies for browser-based authentication (C2 security hardening)
    from app.auth.cookies import set_auth_cookies

    set_auth_cookies(response, access_token, refresh_token)

    # Every successful auth path stamps last_login_at. This function is the single
    # post-MFA session-issue point, so it covers /mfa/verify AND the forced
    # enrolment completion at /mfa/verify-setup. Imported here rather than at module
    # scope: login.py imports this module's siblings, and a top-level import would
    # close the cycle.
    from app.api.endpoints.auth.login import record_successful_login

    record_successful_login(db, user)
    return response


def _complete_mfa_verification(
    db: Session,
    user: User,
    user_mfa: UserMFA,
    user_uuid_str: str,
    user_role: str,
    mfa_jti: str,
    used_backup_code: bool,
    client_ip: str,
    user_agent: str,
) -> JSONResponse:
    """Finalize MFA verification and generate tokens.

    Args:
        db: Database session
        user: User model object
        user_mfa: User's MFA record
        user_uuid_str: User UUID string
        user_role: User's role
        mfa_jti: MFA token JTI (for blacklisting)
        used_backup_code: Whether a backup code was used
        client_ip: Client IP address
        user_agent: Client user agent

    Returns:
        JSONResponse with access_token, refresh_token, and token metadata
    """
    from datetime import datetime as dt

    # Claim the half-token BEFORE anything is minted. This is the authoritative
    # single-use gate: the read in _verify_mfa_token is only an early rejection, so two
    # concurrent verifications of the same jti reach here together and exactly one may
    # continue. Losing the race is indistinguishable from a replay, hence the same 401.
    if mfa_jti:
        mfa_token_ttl_seconds = settings.MFA_TOKEN_EXPIRE_MINUTES * 60
        if not _claim_mfa_token(mfa_jti, mfa_token_ttl_seconds):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="MFA token has already been used",
            )
        logger.debug(f"MFA token claimed for single use (jti={mfa_jti[:8]}...)")

    # Update last verified timestamp
    user_mfa.last_verified_at = dt.now(UTC)  # type: ignore[assignment]
    db.commit()

    # Log MFA verification success (and backup code usage if applicable)
    if used_backup_code:
        audit_logger.log_mfa_event(
            event_type=AuditEventType.AUTH_MFA_BACKUP_USED,
            outcome=AuditOutcome.SUCCESS,
            user_id=user.id,
            username=str(user.email),
            source_ip=client_ip,
            user_agent=user_agent,
        )
    audit_logger.log_mfa_event(
        event_type=AuditEventType.AUTH_MFA_VERIFY,
        outcome=AuditOutcome.SUCCESS,
        user_id=user.id,
        username=str(user.email),
        source_ip=client_ip,
        user_agent=user_agent,
    )

    logger.info(f"MFA verification successful for user: {str(user.email)}")
    return issue_session_response(db, user, user_uuid_str, user_role, client_ip, user_agent)


@dataclass
class EnrollmentContext:
    """Who is allowed to run the MFA enrolment endpoints, and under what authority.

    Attributes:
        user: The account being enrolled.
        user_role: Role to embed in the session issued once enrolment completes.
        mfa_jti: JTI of the enrolment half-token that authorized this call, or None when
            an ordinary session did. Its presence is what tells ``/mfa/verify-setup``
            to burn the token and hand back a real session.
    """

    user: User
    user_role: str
    mfa_jti: str | None = None


def _enrollment_context_from_half_token(db: Session, token: str) -> EnrollmentContext:
    """Resolve an enrolment-scoped half-token into an :class:`EnrollmentContext`."""
    user_uuid_str, token_role, jti = _verify_mfa_token(token, expected_scope=MFA_SCOPE_ENROLL)

    user = db.query(User).filter(User.uuid == UUID(user_uuid_str)).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    if not user.is_active:
        logger.warning(f"MFA enrollment refused for inactive user: {str(user.email)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Inactive user account",
        )
    if not _user_can_setup_mfa(user):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA setup is not available for your authentication type",
        )

    # DB role wins over the token's copy — same rule get_current_user applies.
    return EnrollmentContext(user=user, user_role=str(user.role) or token_role, mfa_jti=jti)


def get_user_for_enrollment(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> EnrollmentContext:
    """Authorize the two MFA enrolment endpoints, and only those.

    Accepts EITHER a normal session (access token / httpOnly cookie, for a user who is
    already logged in and opting into MFA) OR an **enrolment-scoped** half-token, which
    is how a user whose deployment forces MFA gets from the login response to an
    enrolled account without ever holding a session.

    ``get_current_user`` is deliberately NOT relaxed to accept ``type: "mfa"``: that
    check is the whole of the MFA bypass fix, and a half-token must stay useless on the
    other ~31 endpoints that depend on it.

    Raises:
        HTTPException: 401 for an invalid/used/wrongly-scoped token or an inactive user,
            400 when the account's auth type has no local TOTP to enrol in.
    """
    if token:
        # Peek at the purpose claim (signature verified, expiry deferred) so an expired
        # half-token reports "expired MFA token" instead of a generic credentials error.
        try:
            peeked = jwt.decode(
                token,
                settings.JWT_SECRET_KEY,
                algorithms=[settings.JWT_ALGORITHM],
                options={"verify_exp": False},
            )
        except JWTError:
            peeked = {}

        if peeked.get("type") == TOKEN_TYPE_MFA:
            return _enrollment_context_from_half_token(db, token)

    user = get_current_user(request=request, token=token, db=db)
    return EnrollmentContext(user=user, user_role=str(user.role))
