"""Who must use MFA: everyone (``mfa_required``) or administrators (admin and super_admin) only.

Kept free of request/DB types so the login gate, the MFA status endpoint and the
password policy (which lowers the NIST minimum length for MFA-protected users) all
ask the same question. The caller supplies the layered settings object
(``get_auth_settings(db)`` or ``get_process_auth_settings()``).
"""

from __future__ import annotations

from typing import Any

from app.auth.roles import ELEVATED_ROLES
from app.core.config import settings


def user_is_mfa_protected(db: Any, user: Any) -> bool:
    """True when *user* has TOTP enrolled, or MFA is mandatory for them.

    The second case counts because login refuses to issue a session until they enrol, so
    the password is never the sole factor in practice. Drives the NIST 8-character floor.
    """
    from app.core.auth_settings import get_auth_settings
    from app.models.user_mfa import UserMFA

    enrolled = (
        db.query(UserMFA.id)
        .filter(UserMFA.user_id == user.id, UserMFA.totp_enabled.is_(True))
        .first()
        is not None
    )
    return enrolled or mfa_required_for_user(get_auth_settings(db), user)


def mfa_required_for_user(auth_settings: Any, user: Any | None = None) -> bool:
    """True when MFA enrolment/verification is mandatory for *user*.

    ``user=None`` answers the global question (is MFA required of everybody), which is
    what the public auth-methods response advertises. Nothing is required while the MFA
    feature itself is off - the flag would otherwise demand a second factor that cannot
    be enrolled.
    """
    enabled = auth_settings.mfa_enabled or settings.MFA_ENABLED
    if not enabled:
        return False
    if auth_settings.get_bool("mfa_required", settings.MFA_REQUIRED):
        return True
    if user is not None and auth_settings.get_bool(
        "mfa_required_for_admins", settings.MFA_REQUIRED_FOR_ADMINS
    ):
        return str(getattr(user, "role", "")) in ELEVATED_ROLES
    return False
