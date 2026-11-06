"""Read-only questions to the directory about accounts that already exist.

Used by the periodic directory sync and the admin group-mapping test: open one
service-account session, then ask per account whether it is present, disabled, no
longer entitled, or gone. A failure to ASK is never reported as an answer: it raises
:class:`LdapDirectoryUnavailableError`, whose text carries only the error type (the
ldap3 message can quote the directory URL and bind DN and goes to the log instead).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from ldap3 import Connection
from ldap3.core.exceptions import LDAPException

from app.auth.ldap_auth import LdapConfig
from app.auth.ldap_auth import _directory_username
from app.auth.ldap_auth import _is_ldap_admin
from app.auth.ldap_directory import _bind_service_account
from app.auth.ldap_directory import _check_group_access
from app.auth.ldap_directory import _close_connection
from app.auth.ldap_directory import _get_ldap_server
from app.auth.ldap_directory import _get_user_groups
from app.auth.ldap_directory import _search_ldap_user

logger = logging.getLogger(__name__)

DIRECTORY_PRESENT = "present"
DIRECTORY_ABSENT = "absent"
DIRECTORY_DISABLED = "disabled"
DIRECTORY_NOT_ENTITLED = "not_entitled"

#: Active Directory ``userAccountControl`` flag ACCOUNTDISABLE.
AD_UF_ACCOUNT_DISABLE = 0x0002
_ACCOUNT_CONTROL_ATTR = "userAccountControl"


class LdapDirectoryUnavailableError(Exception):
    """The directory could not be asked; says nothing about any account."""


@dataclass(frozen=True)
class LdapProbe:
    """What the directory says about one account."""

    status: str
    groups: tuple[str, ...] = ()
    is_admin: bool = False


def _unavailable(what: str, exc: BaseException) -> LdapDirectoryUnavailableError:
    """Log the full cause and build the opaque error that replaces it."""
    logger.error("LDAP %s: %s: %s", what, type(exc).__name__, exc, exc_info=exc)
    return LdapDirectoryUnavailableError(f"LDAP {what} ({type(exc).__name__})")


@contextmanager
def ldap_directory_session(cfg: LdapConfig) -> Iterator[Connection]:
    """Yield a bound service-account connection, closed however the body exits.

    Args:
        cfg: Resolved LDAP configuration.

    Yields:
        The bound connection.

    Raises:
        LdapDirectoryUnavailableError: The server could not be reached or the service
            account could not bind.
    """
    try:
        server = _get_ldap_server(cfg)
        conn = _bind_service_account(cfg, server)
    except LDAPException as exc:
        raise _unavailable("bind failed", exc) from exc
    except Exception as exc:
        raise _unavailable("connection failed", exc) from exc
    if conn is None:
        raise LdapDirectoryUnavailableError("LDAP service account bind returned no connection")
    try:
        yield conn
    finally:
        _close_connection(conn, "directory-sync")


def _account_disabled(user_entry: Any) -> bool:
    """Whether AD's ACCOUNTDISABLE bit is set; a missing or unreadable value is not."""
    if _ACCOUNT_CONTROL_ATTR not in user_entry:
        return False
    raw = user_entry[_ACCOUNT_CONTROL_ATTR].value
    if raw is None:
        return False
    try:
        flags = int(raw)
    except (TypeError, ValueError):
        logger.debug("Ignoring an unreadable userAccountControl value %r", raw)
        return False
    return bool(flags & AD_UF_ACCOUNT_DISABLE)


def probe_ldap_user(cfg: LdapConfig, bind_conn: Any, ldap_uid: str, email: str = "") -> LdapProbe:
    """Ask the directory about one stored account without authenticating as it.

    Args:
        cfg: Resolved LDAP configuration.
        bind_conn: A bound service connection (see :func:`ldap_directory_session`).
        ldap_uid: The account's stored ``ldap_uid``.
        email: The account's stored email, used for the email fallback search.

    Returns:
        ``present`` (with groups and admin signal), ``absent``, ``disabled`` or
        ``not_entitled``.

    Raises:
        LdapDirectoryUnavailableError: The search or the group check failed.
    """
    try:
        entry = _search_ldap_user(
            cfg,
            bind_conn,
            email or ldap_uid,
            ldap_uid.split("@", 1)[0],
            extra_attributes=[_ACCOUNT_CONTROL_ATTR],
        )
    except LDAPException as exc:
        raise _unavailable("search failed", exc) from exc
    if entry is None:
        return LdapProbe(DIRECTORY_ABSENT)
    if _account_disabled(entry):
        return LdapProbe(DIRECTORY_DISABLED)

    groups = _get_user_groups(cfg, entry)
    try:
        entitled = _check_group_access(cfg, bind_conn, entry.entry_dn, groups, ldap_uid)
    except LDAPException as exc:
        raise _unavailable("group check failed", exc) from exc
    if not entitled:
        return LdapProbe(DIRECTORY_NOT_ENTITLED)

    is_admin = _is_ldap_admin(
        cfg,
        _directory_username(cfg, entry, ldap_uid),
        groups,
        bind_conn=bind_conn,
        user_dn=entry.entry_dn,
    )
    return LdapProbe(DIRECTORY_PRESENT, tuple(groups), is_admin)
