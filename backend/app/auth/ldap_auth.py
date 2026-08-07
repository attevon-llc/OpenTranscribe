"""
LDAP/Active Directory authentication module.

Handles authentication against LDAP/Active Directory servers.
Configuration is loaded from database first, falling back to environment variables.
"""

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TypedDict

from ldap3 import ALL
from ldap3 import AUTO_BIND_TLS_BEFORE_BIND
from ldap3 import Connection
from ldap3 import Server
from ldap3.core.exceptions import LDAPBindError
from ldap3.core.exceptions import LDAPException
from sqlalchemy.exc import IntegrityError

from app.auth.constants import AUTH_TYPE_LDAP
from app.auth.constants import AUTH_TYPE_LOCAL
from app.auth.constants import EXTERNAL_AUTH_NO_PASSWORD
from app.auth.roles import ELEVATED_ROLES
from app.auth.roles import ROLE_ADMIN
from app.auth.roles import ROLE_USER
from app.auth.roles import role_implies_superuser
from app.core.config import settings as env_settings

logger = logging.getLogger(__name__)

# Re-export for backwards compatibility
LDAP_NO_PASSWORD = EXTERNAL_AUTH_NO_PASSWORD


@dataclass(frozen=True)
class LdapConfig:
    """Immutable LDAP configuration resolved from database or environment.

    This is the single source of truth for LDAP settings during an
    authentication attempt. Created once per request, passed to all
    helper functions — no global state mutation.
    """

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
    def from_env(cls) -> "LdapConfig":
        """Create config from environment variables only."""
        return cls(
            enabled=env_settings.LDAP_ENABLED,
            server=env_settings.LDAP_SERVER,
            port=env_settings.LDAP_PORT,
            use_ssl=env_settings.LDAP_USE_SSL,
            use_tls=getattr(env_settings, "LDAP_USE_TLS", False),
            bind_dn=env_settings.LDAP_BIND_DN,
            bind_password=env_settings.LDAP_BIND_PASSWORD,
            search_base=env_settings.LDAP_SEARCH_BASE,
            username_attr=env_settings.LDAP_USERNAME_ATTR,
            email_attr=env_settings.LDAP_EMAIL_ATTR,
            name_attr=env_settings.LDAP_NAME_ATTR,
            group_attr=env_settings.LDAP_GROUP_ATTR,
            user_search_filter=env_settings.LDAP_USER_SEARCH_FILTER,
            admin_users=env_settings.LDAP_ADMIN_USERS,
            admin_groups=env_settings.LDAP_ADMIN_GROUPS,
            user_groups=env_settings.LDAP_USER_GROUPS,
            recursive_groups=env_settings.LDAP_RECURSIVE_GROUPS,
            timeout=env_settings.LDAP_TIMEOUT,
        )

    @classmethod
    def from_db(cls, db) -> "LdapConfig":
        """Create config from database with env fallback.

        Uses DynamicAuthSettings which checks DB > .env > defaults.
        """
        from app.core.auth_settings import get_auth_settings

        auth = get_auth_settings(db)

        def _get(key: str, default):
            """Get value from DB settings, falling back to default."""
            val = auth.get(key)
            return val if val is not None else default

        def _get_bool(key: str, default: bool) -> bool:
            val = auth.get(key)
            if val is None:
                return default
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                return val.lower() in ("true", "1", "yes", "on")
            return bool(val)

        def _get_int(key: str, default: int) -> int:
            val = auth.get(key)
            if val is None:
                return default
            try:
                return int(val)
            except (ValueError, TypeError):
                return default

        return cls(
            enabled=_get_bool("ldap_enabled", env_settings.LDAP_ENABLED),
            server=str(_get("ldap_server", env_settings.LDAP_SERVER) or ""),
            port=_get_int("ldap_port", env_settings.LDAP_PORT),
            use_ssl=_get_bool("ldap_use_ssl", env_settings.LDAP_USE_SSL),
            use_tls=_get_bool("ldap_use_tls", getattr(env_settings, "LDAP_USE_TLS", False)),
            bind_dn=str(_get("ldap_bind_dn", env_settings.LDAP_BIND_DN) or ""),
            bind_password=str(_get("ldap_bind_password", env_settings.LDAP_BIND_PASSWORD) or ""),
            search_base=str(_get("ldap_search_base", env_settings.LDAP_SEARCH_BASE) or ""),
            username_attr=str(_get("ldap_username_attr", env_settings.LDAP_USERNAME_ATTR) or "uid"),
            email_attr=str(_get("ldap_email_attr", env_settings.LDAP_EMAIL_ATTR) or "mail"),
            name_attr=str(_get("ldap_name_attr", env_settings.LDAP_NAME_ATTR) or "cn"),
            group_attr=str(_get("ldap_group_attr", env_settings.LDAP_GROUP_ATTR) or ""),
            user_search_filter=str(
                _get("ldap_user_search_filter", env_settings.LDAP_USER_SEARCH_FILTER)
                or "(uid={username})"
            ),
            admin_users=str(_get("ldap_admin_users", env_settings.LDAP_ADMIN_USERS) or ""),
            admin_groups=str(_get("ldap_admin_groups", env_settings.LDAP_ADMIN_GROUPS) or ""),
            user_groups=str(_get("ldap_user_groups", env_settings.LDAP_USER_GROUPS) or ""),
            recursive_groups=_get_bool("ldap_recursive_groups", env_settings.LDAP_RECURSIVE_GROUPS),
            timeout=_get_int("ldap_timeout", env_settings.LDAP_TIMEOUT),
        )


class LdapUserData(TypedDict):
    """Type definition for LDAP user data returned by ldap_authenticate."""

    username: str
    email: str
    full_name: str
    is_admin: bool
    groups: list[str]


def _escape_ldap_filter(value: str) -> str:
    """Escape special characters in LDAP filter values to prevent injection."""
    return (
        value.replace("\\", "\\5c")
        .replace("*", "\\2a")
        .replace("(", "\\28")
        .replace(")", "\\29")
        .replace("\x00", "\\00")
    )


def _is_valid_email(email: str) -> bool:
    """Validate email format."""
    if not email:
        return False
    pattern = r"^[^\s@]+@[^\s@]+\.[^\s@]+$"
    return bool(re.match(pattern, email))


def _get_ldap_server(cfg: LdapConfig) -> Server:
    """Create and return an LDAP server object."""
    return Server(
        cfg.server,
        port=cfg.port,
        use_ssl=cfg.use_ssl,
        get_info=ALL,
        connect_timeout=cfg.timeout,
    )


def _close_connection(conn: Connection | None, name: str) -> None:
    """Safely close an LDAP connection."""
    if conn is None:
        return
    try:
        if conn.bound:
            conn.unbind()
    except Exception:
        logger.debug(f"Error closing {name} connection (ignored)")


def _bind_service_account(cfg: LdapConfig, server: Server) -> Connection | None:
    """Bind to LDAP server using service account."""
    try:
        conn = Connection(
            server,
            user=cfg.bind_dn,
            password=cfg.bind_password,
            auto_bind=AUTO_BIND_TLS_BEFORE_BIND if cfg.use_ssl else True,
        )
        logger.debug("LDAP service account bind successful")
        return conn
    except LDAPBindError:
        logger.error(
            "Failed to bind to LDAP server with service account. "
            "Check LDAP bind DN and password configuration."
        )
        return None


def _search_ldap_user(
    cfg: LdapConfig,
    bind_conn: Connection,
    username: str,
    ldap_username: str,
    extra_attributes: list[str] | None = None,
):
    """Search for user in LDAP by username or email.

    Handles LDAP servers that don't support certain attributes (e.g., memberOf)
    by retrying without the unsupported attribute.

    ``extra_attributes`` are requested alongside the configured ones but kept OUT
    of ``base_attributes`` on purpose: the invalid-attribute retry below falls back
    to ``base_attributes``, so an optional attribute a given server doesn't know
    (``userAccountControl`` on OpenLDAP/LLDAP) degrades to "not returned" instead
    of failing the whole search.
    """
    base_attributes = [cfg.username_attr, cfg.email_attr, cfg.name_attr]

    # Only add group attribute if configured
    attributes = list(base_attributes)
    if cfg.group_attr:
        attributes.append(cfg.group_attr)
    if extra_attributes:
        attributes.extend(extra_attributes)

    # Search by username attribute first
    search_filter = cfg.user_search_filter.format(username=_escape_ldap_filter(ldap_username))

    def _do_search(search_filter: str, attrs: list[str]) -> bool:
        """Execute search, retrying without group attr if needed. Returns True if found."""
        try:
            bind_conn.search(
                search_base=cfg.search_base,
                search_filter=search_filter,
                attributes=attrs,
            )
            return bool(bind_conn.entries)
        except LDAPException as e:
            # Some LDAP servers (e.g., LLDAP) don't support memberOf
            if "memberOf" in str(e) or "invalid attribute" in str(e).lower():
                logger.info(f"LDAP server doesn't support group attribute, retrying without: {e}")
                bind_conn.search(
                    search_base=cfg.search_base,
                    search_filter=search_filter,
                    attributes=base_attributes,
                )
                return bool(bind_conn.entries)
            raise

    if _do_search(search_filter, attributes):
        return bind_conn.entries[0]

    # Fallback: search by email
    logger.debug(f"User not found by {cfg.username_attr}={ldap_username}, trying email search")
    email_filter = f"({cfg.email_attr}={_escape_ldap_filter(username)})"
    if _do_search(email_filter, attributes):
        return bind_conn.entries[0]

    return None


def _get_user_groups(cfg: LdapConfig, user_entry) -> list[str]:
    """Extract group DNs from LDAP user entry."""
    if not cfg.group_attr:
        return []
    if cfg.group_attr not in user_entry:
        return []

    group_value = user_entry[cfg.group_attr].value
    if group_value is None:
        return []

    if isinstance(group_value, list):
        return [str(g) for g in group_value]
    return [str(group_value)]


def _is_member_of_groups(user_groups: list[str], required_groups: list[str]) -> bool:
    """Check if user is a member of any of the required groups.

    Case-insensitive exact match. Both sides are stripped of whitespace.
    Configure groups using the exact DN string returned by your LDAP server.
    """
    if not required_groups:
        return True
    user_groups_lower = {g.lower().strip() for g in user_groups}
    return any(rg.lower().strip() in user_groups_lower for rg in required_groups)


def _parse_group_list(value: str) -> list[str]:
    """Parse a semicolon-delimited list of LDAP group DNs.

    LDAP/AD distinguished names contain commas as component separators
    (e.g. ``CN=Whisper_Users,CN=Users,DC=example,DC=com``), so commas cannot
    be used to delimit multiple groups. Semicolons are the standard delimiter here.

    Rules:
    - Multiple groups are separated by ``;``
    - Each group value should be the exact DN string your LDAP server returns
    - Whitespace around each entry is stripped

    Examples::

        # Single group (full DN)
        LDAP_USER_GROUPS=CN=Whisper_Users,CN=Users,DC=example,DC=com

        # Multiple groups
        LDAP_USER_GROUPS=CN=Whisper_Users,CN=Users,DC=example,DC=com;CN=OtherGroup,DC=example,DC=com
    """
    value = value.strip()
    if not value:
        return []
    return [g.strip() for g in value.split(";") if g.strip()]


def _get_required_user_groups(cfg: LdapConfig) -> list[str]:
    """Parse user_groups setting into a list of required group DNs."""
    if not cfg.user_groups:
        return []
    return _parse_group_list(cfg.user_groups)


def _search_recursive_group_membership(
    bind_conn: Connection, user_dn: str, group_dns: list[str]
) -> bool:
    """Check recursive group membership using LDAP_MATCHING_RULE_IN_CHAIN.

    Uses Active Directory OID 1.2.840.113556.1.4.1941 for nested groups.
    """
    matching_rule_in_chain = "1.2.840.113556.1.4.1941"

    for group_dn in group_dns:
        try:
            bind_conn.search(
                search_base=group_dn,
                search_filter="(objectClass=*)",
                search_scope="BASE",
                attributes=["distinguishedName"],
            )
            if bind_conn.entries:
                recursive_filter = (
                    f"(&(distinguishedName={_escape_ldap_filter(group_dn)})"
                    f"(member:{matching_rule_in_chain}:={_escape_ldap_filter(user_dn)}))"
                )
                bind_conn.search(
                    search_base=group_dn,
                    search_filter=recursive_filter,
                    search_scope="BASE",
                )
                if bind_conn.entries:
                    logger.debug(f"User {user_dn} is a recursive member of group {group_dn}")
                    return True
        except LDAPException as e:
            logger.debug(f"Error checking recursive group membership for {group_dn}: {e}")
            continue

    return False


def _check_group_access(
    cfg: LdapConfig,
    bind_conn: Connection,
    user_dn: str,
    user_groups: list[str],
    username: str,
) -> bool:
    """Check group-based access with optional recursive membership lookup.

    Returns True if no required groups configured, or user is a member of
    at least one required group.
    """
    required_groups = _get_required_user_groups(cfg)
    if not required_groups:
        return True

    # Check direct membership first
    if _is_member_of_groups(user_groups, required_groups):
        return True

    # Check recursive membership if enabled
    if cfg.recursive_groups:
        if _search_recursive_group_membership(bind_conn, user_dn, required_groups):
            return True
        logger.warning(
            f"User {username} denied access - not a member of required groups "
            "(recursive check enabled)"
        )
        return False

    logger.warning(
        f"User {username} denied access - not a member of any required groups. "
        f"User groups: {user_groups}, Required: {required_groups}"
    )
    return False


def _extract_user_attributes(cfg: LdapConfig, user_entry, ldap_username: str) -> dict | None:
    """Extract and validate user attributes from LDAP entry."""
    # Extract username
    attr_value = (
        getattr(user_entry, cfg.username_attr, None)
        if hasattr(user_entry, cfg.username_attr)
        else None
    )
    username_value = str(attr_value) if attr_value is not None else ldap_username

    # Extract email
    email_value = user_entry[cfg.email_attr].value if cfg.email_attr in user_entry else None
    user_email = str(email_value) if email_value is not None else ""

    # Extract full name
    name_value = user_entry[cfg.name_attr].value if cfg.name_attr in user_entry else None
    user_full_name = str(name_value) if name_value is not None else ""

    # Extract group memberships
    user_groups = _get_user_groups(cfg, user_entry)

    # Validate email
    if not _is_valid_email(user_email):
        logger.warning(
            f"User {username_value} has no valid email attribute in LDAP (got: {user_email!r})"
        )
        return None

    return {
        "username": username_value,
        "email": user_email,
        "full_name": user_full_name,
        "groups": user_groups,
    }


def _verify_user_credentials(
    cfg: LdapConfig, server: Server, user_dn: str, password: str
) -> Connection | None:
    """Verify user credentials by binding as the user."""
    try:
        conn = Connection(
            server,
            user=user_dn,
            password=password,
            auto_bind=AUTO_BIND_TLS_BEFORE_BIND if cfg.use_ssl else True,
        )
        return conn
    except LDAPBindError:
        return None


def _is_ldap_admin(
    cfg: LdapConfig,
    username: str,
    user_groups: list[str],
    bind_conn: Connection | None = None,
    user_dn: str | None = None,
) -> bool:
    """Check if user is an admin via admin_users or admin_groups config."""
    # Check admin_users list
    if cfg.admin_users:
        admin_users = cfg.admin_users.split(",")
        if username.strip().lower() in [u.strip().lower() for u in admin_users]:
            logger.debug(f"User {username} is admin via LDAP admin_users")
            return True

    # Check admin_groups
    if cfg.admin_groups:
        admin_groups = _parse_group_list(cfg.admin_groups)

        if _is_member_of_groups(user_groups, admin_groups):
            logger.debug(f"User {username} is admin via LDAP admin_groups (direct)")
            return True

        if (
            cfg.recursive_groups
            and bind_conn
            and user_dn
            and _search_recursive_group_membership(bind_conn, user_dn, admin_groups)
        ):
            logger.debug(f"User {username} is admin via LDAP admin_groups (recursive)")
            return True

    return False


def ldap_authenticate(username: str, password: str, db=None) -> LdapUserData | None:
    """Authenticate a user against LDAP/Active Directory.

    Loads configuration from database when db is provided, otherwise uses
    environment variables. Configuration is resolved once and passed through
    to all helper functions as an immutable LdapConfig dataclass.

    Args:
        username: The username or email to authenticate
        password: The user's password
        db: Optional database session for loading config from database

    Returns:
        LdapUserData dict if authentication succeeds, None otherwise
    """
    # Resolve configuration once (DB > .env > defaults)
    if db is not None:
        try:
            cfg = LdapConfig.from_db(db)
        except Exception as e:
            logger.warning(f"Failed to load LDAP config from database, using .env: {e}")
            cfg = LdapConfig.from_env()
    else:
        cfg = LdapConfig.from_env()

    if not cfg.enabled:
        logger.warning("LDAP authentication attempted but LDAP is not enabled")
        return None

    # Validate inputs early
    if not username or not password:
        logger.warning("LDAP authentication attempted with empty username or password")
        return None

    logger.debug(f"LDAP authenticate called for: {username}")
    ldap_username = username.split("@")[0] if "@" in username else username

    bind_conn: Connection | None = None
    user_conn: Connection | None = None

    try:
        server = _get_ldap_server(cfg)

        # Step 1: Bind with service account
        bind_conn = _bind_service_account(cfg, server)
        if not bind_conn:
            return None

        # Step 2: Search for user
        user_entry = _search_ldap_user(cfg, bind_conn, username, ldap_username)
        if not user_entry:
            logger.warning(f"User not found in LDAP: {username}")
            return None

        user_dn = user_entry.entry_dn
        logger.debug(f"Found user in LDAP: {user_dn}")

        # Step 3: Extract and validate attributes
        attrs = _extract_user_attributes(cfg, user_entry, ldap_username)
        if not attrs:
            return None

        user_groups = attrs.get("groups", [])
        logger.debug(f"User {attrs['username']} belongs to {len(user_groups)} groups")

        # Step 4: Check group-based access requirements
        if not _check_group_access(cfg, bind_conn, user_dn, user_groups, attrs["username"]):
            return None

        # Step 5: Verify credentials
        user_conn = _verify_user_credentials(cfg, server, user_dn, password)
        if not user_conn:
            logger.warning(f"LDAP password verification failed for user: {attrs['username']}")
            return None

        logger.info(f"LDAP authentication successful for user: {attrs['username']}")

        # Step 6: Determine admin status
        is_admin = _is_ldap_admin(
            cfg,
            attrs["username"],
            user_groups,
            bind_conn=bind_conn,
            user_dn=user_dn,
        )

        return LdapUserData(
            username=attrs["username"],
            email=attrs["email"],
            full_name=attrs["full_name"],
            is_admin=is_admin,
            groups=user_groups,
        )

    except LDAPException as e:
        logger.error(f"LDAP authentication error for {username}: {type(e).__name__}: {e}")
        return None
    except Exception as e:
        logger.error(
            f"Unexpected error during LDAP authentication for {username}: {type(e).__name__}: {e}"
        )
        return None
    finally:
        _close_connection(user_conn, "user")
        _close_connection(bind_conn, "service")


def _create_ldap_user(db, username: str, email: str, ldap_data: LdapUserData):
    """Create a new user from LDAP data."""
    from app.models.user import User

    logger.info(f"Creating new user from LDAP: {username} ({email})")
    # External IdPs grant at most 'admin'; super_admin is local-only.
    role = ROLE_ADMIN if ldap_data["is_admin"] else ROLE_USER
    user = User(
        email=email,
        full_name=ldap_data["full_name"] or email.split("@")[0],
        hashed_password=LDAP_NO_PASSWORD,
        auth_type=AUTH_TYPE_LDAP,
        ldap_uid=username,
        role=role,
        is_active=True,
        is_superuser=role_implies_superuser(role),
    )
    db.add(user)

    try:
        db.commit()
        return user
    except IntegrityError:
        db.rollback()
        logger.info(f"User {username} was created by concurrent request, fetching existing user")
        user = db.query(User).filter(User.ldap_uid == username).first()
        if not user:
            user = db.query(User).filter(User.email == email).first()
        if not user:
            raise ValueError(f"Failed to create or find LDAP user: {username}") from None
        return user


def _update_ldap_user(db, user, username: str, email: str, ldap_data: LdapUserData):
    """Update an existing LDAP user's data."""
    logger.info(f"Updating existing LDAP user: {username} ({email})")

    if email and email != user.email:
        logger.warning(
            f"SECURITY: User email changed during LDAP login. "
            f"ldap_uid={username}, old_email={user.email}, new_email={email}"
        )
    user.email = email
    user.full_name = ldap_data["full_name"] or user.full_name
    user.ldap_uid = username
    user.auth_type = AUTH_TYPE_LDAP

    if ldap_data["is_admin"]:
        # External IdPs grant at most 'admin'; never demote a local super_admin.
        if user.role not in ELEVATED_ROLES:
            logger.info(f"Promoting LDAP user {username} to admin")
            user.role = ROLE_ADMIN
        user.is_superuser = role_implies_superuser(user.role)
    elif user.role == ROLE_ADMIN:
        logger.info(
            f"Demoting LDAP user {username} from admin (removed from LDAP admin_users/admin_groups)"
        )
        user.role = ROLE_USER
        user.is_superuser = role_implies_superuser(user.role)

    db.commit()
    return user


def _convert_local_user_to_ldap(db, user, username: str, email: str, ldap_data: LdapUserData):
    """Convert an existing local user to LDAP authentication."""
    logger.info(f"Converting local user {user.email} to LDAP auth: {username}")

    user.auth_type = AUTH_TYPE_LDAP
    user.ldap_uid = username
    user.hashed_password = LDAP_NO_PASSWORD

    if email and email != user.email:
        logger.warning(
            f"SECURITY: User email changed during LDAP conversion. "
            f"ldap_uid={username}, old_email={user.email}, new_email={email}"
        )
    user.email = email
    user.full_name = ldap_data["full_name"] or user.full_name

    if ldap_data["is_admin"]:
        # External IdPs grant at most 'admin'; never demote a local super_admin.
        # Setting role='admin' with is_superuser=True here used to violate the
        # ck_user_superuser_matches_role CHECK constraint (v369) and 500 the commit
        # below, locking every LDAP-admin local user out of conversion.
        if user.role not in ELEVATED_ROLES:
            logger.info(f"Promoting converted LDAP user {username} to admin")
            user.role = ROLE_ADMIN
        user.is_superuser = role_implies_superuser(user.role)
    elif user.role == ROLE_ADMIN:
        logger.info(
            f"Demoting converted LDAP user {username} from admin "
            "(not in LDAP admin_users/admin_groups)"
        )
        user.role = ROLE_USER
        user.is_superuser = role_implies_superuser(user.role)

    db.commit()
    return user


def sync_ldap_user_to_db(db, ldap_data: LdapUserData):
    """Create or update a user in the database from LDAP data.

    Handles creating new users, updating existing LDAP users,
    converting local users to LDAP, and race conditions.
    """
    from app.models.user import User

    username = ldap_data["username"]
    email = ldap_data["email"]

    user = db.query(User).filter(User.ldap_uid == username).first()
    if not user:
        user = db.query(User).filter(User.email == email).first()

    if not user:
        user = _create_ldap_user(db, username, email, ldap_data)
    elif user.auth_type == AUTH_TYPE_LOCAL:
        logger.warning(
            f"SECURITY: Converting local user {email} to LDAP auth. "
            "User will now authenticate exclusively via LDAP. "
            "Local password will be cleared."
        )
        user = _convert_local_user_to_ldap(db, user, username, email, ldap_data)
    else:
        user = _update_ldap_user(db, user, username, email, ldap_data)

    db.refresh(user)
    return user


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
        raise LdapDirectoryUnavailableError(f"LDAP bind failed: {type(e).__name__}: {e}") from e
    except Exception as e:  # noqa: BLE001 - any failure here means "could not ask"
        raise LdapDirectoryUnavailableError(
            f"LDAP connection failed: {type(e).__name__}: {e}"
        ) from e

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


def probe_ldap_user(cfg: LdapConfig, bind_conn: Connection, ldap_uid: str, email: str = "") -> str:
    """Ask the directory about one account, without authenticating as it.

    Args:
        cfg: Resolved LDAP configuration.
        bind_conn: Connection from :func:`ldap_directory_session`.
        ldap_uid: The stored ``User.ldap_uid``.
        email: The stored email, used for the same fallback search login uses.

    Returns:
        One of ``DIRECTORY_PRESENT`` / ``DIRECTORY_ABSENT`` / ``DIRECTORY_DISABLED``
        / ``DIRECTORY_NOT_ENTITLED``.

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
        raise LdapDirectoryUnavailableError(f"LDAP search failed: {type(e).__name__}: {e}") from e

    if not user_entry:
        return DIRECTORY_ABSENT

    if _is_ad_account_disabled(user_entry):
        return DIRECTORY_DISABLED

    # Same entitlement rule login enforces, so the sweep can never disable an
    # account that would still be allowed to log in.
    try:
        entitled = _check_group_access(
            cfg, bind_conn, user_entry.entry_dn, _get_user_groups(cfg, user_entry), ldap_uid
        )
    except LDAPException as e:
        raise LdapDirectoryUnavailableError(
            f"LDAP group check failed: {type(e).__name__}: {e}"
        ) from e

    return DIRECTORY_PRESENT if entitled else DIRECTORY_NOT_ENTITLED
