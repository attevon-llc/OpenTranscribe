"""
LDAP/Active Directory authentication module.

Handles authentication against LDAP/Active Directory servers.
Configuration is loaded from database first, falling back to environment variables.
The directory connection, user search and group checks live in ``auth/ldap_directory.py``;
the read-only deprovisioning probes in ``auth/ldap_probe.py``.
"""

import logging
import re
from dataclasses import dataclass
from typing import TypedDict

from ldap3 import Connection
from ldap3 import Server
from ldap3.core.exceptions import LDAPException
from sqlalchemy.exc import IntegrityError

from app.auth.account_linking import assert_email_link_permitted
from app.auth.account_linking import assert_provider_id_link_permitted
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
from app.core.auth_types import AUTH_TYPE_LDAP
from app.core.auth_types import AUTH_TYPE_LOCAL
from app.core.config import resolve_ldap_search_filter
from app.core.config import settings as env_settings
from app.core.env import is_truthy

logger = logging.getLogger(__name__)

# Re-export for backwards compatibility
LDAP_NO_PASSWORD = EXTERNAL_AUTH_NO_PASSWORD

#: Whether an LDAP directory asserts that a user's ``mail`` value is a *verified*
#: address. It does not, and nothing in LDAP or AD can: ``mail`` is an ordinary
#: writable attribute with no verification semantics, and in a self-service directory
#: the user owns it. The "source asserts the address verified" guard therefore fails
#: closed for LDAP, so email-match linking is always refused. Accounts already carrying
#: an ``ldap_uid`` are unaffected, and so is a new LDAP user whose address collides
#: with no existing account.
LDAP_ASSERTS_EMAIL_VERIFIED = False


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
                return is_truthy(val)
            return bool(val)

        def _get_int(key: str, default: int) -> int:
            val = auth.get(key)
            if val is None:
                return default
            try:
                return int(val)
            except (ValueError, TypeError):
                return default

        username_attr = str(_get("ldap_username_attr", env_settings.LDAP_USERNAME_ATTR) or "uid")
        # Resolve the `{username_attr}` placeholder the same way the .env-backed
        # Settings validator does (app/core/config.py). Without this, a DB-configured
        # search filter containing the literal placeholder never matches any real
        # LDAP attribute name and every DB-configured LDAP login fails to find the
        # user (or matches nothing/everything, depending on the server's parsing).
        user_search_filter = resolve_ldap_search_filter(
            str(
                _get("ldap_user_search_filter", env_settings.LDAP_USER_SEARCH_FILTER)
                or "(uid={username})"
            ),
            username_attr,
        )

        return cls(
            enabled=_get_bool("ldap_enabled", env_settings.LDAP_ENABLED),
            server=str(_get("ldap_server", env_settings.LDAP_SERVER) or ""),
            port=_get_int("ldap_port", env_settings.LDAP_PORT),
            use_ssl=_get_bool("ldap_use_ssl", env_settings.LDAP_USE_SSL),
            use_tls=_get_bool("ldap_use_tls", getattr(env_settings, "LDAP_USE_TLS", False)),
            bind_dn=str(_get("ldap_bind_dn", env_settings.LDAP_BIND_DN) or ""),
            bind_password=str(_get("ldap_bind_password", env_settings.LDAP_BIND_PASSWORD) or ""),
            search_base=str(_get("ldap_search_base", env_settings.LDAP_SEARCH_BASE) or ""),
            username_attr=username_attr,
            email_attr=str(_get("ldap_email_attr", env_settings.LDAP_EMAIL_ATTR) or "mail"),
            name_attr=str(_get("ldap_name_attr", env_settings.LDAP_NAME_ATTR) or "cn"),
            group_attr=str(_get("ldap_group_attr", env_settings.LDAP_GROUP_ATTR) or ""),
            user_search_filter=user_search_filter,
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


def _is_valid_email(email: str) -> bool:
    """Validate email format."""
    if not email:
        return False
    pattern = r"^[^\s@]+@[^\s@]+\.[^\s@]+$"
    return bool(re.match(pattern, email))


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
    return _connect_and_bind(cfg, server, user_dn, password)


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
    # A value of only separators/whitespace parses to no groups; an empty list
    # means "no requirement" to the membership helper, so it must not grant admin.
    admin_groups = _parse_group_list(cfg.admin_groups) if cfg.admin_groups else []
    if admin_groups:
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


def _create_ldap_user(db, username: str, email: str, ldap_data: LdapUserData, *, is_admin: bool):
    """Create a new user from LDAP data.

    Args:
        is_admin: The *effective* admin signal — the legacy ``admin_users`` /
            ``admin_groups`` rule OR-ed with any ``group_mapping`` that grants
            ``admin``. Computed by :func:`sync_ldap_user_to_db`.
    """
    from app.auth.approval import initial_approval_status
    from app.models.user import User

    logger.info(f"Creating new user from LDAP: {username} ({email})")
    # External IdPs grant at most 'admin'; super_admin is local-only.
    role = ROLE_ADMIN if is_admin else ROLE_USER
    user = User(
        email=email,
        full_name=ldap_data["full_name"] or email.split("@")[0],
        hashed_password=LDAP_NO_PASSWORD,
        auth_type=AUTH_TYPE_LDAP,
        ldap_uid=username,
        role=role,
        is_active=True,
        is_superuser=role_implies_superuser(role),
        # Same rule as OIDC JIT: the directory authenticated them, it did not
        # decide this deployment wants them. 'approved' unless the setting is on.
        approval_status=initial_approval_status(db),
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
    """Update an existing LDAP user's profile fields.

    Privilege is deliberately NOT decided here. Promotion and demotion used to be
    copy-pasted into this function, :func:`_convert_local_user_to_ldap` and both
    OIDC equivalents, each with its own super_admin guard and none of them
    revoking sessions. It now lives in
    ``services/idp_group_mapping_service.reconcile_user``, which
    :func:`sync_ldap_user_to_db` calls for every login.
    """
    logger.info(f"Updating existing LDAP user: {username} ({email})")

    # Conditional, matching oidc/provisioning.py, saml/provisioning.py and
    # external_sync.py: an unconditional write is a footgun the moment the upstream
    # _is_valid_email gate changes (#910). Latent today — that gate refuses a
    # blank/malformed mail, and assert_provider_id_link_permitted above already
    # refuses a disagreeing one — so what this actually changes is that a directory
    # re-casing an address no longer rewrites the stored column.
    if email and email != user.email:
        logger.warning(
            f"SECURITY: User email changed during LDAP login. "
            f"ldap_uid={username}, old_email={user.email}, new_email={email}"
        )
        user.email = email
    user.full_name = ldap_data["full_name"] or user.full_name
    user.ldap_uid = username
    user.auth_type = AUTH_TYPE_LDAP

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

    # Privilege is applied by reconcile_user after this returns — see
    # _update_ldap_user's docstring. (Setting role='admin' with is_superuser=True
    # here used to violate ck_user_superuser_matches_role (v369) and 500 the
    # commit, locking every LDAP-admin local user out of conversion.)
    db.commit()
    return user


def sync_ldap_user_to_db(db, ldap_data: LdapUserData):
    """Create or update a user in the database from LDAP data.

    Handles creating new users, updating existing LDAP users, converting local
    users to LDAP, and race conditions — and then reconciles the account's group
    memberships and privilege against the configured ``group_mapping`` rows
    (``v378``). Before that, ``ldap_data["groups"]`` was collected on every login
    and thrown away: ``is_admin`` was the only bit that survived, so a directory
    group could never become an OpenTranscribe sharing group.

    With no mappings configured this behaves exactly as it did before: the grant
    set is empty, no membership changes, and the legacy ``admin_users`` /
    ``admin_groups`` rule alone decides ``admin``.

    Lookup is ``ldap_uid`` first, then email. The email fallback links a directory
    identity to a **pre-existing** account, so it goes through
    ``account_linking.assert_email_link_permitted`` — see that module for the rule,
    why a refusal fails the login instead of creating a second account, and the
    operator remedy.

    Raises:
        AuthenticationError: 401, when an email-matched link is refused. Deliberately the
            same response ``_authenticate_ldap_user`` gives for a bad password, so a
            refusal is not an oracle for "this address exists".
    """
    from app.models.group import MAPPING_SOURCE_LDAP
    from app.models.user import User
    from app.services.idp_group_mapping_service import reconcile_user
    from app.services.idp_group_mapping_service import resolve_grants

    username = ldap_data["username"]
    email = ldap_data["email"]
    groups = ldap_data.get("groups") or []

    user = db.query(User).filter(User.ldap_uid == username).first()
    if user:
        # A stored ldap_uid is not necessarily a deliberate admin link — JIT
        # provisioning stamps it on ordinary first logins too, so a stale or
        # reassigned uid still needs the corroboration/super_admin guard.
        assert_provider_id_link_permitted(
            user,
            provider=AUTH_TYPE_LDAP,
            source_identifier=username,
            asserted_email=email,
            failure_detail="Incorrect username or password",
            failure_headers={"WWW-Authenticate": "Bearer"},
        )
    if not user:
        # The email fallback links a directory identity to a PRE-EXISTING account,
        # so it is gated too.
        user = db.query(User).filter(User.email == email).first()
        if user:
            assert_email_link_permitted(
                user,
                provider=AUTH_TYPE_LDAP,
                source_identifier=username,
                email_verified=LDAP_ASSERTS_EMAIL_VERIFIED,
                failure_detail="Incorrect username or password",
                failure_headers={"WWW-Authenticate": "Bearer"},
            )

    # Resolved before the row is written so a brand-new account is created at the
    # right role instead of being created and then immediately promoted.
    grants = resolve_grants(db, MAPPING_SOURCE_LDAP, groups)
    is_admin = bool(ldap_data["is_admin"]) or grants.grants_admin

    if not user:
        user = _create_ldap_user(db, username, email, ldap_data, is_admin=is_admin)
    elif user.auth_type == AUTH_TYPE_LOCAL:
        logger.warning(
            f"SECURITY: Converting local user {email} to LDAP auth. "
            "User will now authenticate exclusively via LDAP. "
            "Local password will be cleared."
        )
        user = _convert_local_user_to_ldap(db, user, username, email, ldap_data)
    else:
        user = _update_ldap_user(db, user, username, email, ldap_data)

    reconcile_user(
        db,
        user,
        MAPPING_SOURCE_LDAP,
        groups,
        legacy_admin=bool(ldap_data["is_admin"]),
        reason="idp_login",
    )

    db.refresh(user)
    return user
