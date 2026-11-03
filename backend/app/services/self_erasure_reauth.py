"""Re-authentication for a self-service erasure request (#1354, decision D4).

The caller proves they are the account holder again, in the request itself: their password (an
account that can sign in with one) or a current TOTP code (an account with MFA enabled). An
account with neither route -- an SSO-only account without MFA -- gets ``reauth_unavailable`` and
is pointed at the operator, rather than being let through on the session alone.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.auth.mfa import MFAService
from app.core.security import verify_password
from app.models.user import User


def _mfa_secret(db: Session, user: User) -> str | None:
    from app.models.user_mfa import UserMFA

    row = (
        db.query(UserMFA).filter(UserMFA.user_id == user.id, UserMFA.totp_enabled.is_(True)).first()
    )
    if row is None:
        return None
    return MFAService.decrypt_totp_secret(str(row.totp_secret))


def can_reauthenticate(db: Session, user: User) -> bool:
    uses_password = user.auth_type == "local" or bool(user.allow_local_fallback)
    return uses_password or _mfa_secret(db, user) is not None


def verify_reauth(db: Session, user: User, *, password: str | None, mfa_code: str | None) -> bool:
    """True when the password or the TOTP code the caller supplied is valid for ``user``."""
    uses_password = user.auth_type == "local" or bool(user.allow_local_fallback)
    if password and uses_password and verify_password(password, str(user.hashed_password)):
        return True
    if mfa_code:
        secret = _mfa_secret(db, user)
        if secret is not None:
            return MFAService.verify_totp(secret, mfa_code, int(user.id))
    return False
