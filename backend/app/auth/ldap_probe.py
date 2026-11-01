"""Read-only LDAP directory probes for deprovisioning and the admin mapping test.

``ldap_directory_session`` binds the service account or raises
:class:`LdapDirectoryUnavailableError`; ``probe_ldap_user`` reports one account as present,
absent, disabled or no longer entitled, using the same search, entitlement and admin rules
login uses (``auth/ldap_directory.py``, ``auth/ldap_auth.py``).
"""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from ldap3 import Connection
from ldap3.core.exceptions import LDAPException

from app.auth.ldap_auth import LdapConfig
from app.auth.ldap_auth import _is_ldap_admin
from app.auth.ldap_directory import _bind_service_account
from app.auth.ldap_directory import _check_group_access
from app.auth.ldap_directory import _close_connection
from app.auth.ldap_directory import _get_ldap_server
from app.auth.ldap_directory import _get_user_groups
from app.auth.ldap_directory import _search_ldap_user

logger = logging.getLogger(__name__)


# =============================================================================
# Directory reconciliation (read-only probes for deprovisioning)
# =============================================================================
#
# Login-time sync is upward-only: it can create and promote, and it can refuse a
# login, but nothing ever walks the accounts that STOPPED logging in. These two
# helpers give the periodic sweep in ``services/directory_sync_service.py`` a
# read-only way to ask "is this identity still there?" without a second LDAP
# client — they reuse the same config, bind and search path as authentication.


class LdapDirectoryUnavailableError(Exception):
    """The directory could not be consulted, so nothing may be concluded from it.

    Deliberately distinct from "the directory says this user is gone". A caller
    that deprovisions accounts MUST treat this as "do nothing": disabling every
    LDAP user because the server was down for a minute is far worse than the
    stale-account window it would be closing.
    """


#: The directory answered and the account is present, enabled and still entitled.
DIRECTORY_PRESENT = "present"
#: The directory answered and holds no entry for this account.
DIRECTORY_ABSENT = "absent"
#: The entry exists but Active Directory flags it as a disabled account.
DIRECTORY_DISABLED = "disabled"
#: The entry exists but is no longer in any of the configured ``user_groups``.
DIRECTORY_NOT_ENTITLED = "not_entitled"

#: ``userAccountControl`` ADS_UF_ACCOUNTDISABLE bit. AD keeps disabled accounts as
#: live entries, so presence alone would never notice an offboarded user.
AD_UF_ACCOUNT_DISABLE = 0x0002
_UAC_ATTR = "userAccountControl"


@dataclass(frozen=True)
class LdapProbe:
    """One directory answer about one account.

    ``groups`` and ``is_admin`` are what let the periodic sweep reconcile group
    mappings and privilege, not just deprovision. ``is_admin`` is evaluated with
    the same :func:`_is_ldap_admin` rule login uses — without it the sweep would
    see "no admin signal" for every account promoted via ``admin_groups`` and
    demote the entire admin population on its first pass.

    Both are empty for any status other than :data:`DIRECTORY_PRESENT`: a user the
    directory does not have, or has disabled, asserts no memberships.
    """

    status: str
    groups: tuple[str, ...] = ()
    is_admin: bool = False


@contextmanager
def ldap_directory_session(cfg: LdapConfig) -> Iterator[Connection]:
    """Yield a service-account-bound connection, or raise ``LdapDirectoryUnavailableError``.

    Args:
        cfg: Resolved LDAP configuration (DB > .env > defaults).

    Yields:
        A bound ``Connection`` usable for read-only searches.

    Raises:
        LdapDirectoryUnavailableError: The server is unreachable or the service account
            cannot bind. Never raised to mean "user not found".
    """
    conn: Connection | None = None
    try:
        conn = _bind_service_account(cfg, _get_ldap_server(cfg))
    except LDAPException as e:
        # Scrubbed at the raiser (the repo's "one owner" preference) rather than
        # at each of this error's consumers -- an ldap3 LDAPException can quote
        # the directory URL and the service-account bind DN (#914).
        logger.exception("LDAP service-account bind failed")
        raise LdapDirectoryUnavailableError(f"LDAP bind failed ({type(e).__name__})") from e
    except Exception as e:  # noqa: BLE001 - any failure here means "could not ask"
        logger.exception("LDAP connection failed")
        raise LdapDirectoryUnavailableError(f"LDAP connection failed ({type(e).__name__})") from e

    if conn is None:
        raise LdapDirectoryUnavailableError("LDAP service account bind returned no connection")

    try:
        yield conn
    finally:
        _close_connection(conn, "directory-sync")


def _is_ad_account_disabled(user_entry) -> bool:
    """Return True only when AD positively reports the ACCOUNTDISABLE bit.

    An absent or unparseable ``userAccountControl`` means "this server doesn't
    publish account state" — reported as *not* disabled, because a missing answer
    must never be read as a reason to deprovision.
    """
    if _UAC_ATTR not in user_entry:
        return False
    raw = user_entry[_UAC_ATTR].value
    if raw is None:
        return False
    try:
        return bool(int(raw) & AD_UF_ACCOUNT_DISABLE)
    except (TypeError, ValueError):
        logger.debug(f"Unparseable {_UAC_ATTR} value {raw!r}; treating account as enabled")
        return False


def probe_ldap_user(
    cfg: LdapConfig, bind_conn: Connection, ldap_uid: str, email: str = ""
) -> LdapProbe:
    """Ask the directory about one account, without authenticating as it.

    Args:
        cfg: Resolved LDAP configuration.
        bind_conn: Connection from :func:`ldap_directory_session`.
        ldap_uid: The stored ``User.ldap_uid``.
        email: The stored email, used for the same fallback search login uses.

    Returns:
        An :class:`LdapProbe` whose ``status`` is one of ``DIRECTORY_PRESENT`` /
        ``DIRECTORY_ABSENT`` / ``DIRECTORY_DISABLED`` / ``DIRECTORY_NOT_ENTITLED``,
        carrying the account's groups and admin signal when it is present.

    Raises:
        LdapDirectoryUnavailableError: The search itself failed. The account's state is
            unknown and the caller must not act on it.
    """
    ldap_username = ldap_uid.split("@")[0] if "@" in ldap_uid else ldap_uid
    try:
        user_entry = _search_ldap_user(
            cfg, bind_conn, email or ldap_uid, ldap_username, extra_attributes=[_UAC_ATTR]
        )
    except LDAPException as e:
        logger.exception("LDAP search failed")
        raise LdapDirectoryUnavailableError(f"LDAP search failed ({type(e).__name__})") from e

    if not user_entry:
        return LdapProbe(DIRECTORY_ABSENT)

    if _is_ad_account_disabled(user_entry):
        return LdapProbe(DIRECTORY_DISABLED)

    user_groups = _get_user_groups(cfg, user_entry)

    # Same entitlement rule login enforces, so the sweep can never disable an
    # account that would still be allowed to log in.
    try:
        entitled = _check_group_access(cfg, bind_conn, user_entry.entry_dn, user_groups, ldap_uid)
    except LDAPException as e:
        logger.exception("LDAP group check failed")
        raise LdapDirectoryUnavailableError(f"LDAP group check failed ({type(e).__name__})") from e

    if not entitled:
        return LdapProbe(DIRECTORY_NOT_ENTITLED)

    is_admin = _is_ldap_admin(
        cfg, ldap_username, user_groups, bind_conn=bind_conn, user_dn=user_entry.entry_dn
    )
    return LdapProbe(DIRECTORY_PRESENT, tuple(user_groups), is_admin)
