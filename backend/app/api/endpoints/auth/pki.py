"""PKI / mTLS client-certificate authentication endpoint."""

import logging
from datetime import timedelta

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.endpoints.auth.dependencies import _get_client_info
from app.auth.audit import audit_logger
from app.auth.direct_auth import create_access_token as direct_create_token
from app.auth.pki_auth import pki_authenticate
from app.auth.pki_auth import sync_pki_user_to_db
from app.auth.token_service import token_service
from app.core.auth_settings import get_auth_settings
from app.core.config import settings
from app.db.base import get_db
from app.schemas.user import Token

router = APIRouter()


logger = logging.getLogger(__name__)


@router.post("/pki/authenticate", response_model=Token)
async def pki_login(request: Request, db: Session = Depends(get_db)):
    """
    Authenticate via X.509 client certificate.

    The reverse proxy (Nginx) must be configured to pass the client
    certificate information via headers (X-Client-Cert or X-Client-Cert-DN).
    """
    # Check database settings first, then fall back to .env
    auth_settings = get_auth_settings(db)
    pki_enabled = auth_settings.pki_enabled or settings.PKI_ENABLED

    if not pki_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="PKI authentication is not enabled",
        )

    client_ip, user_agent = _get_client_info(request)

    pki_data = pki_authenticate(request, admin_dns_config=auth_settings.pki_admin_dns)
    if not pki_data:
        logger.warning("PKI authentication failed - invalid or missing certificate")
        # Log PKI login failure
        audit_logger.log_login_failure(
            username="unknown",
            source_ip=client_ip,
            user_agent=user_agent,
            error_code="INVALID_CERTIFICATE",
            auth_method="pki",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing client certificate",
            headers={"WWW-Authenticate": "Certificate"},
        )

    # Sync user to database
    user = sync_pki_user_to_db(db, pki_data)

    if not user.is_active:
        logger.warning(f"PKI user account is inactive: {pki_data['subject_dn']}")
        # Log PKI login failure for inactive user
        audit_logger.log_login_failure(
            username=pki_data.get("subject_dn", "unknown"),
            source_ip=client_ip,
            user_agent=user_agent,
            error_code="INACTIVE_USER",
            auth_method="pki",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inactive user account",
        )

    # Generate JWT token
    access_token_expires = timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    token_data = {"sub": str(user.uuid), "role": user.role}
    access_token = direct_create_token(data=token_data, expires_delta=access_token_expires)

    # Log PKI login success
    audit_logger.log_login_success(
        user_id=user.id,
        username=user.email,
        source_ip=client_ip,
        user_agent=user_agent,
        auth_method="pki",
    )

    logger.info(f"PKI authentication successful for user: {pki_data['subject_dn']}")

    # Generate refresh token for PKI users too
    refresh_token, _ = token_service.create_refresh_token(
        db=db,
        user_id=user.id,
        user_uuid=str(user.uuid),
        role=str(user.role),
        user_agent=user_agent,
        ip_address=client_ip,
    )

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

    set_auth_cookies(response, access_token, refresh_token)
    return response
