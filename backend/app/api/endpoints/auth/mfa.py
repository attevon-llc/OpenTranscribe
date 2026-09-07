"""MFA setup / verification / disable endpoints."""

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.endpoints.auth.dependencies import _get_client_info
from app.api.endpoints.auth.dependencies import get_current_active_user
from app.api.endpoints.auth.mfa_enrollment import EnrollmentContext
from app.api.endpoints.auth.mfa_enrollment import _complete_mfa_verification
from app.api.endpoints.auth.mfa_enrollment import get_user_for_enrollment
from app.api.endpoints.auth.mfa_enrollment import issue_session_response
from app.api.endpoints.auth.mfa_tokens import _claim_mfa_token
from app.api.endpoints.auth.mfa_tokens import _get_user_for_mfa
from app.api.endpoints.auth.mfa_tokens import _is_mfa_enabled
from app.api.endpoints.auth.mfa_tokens import _is_mfa_required
from app.api.endpoints.auth.mfa_tokens import _user_can_setup_mfa
from app.api.endpoints.auth.mfa_tokens import _verify_mfa_code
from app.api.endpoints.auth.mfa_tokens import mfa_token_ttl_seconds
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.mfa import MFAService
from app.auth.rate_limit import get_auth_rate_limit
from app.auth.rate_limit import limiter
from app.core.auth_settings import get_auth_settings
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
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Get MFA status for the current user.

    Returns whether MFA is enabled/configured for the user and
    whether the system requires MFA.

    Needs a full session, not a mid-login one: forced enrolment goes through
    ``/mfa/setup`` + ``/mfa/verify-setup`` under ``get_user_for_enrollment``,
    and the only caller of this route is the in-app security settings panel.
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
@limiter.limit(get_auth_rate_limit())
def setup_mfa(
    request: Request,
    response: Response,
    enrollment: EnrollmentContext = Depends(get_user_for_enrollment),
    db: Session = Depends(get_db),
):
    """
    Initiate MFA setup for the current user.

    Returns the TOTP secret, provisioning URI, and QR code for authenticator app setup.
    The user must verify with a valid TOTP code to complete setup.

    Authorized by a normal session OR an enrolment-scoped half-token (forced enrolment
    on an MFA_REQUIRED deployment). A *verify*-scoped half-token is refused: this
    endpoint overwrites the TOTP secret and wipes the backup codes, so honouring one
    would let a password-only attacker reset the second factor.

    Rate limited: reachable pre-session, and every call generates a secret and renders
    a QR code (CPU).

    Note: This endpoint is only available when MFA is enabled and the user
    is not using PKI or OIDC authentication (which handle MFA separately).
    """
    current_user = enrollment.user

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

    # Generate provisioning URI for authenticator apps (uses plaintext secret).
    # The issuer is what the authenticator app displays next to the code, and it
    # is admin-settable — pass the session-resolved value rather than letting the
    # service fall back, so an issuer saved seconds ago is already in this QR.
    provisioning_uri = MFAService.get_provisioning_uri(
        secret=totp_secret,
        email=str(current_user.email),
        issuer_name=get_auth_settings(db).mfa_issuer_name,
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
@limiter.limit(get_auth_rate_limit())
def verify_mfa_setup(
    request: Request,
    response: Response,
    request_body: MFAVerifySetupRequest,
    enrollment: EnrollmentContext = Depends(get_user_for_enrollment),
    db: Session = Depends(get_db),
):
    """
    Verify MFA setup with the initial TOTP code.

    This completes the MFA setup process and generates backup codes.
    Backup codes are returned only once - the user must save them securely.

    When the call was authorized by an enrolment half-token (forced enrolment), the
    token is burned here — not at /mfa/setup, which the user may legitimately re-run to
    re-render the QR code — and the response additionally carries a real session, so the
    user lands logged in instead of being bounced back to the login form.

    Rate limited: this is a 6-digit-code guessing surface, reachable pre-session.
    """
    current_user = enrollment.user

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

    # Possession is proven — burn the enrolment half-token now, before MFA is enabled.
    # Claiming only after a *successful* code check keeps a mistyped code from costing
    # the user their enrolment token, while still making the token single-use: a second
    # /mfa/setup + replay of the same token cannot re-run enrolment.
    if enrollment.mfa_jti and not _claim_mfa_token(enrollment.mfa_jti, mfa_token_ttl_seconds()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="MFA token has already been used",
        )

    # How many codes is admin-settable; calling with no argument used the .env
    # value and ignored it.
    backup_codes = MFAService.generate_backup_codes(
        count=get_auth_settings(db).mfa_backup_code_count
    )
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

    body = {
        "success": True,
        "backup_codes": backup_codes,  # Return plaintext codes only once
        "message": "MFA has been enabled successfully. Save your backup codes securely.",
    }

    if enrollment.mfa_jti:
        # Forced enrolment: the caller has no session yet and has just proven the second
        # factor, so issue one the same way /mfa/verify does (cookies included).
        return issue_session_response(
            db,
            current_user,
            str(current_user.uuid),
            enrollment.user_role,
            client_ip,
            user_agent,
            request,
            extra_content=body,
        )

    return JSONResponse(content=body)


@router.post("/mfa/verify", response_model=MFAVerifyResponse)
@limiter.limit(get_auth_rate_limit())
def verify_mfa(
    request: Request,
    response: Response,
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
        request,
    )


@router.post("/mfa/disable")
@limiter.limit(get_auth_rate_limit())
def disable_mfa(
    request: Request,
    response: Response,
    request_body: MFADisableRequest,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):
    """
    Disable MFA for the current user.

    Requires a valid TOTP code or backup code to confirm the action.

    Unlike the enrolment pair this is not a mid-login state — it weakens an
    already-usable account, so it runs behind the account-lifecycle gate.
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
        is_valid, matched_hash = MFAService.verify_backup_code(request_body.code, backup_codes_list)
        if is_valid and matched_hash:
            # Consume it. A backup code is one-time use: accepting it here without
            # removing it left the code valid forever, so a single leaked code could
            # keep disabling MFA every time it was re-enabled. Mirrors
            # mfa_tokens._verify_mfa_code, which has always consumed the match.
            user_mfa.backup_codes = [c for c in backup_codes_list if c != matched_hash]  # type: ignore[assignment]
            db.commit()
            logger.info(
                f"Backup code consumed for MFA disable by user: {str(current_user.email)}. "
                f"{len(user_mfa.backup_codes)} codes remaining."
            )

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
