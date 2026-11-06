"""LDAP / Active Directory login: configuration, password check and account provisioning.

A login resolves one immutable :class:`LdapConfig`, finds the user's entry with the
service account, checks the entry's email and group entitlement, verifies the password
by binding as the entry's DN, and then provisions (or refreshes) the local ``User`` row.
Directory I/O lives in :mod:`app.auth.ldap_directory`; the read-only probes used by the
directory sync live in :mod:`app.auth.ldap_probe`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from typing import TypedDict

from ldap3.core.exceptions import LDAPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.account_linking import assert_email_link_permitted
from app.auth.account_linking import assert_provider_id_link_permitted
from app.auth.approval import initial_approval_status
from app.auth.constants import EXTERNAL_AUTH_NO_PASSWORD
from app.auth.ldap_directory import _bind_service_account
from app.auth.ldap_directory import _check_group_access
from app.auth.ldap_directory import _close_connection
from app.auth.ldap_directory import _connect_and_bind
from app.auth.ldap_directory import _get_ldap_server
from app.auth.ldap_directory import _get_user_groups
from app.auth.ldap_directory import _is_member_of_groups
from app.auth.ldap_directory import _parse_group_list
from app.auth.ldap_directory import _search_ldap_user
from app.auth.ldap_directory import _search_recursive_group_membership
from app.auth.roles import ROLE_ADMIN
from app.auth.roles import ROLE_USER
from app.auth.roles import role_implies_superuser
from app.core import auth_settings
from app.core.auth_types import AUTH_TYPE_LDAP
from app.core.auth_types import AUTH_TYPE_LOCAL
from app.core.config import resolve_ldap_search_filter
from app.core.config import settings
from app.core.env import is_truthy
from app.models.group import MAPPING_SOURCE_LDAP
from app.models.user import User
from app.services import idp_group_mapping_service

logger = logging.getLogger(__name__)

#: A directory's ``mail`` attribute is writable data, not a verified address.
LDAP_ASSERTS_EMAIL_VERIFIED = False

_LOGIN_REFUSED = "Incorrect username or password"
_BEARER_CHALLENGE = {"WWW-Authenticate": "Bearer"}
_EMAIL_SHAPE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

#: Value used when a stored string setting is empty, per field (``""`` for the rest).
_EMPTY_STRING_DEFAULTS = {
    "username_attr": "uid",
    "user_search_filter": "(uid={username})",
    "email_attr": "mail",
    "name_attr": "cn",
}


@dataclass(frozen=True)
class LdapConfig:
    """Everything one LDAP login or probe needs, resolved once and never mutated."""

    enabled: bool = False
    server: str = ""
    port: int = 636
    use_ssl: bool = True
    use_tls: bool = False
    bind_dn: str = ""
    bind_password: str = ""
    search_base: str = ""
    username_attr: str = "uid"
    email_attr: str = "mail"
    name_attr: str = "cn"
    group_attr: str = "memberOf"
    user_search_filter: str = "(uid={username})"
    admin_users: str = ""
    admin_groups: str = ""
    user_groups: str = ""
    recursive_groups: bool = False
    timeout: int = 10

    @classmethod
    def from_env(cls) -> LdapConfig:
        """Build the configuration from the process settings (the ``LDAP_*`` variables).

        Returns:
            The environment's LDAP configuration.
        """
        return cls(
            enabled=settings.LDAP_ENABLED,
            server=settings.LDAP_SERVER,
            port=settings.LDAP_PORT,
            use_ssl=settings.LDAP_USE_SSL,
            use_tls=getattr(settings, "LDAP_USE_TLS", False),
            bind_dn=settings.LDAP_BIND_DN,
            bind_password=settings.LDAP_BIND_PASSWORD,
            search_base=settings.LDAP_SEARCH_BASE,
            username_attr=settings.LDAP_USERNAME_ATTR,
            email_attr=settings.LDAP_EMAIL_ATTR,
            name_attr=settings.LDAP_NAME_ATTR,
            group_attr=settings.LDAP_GROUP_ATTR,
            user_search_filter=settings.LDAP_USER_SEARCH_FILTER,
            admin_users=settings.LDAP_ADMIN_USERS,
            admin_groups=settings.LDAP_ADMIN_GROUPS,
            user_groups=settings.LDAP_USER_GROUPS,
            recursive_groups=settings.LDAP_RECURSIVE_GROUPS,
            timeout=settings.LDAP_TIMEOUT,
        )

    @classmethod
    def from_db(cls, db: Session) -> LdapConfig:
        """Build the configuration from the admin-managed ``auth_config`` keys.

        Each ``ldap_*`` key is read through the dynamic auth settings; a key with no
        value takes the matching ``LDAP_*`` environment setting.

        Args:
            db: Database session.

        Returns:
            The effective LDAP configuration.
        """
        stored = auth_settings.get_auth_settings(db)
        env = cls.from_env()

        def flag(key: str, fallback: bool) -> bool:
            return _as_bool(stored.get(key), fallback)

        def number(key: str, fallback: int) -> int:
            return _as_int(stored.get(key), fallback)

        def string(key: str, field_name: str, fallback: str) -> str:
            return _as_text(stored.get(key), fallback, _EMPTY_STRING_DEFAULTS.get(field_name, ""))

        username_attr = string("ldap_username_attr", "username_attr", env.username_attr)
        search_filter = string(
            "ldap_user_search_filter", "user_search_filter", env.user_search_filter
        )
        return cls(
            enabled=flag("ldap_enabled", env.enabled),
            server=string("ldap_server", "server", env.server),
            port=number("ldap_port", env.port),
            use_ssl=flag("ldap_use_ssl", env.use_ssl),
            use_tls=flag("ldap_use_tls", env.use_tls),
            bind_dn=string("ldap_bind_dn", "bind_dn", env.bind_dn),
            bind_password=string("ldap_bind_password", "bind_password", env.bind_password),
            search_base=string("ldap_search_base", "search_base", env.search_base),
            username_attr=username_attr,
            email_attr=string("ldap_email_attr", "email_attr", env.email_attr),
            name_attr=string("ldap_name_attr", "name_attr", env.name_attr),
            group_attr=string("ldap_group_attr", "group_attr", env.group_attr),
            user_search_filter=resolve_ldap_search_filter(search_filter, username_attr),
            admin_users=string("ldap_admin_users", "admin_users", env.admin_users),
            admin_groups=string("ldap_admin_groups", "admin_groups", env.admin_groups),
            user_groups=string("ldap_user_groups", "user_groups", env.user_groups),
            recursive_groups=flag("ldap_recursive_groups", env.recursive_groups),
            timeout=number("ldap_timeout", env.timeout),
        )


def _as_bool(raw: Any, fallback: bool) -> bool:
    if raw is None:
        return fallback
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return is_truthy(raw)
    return bool(raw)


def _as_int(raw: Any, fallback: int) -> int:
    if raw is None:
        return fallback
    try:
        return int(raw)
    except (TypeError, ValueError):
        return fallback


def _as_text(raw: Any, fallback: str, when_empty: str) -> str:
    resolved = str(fallback if raw is None else raw)
    return resolved or when_empty


class LdapUserData(TypedDict):
    """What a successful directory login reports about the user."""

    username: str
    email: str
    full_name: str
    is_admin: bool
    groups: list[str]


def _is_valid_email(email: str) -> bool:
    """Whether ``email`` has the shape ``local@domain.tld`` with no whitespace.

    Args:
        email: Value read from the directory.

    Returns:
        ``True`` for a plausible address, ``False`` otherwise (including empty).
    """
    return bool(email) and _EMAIL_SHAPE.fullmatch(email) is not None


def _entry_text(user_entry: Any, attr: str) -> str:
    """The string value of ``attr`` on the entry, or ``""`` when absent or empty."""
    if not attr or attr not in user_entry:
        return ""
    value = user_entry[attr].value
    return "" if value is None else str(value)


def _directory_username(cfg: LdapConfig, user_entry: Any, fallback: str) -> str:
    """The username form stored as ``ldap_uid`` and compared against ``admin_users``.

    Login and the directory sync both call this, so they always compare the same value:
    the entry's username-attribute value as the directory holds it.

    Args:
        cfg: Resolved LDAP configuration (``username_attr``).
        user_entry: The directory entry.
        fallback: Used when the entry has no value for the attribute.

    Returns:
        The attribute's value as a string, or ``fallback``.
    """
    attr = cfg.username_attr
    if attr and attr in user_entry:
        value = getattr(user_entry, attr, None)
        if value is not None:
            return str(value)
    return fallback


def _is_ldap_admin(
    cfg: LdapConfig,
    username: str,
    user_groups: list[str],
    bind_conn: Any = None,
    user_dn: str | None = None,
) -> bool:
    """Whether the directory marks this user as an admin.

    True when ``username`` is listed in ``admin_users`` (comma-separated, exact,
    case-insensitive) or the user belongs to an ``admin_groups`` group (directly, or
    nested when ``recursive_groups`` is on and a connection and DN are supplied). A blank
    or separator-only ``admin_groups`` grants admin to nobody.

    Args:
        cfg: Resolved LDAP configuration.
        username: The directory username form (see :func:`_directory_username`).
        user_groups: Groups read from the user's entry.
        bind_conn: Bound connection for the nested-group lookup.
        user_dn: The user's DN for the nested-group lookup.

    Returns:
        The admin signal.
    """
    candidate = username.strip().lower()
    listed = {name.strip().lower() for name in cfg.admin_users.split(",") if name.strip()}
    if candidate and candidate in listed:
        logger.debug("LDAP user %s is an admin through admin_users", username)
        return True

    admin_groups = _parse_group_list(cfg.admin_groups)
    if not admin_groups:
        return False
    if _is_member_of_groups(user_groups, admin_groups):
        logger.debug("LDAP user %s is an admin through a direct admin group", username)
        return True
    if (
        cfg.recursive_groups
        and bind_conn is not None
        and user_dn
        and _search_recursive_group_membership(bind_conn, user_dn, admin_groups)
    ):
        logger.debug("LDAP user %s is an admin through a nested admin group", username)
        return True
    return False


def _resolve_login_config(db: Session | None) -> LdapConfig:
    if db is None:
        return LdapConfig.from_env()
    try:
        return LdapConfig.from_db(db)
    except Exception as exc:
        logger.warning(
            "Could not read the LDAP settings from the database (%s); using the environment",
            type(exc).__name__,
        )
        return LdapConfig.from_env()


def ldap_authenticate(
    username: str, password: str, db: Session | None = None
) -> LdapUserData | None:
    """Verify a username and password against the directory.

    Order: refuse when disabled or given an empty name or password (an empty-password
    bind is anonymous on many servers); bind the service account; find exactly one
    entry; require a valid email and group entitlement; only then bind as the entry's
    DN with the password. Both connections are always closed.

    Args:
        username: Login name as typed: a bare name or an email address.
        password: The password to verify.
        db: Session for the admin-managed settings; without one the environment is used.

    Returns:
        The user's directory data, or ``None`` when the login is refused for any reason.
        Never raises.
    """
    cfg = _resolve_login_config(db)
    if not cfg.enabled:
        logger.warning("LDAP login attempted for %s while LDAP is disabled", username)
        return None
    if not username or not password:
        logger.warning("LDAP login refused: empty username or password")
        return None

    ldap_username = username.split("@", 1)[0]
    service_conn = None
    user_conn = None
    try:
        server = _get_ldap_server(cfg)
        service_conn = _bind_service_account(cfg, server)
        if service_conn is None:
            return None

        entry = _search_ldap_user(cfg, service_conn, username, ldap_username)
        if entry is None:
            logger.warning("LDAP user not found: %s", username)
            return None

        directory_name = _directory_username(cfg, entry, ldap_username)
        email = _entry_text(entry, cfg.email_attr)
        full_name = _entry_text(entry, cfg.name_attr)
        groups = _get_user_groups(cfg, entry)

        if not _is_valid_email(email):
            logger.warning(
                "LDAP login refused for %s: email attribute %r is missing or invalid",
                directory_name,
                email,
            )
            return None
        if not _check_group_access(cfg, service_conn, entry.entry_dn, groups, directory_name):
            return None

        user_conn = _connect_and_bind(cfg, server, entry.entry_dn, password)
        if user_conn is None:
            logger.warning("LDAP password check failed for %s", directory_name)
            return None

        logger.info("LDAP login succeeded for %s", directory_name)
        is_admin = _is_ldap_admin(
            cfg, directory_name, groups, bind_conn=service_conn, user_dn=entry.entry_dn
        )
        return LdapUserData(
            username=directory_name,
            email=email,
            full_name=full_name,
            is_admin=is_admin,
            groups=groups,
        )
    except LDAPException as exc:
        logger.error("LDAP login for %s failed: %s: %s", username, type(exc).__name__, exc)
        return None
    except Exception as exc:
        logger.error(
            "Unexpected error during LDAP login for %s: %s: %s", username, type(exc).__name__, exc
        )
        return None
    finally:
        _close_connection(user_conn, "user")
        _close_connection(service_conn, "service")


def _apply_directory_profile(
    user: User, username: str, email: str, ldap_data: LdapUserData
) -> None:
    """Copy the directory's email and name onto ``user`` without ever blanking them."""
    if email and email != user.email:
        logger.warning(
            "SECURITY: LDAP login changes the email of %s from %s to %s",
            username,
            user.email,
            email,
        )
        user.email = email
    if ldap_data["full_name"]:
        user.full_name = ldap_data["full_name"]


def _create_ldap_user(
    db: Session, username: str, email: str, ldap_data: LdapUserData, *, is_admin: bool
) -> User:
    """Create the account for a first directory login.

    Args:
        db: Database session.
        username: Directory username (stored as ``ldap_uid``).
        email: Directory email.
        ldap_data: The login's directory data.
        is_admin: Effective admin signal (directory OR group mapping).

    Returns:
        The new row, or the row a concurrent login created first.

    Raises:
        ValueError: The insert conflicted but no conflicting row can be found.
    """
    role = ROLE_ADMIN if is_admin else ROLE_USER
    user = User(
        email=email,
        full_name=ldap_data["full_name"] or email.split("@", 1)[0],
        hashed_password=EXTERNAL_AUTH_NO_PASSWORD,
        auth_type=AUTH_TYPE_LDAP,
        ldap_uid=username,
        role=role,
        is_active=True,
        is_superuser=role_implies_superuser(role),
        approval_status=initial_approval_status(db),
    )
    db.add(user)
    logger.info("Creating LDAP account %s with role %s", username, role)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.info("LDAP account %s was created by a concurrent login; reusing it", username)
        winner = (
            db.query(User).filter(User.ldap_uid == username).first()
            or db.query(User).filter(User.email == email).first()
        )
        if winner is None:
            raise ValueError(
                f"LDAP account {username} conflicted on insert, but no conflicting row exists"
            ) from None
        return winner
    return user


def _update_ldap_user(
    db: Session, user: User, username: str, email: str, ldap_data: LdapUserData
) -> User:
    """Refresh an existing directory-owned account from the directory.

    Role, superuser flag and sessions are left to group reconciliation.

    Args:
        db: Database session.
        user: The stored account.
        username: Directory username.
        email: Directory email (an empty value never clears the stored one).
        ldap_data: The login's directory data.

    Returns:
        The updated row.
    """
    _apply_directory_profile(user, username, email, ldap_data)
    user.ldap_uid = username
    user.auth_type = AUTH_TYPE_LDAP
    db.commit()
    return user


def _convert_local_user_to_ldap(
    db: Session, user: User, username: str, email: str, ldap_data: LdapUserData
) -> User:
    """Turn a local account already linked to this ``ldap_uid`` into a directory account.

    Its local password is replaced by the no-password marker. Role and superuser flag
    are left to group reconciliation.

    Args:
        db: Database session.
        user: The local account.
        username: Directory username.
        email: Directory email (an empty value never clears the stored one).
        ldap_data: The login's directory data.

    Returns:
        The converted row.
    """
    _apply_directory_profile(user, username, email, ldap_data)
    user.auth_type = AUTH_TYPE_LDAP
    user.ldap_uid = username
    user.hashed_password = EXTERNAL_AUTH_NO_PASSWORD
    db.commit()
    return user


def sync_ldap_user_to_db(db: Session, ldap_data: LdapUserData) -> User:
    """Create or refresh the ``User`` row for a successful directory login.

    The account is found by ``ldap_uid`` (guarded against a divergent email and against
    super_admin takeover), else by email, which LDAP may never link on its own: that
    refusal is indistinguishable from a wrong password. Privilege is resolved before
    any write, so a new account is created at its final role, and is then reconciled
    against the IdP group mappings.

    Args:
        db: Database session.
        ldap_data: Result of :func:`ldap_authenticate`.

    Returns:
        The refreshed row.

    Raises:
        AuthenticationError: The match may not be linked to this directory identity.
    """
    username = ldap_data["username"]
    email = ldap_data["email"]
    groups = ldap_data["groups"] or []

    user = db.query(User).filter(User.ldap_uid == username).first()
    if user is not None:
        assert_provider_id_link_permitted(
            user,
            provider=AUTH_TYPE_LDAP,
            source_identifier=username,
            asserted_email=email,
            failure_detail=_LOGIN_REFUSED,
            failure_headers=_BEARER_CHALLENGE,
        )
    else:
        user = db.query(User).filter(User.email == email).first()
        if user is not None:
            assert_email_link_permitted(
                user,
                provider=AUTH_TYPE_LDAP,
                source_identifier=username,
                email_verified=LDAP_ASSERTS_EMAIL_VERIFIED,
                failure_detail=_LOGIN_REFUSED,
                failure_headers=_BEARER_CHALLENGE,
            )

    grants = idp_group_mapping_service.resolve_grants(db, MAPPING_SOURCE_LDAP, groups)
    effective_admin = bool(ldap_data["is_admin"]) or bool(grants.grants_admin)

    if user is None:
        user = _create_ldap_user(db, username, email, ldap_data, is_admin=effective_admin)
    elif user.auth_type == AUTH_TYPE_LOCAL:
        logger.warning(
            "SECURITY: converting local account %s to LDAP; its local password is cleared",
            user.email,
        )
        user = _convert_local_user_to_ldap(db, user, username, email, ldap_data)
    else:
        user = _update_ldap_user(db, user, username, email, ldap_data)

    idp_group_mapping_service.reconcile_user(
        db,
        user,
        MAPPING_SOURCE_LDAP,
        groups,
        legacy_admin=bool(ldap_data["is_admin"]),
        reason="idp_login",
    )
    db.refresh(user)
    return user
