"""Current-user endpoints: ``/me``, ``/session``, ``/me/certificate``."""

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from app.api.endpoints.auth.dependencies import get_current_active_user
from app.api.endpoints.auth.dependencies import get_current_user
from app.api.endpoints.auth.dependencies import oauth2_scheme
from app.db.base import get_db
from app.models.user import User
from app.schemas.user import User as UserSchema

router = APIRouter()


@router.get("/me", response_model=UserSchema, summary="Get current user")
def read_users_me(current_user: User = Depends(get_current_user)):
    """
    Get current user using the current_user dependency
    """
    return current_user


@router.get("/session", summary="Cookie-session status probe (never 401s)")
def session_status(
    request: Request,
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> dict:
    """Report whether the caller has a valid session — with 200 either way.

    The SPA's initAuth probes this on every page load. Anonymous visitors
    must not receive a 401 (the browser logs every 401 as a console error
    and the old /auth/me probe triggered a spurious logout cascade).

    ``refreshable`` is true when no valid access token was presented but a
    refresh_token cookie is — the client should attempt one silent refresh
    before treating the visitor as logged out.
    """
    from app.auth.cookies import get_refresh_token_from_cookie

    try:
        user = get_current_user(request, token=token, db=db)
        return {
            "authenticated": True,
            "refreshable": False,
            "user": UserSchema.model_validate(user).model_dump(mode="json"),
        }
    except HTTPException as exc:
        if exc.status_code != status.HTTP_401_UNAUTHORIZED:
            raise
        return {
            "authenticated": False,
            "refreshable": get_refresh_token_from_cookie(request) is not None,
            "user": None,
        }


@router.get("/me/certificate", summary="Get current user's certificate info")
def get_user_certificate_info(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    """
    Get certificate information for the current user.

    Returns certificate metadata for users who authenticated via PKI (X.509)
    or via an OIDC provider brokering X.509 certificate authentication.

    For non-PKI users, returns has_certificate: false.

    Certificate metadata includes:
    - subject_dn: Full Distinguished Name from certificate
    - serial_number: Certificate serial number (hex format)
    - issuer_dn: Certificate issuer Distinguished Name
    - organization: Organization from certificate subject
    - organizational_unit: Organizational unit from certificate subject
    - valid_from: Certificate validity start date (ISO format)
    - valid_until: Certificate validity end date (ISO format)
    - fingerprint: SHA-256 fingerprint (colon-separated hex)
    """
    # Check if user has certificate metadata stored
    has_cert_metadata = bool(
        current_user.pki_subject_dn
        or current_user.pki_fingerprint_sha256
        or current_user.pki_serial_number
    )

    if not has_cert_metadata:
        return {"has_certificate": False}

    # Format fingerprint with colons for display
    fingerprint_formatted = None
    if current_user.pki_fingerprint_sha256:
        fp = str(current_user.pki_fingerprint_sha256)
        fingerprint_formatted = ":".join(fp[i : i + 2] for i in range(0, len(fp), 2)).upper()

    return {
        "has_certificate": True,
        "subject_dn": current_user.pki_subject_dn,
        "common_name": current_user.pki_common_name,
        "serial_number": current_user.pki_serial_number,
        "issuer_dn": current_user.pki_issuer_dn,
        "organization": current_user.pki_organization,
        "organizational_unit": current_user.pki_organizational_unit,
        "valid_from": current_user.pki_not_before.isoformat()
        if current_user.pki_not_before
        else None,
        "valid_until": current_user.pki_not_after.isoformat()
        if current_user.pki_not_after
        else None,
        "fingerprint": fingerprint_formatted,
    }
