"""User registration, password policy, and password reset endpoints."""

import logging
from datetime import UTC

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy.orm import Session

from app.api.endpoints.auth.dependencies import _get_client_info
from app.auth.audit import AuditEventType
from app.auth.audit import audit_logger
from app.auth.constants import AUTH_TYPE_LOCAL
from app.auth.password_history import add_password_to_history
from app.auth.rate_limit import get_auth_rate_limit
from app.auth.rate_limit import limiter
from app.core.auth_settings import get_auth_settings
from app.core.security import get_password_hash
from app.db.base import get_db
from app.models.user import User
from app.schemas.user import User as UserSchema
from app.schemas.user import UserCreate

router = APIRouter()


logger = logging.getLogger(__name__)


# Pydantic request bodies for password reset endpoints
class PasswordResetRequestBody(PydanticBaseModel):
    email: str = ""


class PasswordResetConfirmBody(PydanticBaseModel):
    token: str = ""
    new_password: str = ""


@router.post("/register", response_model=UserSchema)
@limiter.limit(get_auth_rate_limit())
def register(request: Request, user_in: UserCreate, db: Session = Depends(get_db)):
    """
    Register a new user.

    Password must meet the configured password policy requirements (FedRAMP IA-5):
    - Minimum length (default: 12 characters)
    - At least one uppercase letter
    - At least one lowercase letter
    - At least one digit
    - At least one special character
    - Cannot contain email username or name parts

    Note: When LDAP is enabled, local registration is still allowed for admin accounts.
    Regular users should use LDAP authentication.

    Rate-limited like every other auth route — this was the ONLY one without a limiter,
    while creating accounts that are immediately active and GPU-capable (issue #284
    A0.11).

    Registration can be closed entirely, which a deployment fronted by an external
    IdP should always do. The switch is DB-backed (Settings -> Authentication ->
    Local) with ``ALLOW_OPEN_REGISTRATION`` as the env fallback, matching the
    precedence every other auth setting uses. It previously read the env var ONLY,
    so the admin-UI toggle that appeared to control this did nothing at all — a
    deployment running LDAP reported that users could still self-register (#354).
    """
    if not get_auth_settings(db).allow_registration:
        logger.warning(
            "Rejected registration attempt for %s: open registration is disabled",
            user_in.email,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Self-registration is disabled. Contact your administrator for an account.",
        )

    # Self-registration only ever creates a LOCAL account (auth_type is hardcoded
    # below). ``UserCreate.password`` became optional so an admin can provision an
    # external account without inventing one — refuse that shape here rather than
    # letting a passwordless body reach get_password_hash().
    if (user_in.auth_type or AUTH_TYPE_LOCAL) != AUTH_TYPE_LOCAL or not user_in.password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Self-registration creates local password accounts only.",
        )

    # Check if email already exists
    user_exists = db.query(User).filter(User.email == user_in.email).first()

    if user_exists:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    # Note: Password validation is performed by UserCreate schema's model_validator
    # which calls validate_password() from password_policy module

    # Hash the password
    from datetime import datetime

    password_hash = get_password_hash(user_in.password)

    # Create new user with local authentication
    db_user = User(
        email=user_in.email,
        full_name=user_in.full_name,
        hashed_password=password_hash,
        role="user",
        auth_type=AUTH_TYPE_LOCAL,
        is_active=True,
        is_superuser=False,
        password_changed_at=datetime.now(UTC),  # Track initial password time
    )

    db.add(db_user)
    db.commit()
    db.refresh(db_user)

    # Product metric: local self-registration (API process). External/JIT
    # signups are counted in app.auth.external_sync.
    from app.core.metrics import user_signups_total

    user_signups_total.labels(method=AUTH_TYPE_LOCAL).inc()

    # Store initial password in history (FedRAMP IA-5)
    add_password_to_history(db, db_user.id, password_hash)
    db.commit()

    # Log user registration
    client_ip, user_agent = _get_client_info(request)
    audit_logger.log_admin_action(
        event_type=AuditEventType.ADMIN_USER_CREATE,
        admin_user_id=db_user.id,  # Self-registration
        admin_username=str(db_user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        details={"registration_type": "self", "auth_type": AUTH_TYPE_LOCAL},
    )

    logger.info(
        f"New local user registered: {str(db_user.email)} (auth_type={db_user.auth_type}, role={db_user.role})"
    )
    return db_user


@router.get("/password-policy")
def get_password_policy():
    """
    Get the current password policy requirements.

    Returns the configured password policy settings so the frontend can
    display requirements to users during registration and password changes.

    This endpoint is public (no authentication required) to allow displaying
    requirements on registration forms.
    """
    from app.auth.password_policy import get_policy_requirements

    return get_policy_requirements()


@router.post("/password-reset/request")
@limiter.limit(get_auth_rate_limit())
def request_password_reset_endpoint(
    request: Request,
    body: "PasswordResetRequestBody",
    db: Session = Depends(get_db),
):
    """Request a password reset link.

    Always returns 200 regardless of whether the email exists to prevent
    email enumeration attacks. Rate limited per IP.
    """
    from app.auth.password_reset import request_password_reset

    client_ip = request.client.host if request.client else "unknown"
    request_password_reset(db, body.email, client_ip)

    return {"message": "If that email address is registered, you will receive a reset link."}


@router.post("/password-reset/confirm")
@limiter.limit(get_auth_rate_limit())
def confirm_password_reset_endpoint(
    request: Request,
    body: "PasswordResetConfirmBody",
    db: Session = Depends(get_db),
):
    """Confirm a password reset with token and new password.

    Validates the token and sets the new password. Returns 400 if the
    token is invalid or expired. Rate limited per IP.
    """
    from app.auth.password_reset import confirm_password_reset

    ok, errors = confirm_password_reset(db, body.token, body.new_password)
    if not ok:
        detail = errors[0] if errors else "Invalid or expired reset token"
        raise HTTPException(status_code=400, detail=detail)

    return {"message": "Password has been reset successfully."}
