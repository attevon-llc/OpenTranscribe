"""Gate for the anonymous local-account routes when local authentication is off.

``local_enabled`` (DB-backed, ``LOCAL_AUTH_ENABLED`` as the env fallback) used to be
read by the login path only. The anonymous routes that exist purely to serve local
password accounts — email verification, self-registration, password reset — kept
doing full work with it off: a user lookup, an audit write, sometimes a token row,
all for a feature the deployment had switched off, and all available to any
anonymous caller (issue #997).

Two account classes survive local auth being off, and this module is where the
routes tell them apart:

* nobody, for email verification and registration — both exist only for local
  password login;
* the active ``super_admin`` break-glass account, for password reset. It is exempt
  from ``local_enabled`` at login (``authenticators._local_auth_permitted``), and
  resetting its password is the documented way back in when the identity provider
  is misconfigured and the break-glass password is lost (issue #910).

``/token`` is deliberately NOT gated here: LDAP authenticates through the same form,
and the break-glass login must keep working.
"""

from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from sqlalchemy.orm import Session

from app.auth.roles import ROLE_SUPER_ADMIN
from app.core.auth_settings import get_auth_settings
from app.db.base import get_db
from app.models.user import User


def local_auth_enabled(db: Session) -> bool:
    """Whether local password accounts may authenticate on this deployment."""
    return bool(get_auth_settings(db).local_enabled)


def require_local_auth_enabled(db: Session = Depends(get_db)) -> None:
    """Route dependency: 404 when local authentication is disabled.

    404 rather than 403 because, with local auth off, the route does not exist for
    this deployment — the same answer a capability-disabled route gives.
    """
    if not local_auth_enabled(db):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")


def is_break_glass_account(user: User | None) -> bool:
    """Whether *user* keeps local-password access with local auth disabled."""
    return user is not None and bool(user.is_active) and user.role == ROLE_SUPER_ADMIN
