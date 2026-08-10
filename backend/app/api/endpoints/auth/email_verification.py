"""Email-verification endpoints (v375).

Both routes are public — a user who cannot log in until their address is
verified obviously cannot authenticate to verify it — and both are rate-limited
like every other unauthenticated auth route.

The resend route answers identically for a registered address, an unknown one,
and an already-verified one: anything else is an account-existence oracle that
needs no session.
"""

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import status
from sqlalchemy.orm import Session

from app.auth.email_verification import resend_verification
from app.auth.email_verification import verify_email
from app.auth.rate_limit import get_auth_rate_limit
from app.auth.rate_limit import limiter
from app.db.base import get_db
from app.schemas.invitation import EmailVerificationRequest
from app.schemas.invitation import EmailVerificationResendRequest

logger = logging.getLogger(__name__)

router = APIRouter()

#: Constant response for the resend route — see the module docstring.
_RESEND_MESSAGE = "If that address needs verification, a new link has been sent."


@router.post("/verify-email")
@limiter.limit(get_auth_rate_limit())
def verify_email_endpoint(
    request: Request,
    response: Response,
    body: EmailVerificationRequest,
    db: Session = Depends(get_db),
):
    """Redeem an email-verification token.

    Returns 400 with one generic message for unknown, used and expired tokens
    alike. Rate limited per IP.
    """
    ok, error = verify_email(db, body.token)
    if not ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error)
    return {"message": "Your email address has been verified. You can sign in now."}


@router.post("/verify-email/resend")
@limiter.limit(get_auth_rate_limit())
def resend_verification_endpoint(
    request: Request,
    response: Response,
    body: EmailVerificationResendRequest,
    db: Session = Depends(get_db),
):
    """Request a fresh verification link. Always 200. Rate limited per IP."""
    client_ip = request.client.host if request.client else "unknown"
    resend_verification(db, body.email, client_ip)
    return {"message": _RESEND_MESSAGE}
