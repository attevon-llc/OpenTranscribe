"""MFA setup / verification / disable endpoints."""

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.endpoints.auth.dependencies import _get_client_info
from app.api.endpoints.auth.dependencies import get_current_user
from app.api.endpoints.auth.mfa_tokens import _complete_mfa_verification
from app.api.endpoints.auth.mfa_tokens import _get_user_for_mfa
from app.api.endpoints.auth.mfa_tokens import _is_mfa_enabled
from app.api.endpoints.auth.mfa_tokens import _is_mfa_required
from app.api.endpoints.auth.mfa_tokens import _user_can_setup_mfa
from app.api.endpoints.auth.mfa_tokens import _verify_mfa_code
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.mfa import MFAService
from app.auth.rate_limit import get_auth_rate_limit
from app.auth.rate_limit import limiter
from app.db.base import get_db
from app.models.user import User
from app.models.user_mfa import UserMFA
from app.schemas.user import MFADisableRequest
from app.schemas.user import MFASetupResponse
from app.schemas.user import MFAStatusResponse
from app.schemas.user import MFAVerifyRequest
from app.schemas.user import MFAVerifyResponse
from app.schemas.user import MFAVerifySetupRequest
from app.schemas.user import MFAVerifySetupResponse

router = APIRouter()


logger = logging.getLogger(__name__)


@router.get("/mfa/status", response_model=MFAStatusResponse)
def get_mfa_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Get MFA status for the current user.

    Returns whether MFA is enabled/configured for the user and
    whether the system requires MFA.
    """
    # Check if user has MFA configured
    user_mfa = db.query(UserMFA).filter(UserMFA.user_id == current_user.id).first()

    mfa_enabled = _is_mfa_enabled(db)

    return MFAStatusResponse(
        mfa_enabled=bool(user_mfa.totp_enabled) if user_mfa else False,
        mfa_configured=bool(user_mfa.totp_enabled) if user_mfa else False,
        mfa_required=_is_mfa_required(db),
        can_setup_mfa=mfa_enabled and _user_can_setup_mfa(current_user),
    )


@router.post("/mfa/setup", response_model=MFASetupResponse)
def setup_mfa(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Initiate MFA setup for the current user.

    Returns the TOTP secret, provisioning URI, and QR code for authenticator app setup.
    The user must verify with a valid TOTP code to complete setup.

    Note: This endpoint is only available when MFA is enabled and the user
    is not using PKI or Keycloak authentication (which handle MFA separately).
    """
    if not _is_mfa_enabled(db):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA is not enabled on this system",
        )

    if not _user_can_setup_mfa(current_user):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA setup is not available for your authentication type",
        )

    # Check if user already has MFA enabled
    existing_mfa = db.query(UserMFA).filter(UserMFA.user_id == current_user.id).first()
    if existing_mfa and existing_mfa.totp_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA is already enabled. Disable it first to reconfigure.",
        )

    # Generate new TOTP secret
    totp_secret = MFAService.generate_totp_secret()

    # Generate provisioning URI for authenticator apps (uses plaintext secret)
    provisioning_uri = MFAService.get_provisioning_uri(
        secret=totp_secret,
        email=str(current_user.email),
    )

    # Generate QR code
    qr_code_base64 = MFAService.generate_qr_code_base64(provisioning_uri)

    # Encrypt the TOTP secret before storing (CRITICAL-1: Encrypt at rest)
    encrypted_secret = MFAService.encrypt_totp_secret(totp_secret)

    # Store or update the MFA record (not yet enabled)
    if existing_mfa:
        existing_mfa.totp_secret = encrypted_secret  # type: ignore[assignment]
        existing_mfa.totp_enabled = False  # type: ignore[assignment]
        existing_mfa.backup_codes = []  # type: ignore[assignment]
    else:
        new_mfa = UserMFA(
            user_id=current_user.id,
            totp_secret=encrypted_secret,
            totp_enabled=False,
            backup_codes=[],
        )
        db.add(new_mfa)

    db.commit()

    # Log MFA setup initiated
    client_ip, user_agent = _get_client_info(request)
    audit_logger.log_mfa_event(
        event_type=AuditEventType.AUTH_MFA_SETUP,
        outcome=AuditOutcome.PARTIAL,  # Setup initiated but not completed
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=client_ip,
        user_agent=user_agent,
    )

    logger.info(f"MFA setup initiated for user: {str(current_user.email)}")

    return MFASetupResponse(
        secret=totp_secret,
        provisioning_uri=provisioning_uri,
        qr_code_base64=qr_code_base64,
    )


@router.post("/mfa/verify-setup", response_model=MFAVerifySetupResponse)
def verify_mfa_setup(
    request: Request,
    request_body: MFAVerifySetupRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Verify MFA setup with the initial TOTP code.

    This completes the MFA setup process and generates backup codes.
    Backup codes are returned only once - the user must save them securely.
    """
    if not _is_mfa_enabled(db):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA is not enabled on this system",
        )

    # Get user's MFA record
    user_mfa = db.query(UserMFA).filter(UserMFA.user_id == current_user.id).first()

    if not user_mfa:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA setup not initiated. Please start setup first.",
        )

    if user_mfa.totp_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA is already enabled.",
        )

    # Decrypt the TOTP secret for verification (stored encrypted at rest)
    try:
        decrypted_secret = MFAService.decrypt_totp_secret(str(user_mfa.totp_secret))
    except ValueError as e:
        logger.error(f"Failed to decrypt TOTP secret for user: {str(current_user.email)}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to verify MFA setup. Please try again.",
        ) from e

    # Verify the TOTP code
    client_ip, user_agent = _get_client_info(request)
    if not MFAService.verify_totp(decrypted_secret, request_body.code, user_id=current_user.id):
        logger.warning(f"MFA setup verification failed for user: {str(current_user.email)}")
        # Log MFA setup verification failure
        audit_logger.log_mfa_event(
            event_type=AuditEventType.AUTH_MFA_SETUP,
            outcome=AuditOutcome.FAILURE,
            user_id=current_user.id,
            username=str(current_user.email),
            source_ip=client_ip,
            user_agent=user_agent,
            error_code="INVALID_TOTP_CODE",
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid verification code. Please try again.",
        )

    # Generate backup codes
    backup_codes = MFAService.generate_backup_codes()
    hashed_backup_codes = MFAService.hash_backup_codes(backup_codes)

    # Enable MFA and store hashed backup codes
    user_mfa.totp_enabled = True  # type: ignore[assignment]
    user_mfa.backup_codes = hashed_backup_codes  # type: ignore[assignment]
    db.commit()

    # Log MFA setup complete
    audit_logger.log_mfa_event(
        event_type=AuditEventType.AUTH_MFA_SETUP,
        outcome=AuditOutcome.SUCCESS,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=client_ip,
        user_agent=user_agent,
    )

    logger.info(f"MFA enabled successfully for user: {str(current_user.email)}")

    return MFAVerifySetupResponse(
        success=True,
        backup_codes=backup_codes,  # Return plaintext codes only once
        message="MFA has been enabled successfully. Save your backup codes securely.",
    )


@router.post("/mfa/verify", response_model=MFAVerifyResponse)
@limiter.limit(get_auth_rate_limit())
def verify_mfa(
    request: Request,
    request_body: MFAVerifyRequest,
    db: Session = Depends(get_db),
):
    """
    Verify MFA code during login.

    This endpoint is called after successful password authentication when
    the user has MFA enabled. It accepts either a TOTP code or a backup code.

    Rate limited to prevent brute force attacks on TOTP codes.
    """
    if not _is_mfa_enabled(db):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA is not enabled on this system",
        )

    # Get user and verify MFA token
    user, user_mfa, user_uuid_str, user_role, mfa_jti = _get_user_for_mfa(
        db, request_body.mfa_token
    )

    # Decrypt the TOTP secret for verification (stored encrypted at rest)
    try:
        decrypted_secret = MFAService.decrypt_totp_secret(str(user_mfa.totp_secret))
    except ValueError as e:
        logger.error(f"Failed to decrypt TOTP secret for user: {str(user.email)}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to verify MFA. Please contact support.",
        ) from e

    # Verify TOTP or backup code
    backup_codes_list: list[str] = list(user_mfa.backup_codes)
    is_valid, used_backup_code = _verify_mfa_code(
        request_body.code,
        decrypted_secret,
        backup_codes_list,
        db,
        user_mfa,
        str(user.email),
    )

    # Get client info for audit logging
    client_ip, user_agent = _get_client_info(request)

    if not is_valid:
        logger.warning(f"MFA verification failed for user: {str(user.email)}")
        audit_logger.log_mfa_event(
            event_type=AuditEventType.AUTH_MFA_VERIFY,
            outcome=AuditOutcome.FAILURE,
            user_id=user.id,
            username=str(user.email),
            source_ip=client_ip,
            user_agent=user_agent,
            error_code="INVALID_MFA_CODE",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid MFA code",
        )

    # Complete verification and generate tokens
    return _complete_mfa_verification(
        db,
        user,
        user_mfa,
        user_uuid_str,
        user_role,
        mfa_jti,
        used_backup_code,
        client_ip,
        user_agent,
    )


@router.post("/mfa/disable")
@limiter.limit(get_auth_rate_limit())
def disable_mfa(
    request: Request,
    request_body: MFADisableRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Disable MFA for the current user.

    Requires a valid TOTP code or backup code to confirm the action.
    """
    if not _is_mfa_enabled(db):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA is not enabled on this system",
        )

    # Get user's MFA record
    user_mfa = db.query(UserMFA).filter(UserMFA.user_id == current_user.id).first()

    if not user_mfa or not user_mfa.totp_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA is not enabled for your account",
        )

    code = request_body.code.replace("-", "").replace(" ", "")
    is_valid = False

    # Decrypt the TOTP secret for verification (stored encrypted at rest)
    try:
        decrypted_secret = MFAService.decrypt_totp_secret(str(user_mfa.totp_secret))
    except ValueError as e:
        logger.error(f"Failed to decrypt TOTP secret for user: {str(current_user.email)}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to verify MFA. Please contact support.",
        ) from e

    # Try TOTP verification first
    if len(code) == 6 and code.isdigit():
        is_valid = MFAService.verify_totp(decrypted_secret, code, user_id=current_user.id)

    # Try backup code verification
    if not is_valid:
        backup_codes_list: list[str] = list(user_mfa.backup_codes)
        is_valid, _ = MFAService.verify_backup_code(request_body.code, backup_codes_list)

    # Get client info for audit logging
    client_ip, user_agent = _get_client_info(request)

    if not is_valid:
        logger.warning(f"MFA disable attempt failed for user: {str(current_user.email)}")
        # Log MFA disable failure
        audit_logger.log_mfa_event(
            event_type=AuditEventType.AUTH_MFA_DISABLE,
            outcome=AuditOutcome.FAILURE,
            user_id=current_user.id,
            username=str(current_user.email),
            source_ip=client_ip,
            user_agent=user_agent,
            error_code="INVALID_VERIFICATION_CODE",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid verification code",
        )

    # Delete the MFA record
    db.delete(user_mfa)
    db.commit()

    # Log MFA disabled
    audit_logger.log_mfa_event(
        event_type=AuditEventType.AUTH_MFA_DISABLE,
        outcome=AuditOutcome.SUCCESS,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=client_ip,
        user_agent=user_agent,
    )

    logger.info(f"MFA disabled for user: {str(current_user.email)}")

    return JSONResponse(content={"message": "MFA has been disabled successfully"})
