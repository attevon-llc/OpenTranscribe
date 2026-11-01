"""LDAP directory access: connection, bind, user search and group-membership checks.

One path to the directory, shared by login (``auth/ldap_auth.py``: ``ldap_authenticate``)
and the read-only deprovisioning probes (``auth/ldap_probe.py``): the server object, the
encrypted-or-refused bind (StartTLS fails closed), the injection-escaped and
ambiguity-refusing user search, and the ``user_groups`` entitlement check with its
optional recursive (``LDAP_MATCHING_RULE_IN_CHAIN``) lookup.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ldap3 import ALL
from ldap3 import Connection
from ldap3 import Server
from ldap3.core.exceptions import LDAPBindError
from ldap3.core.exceptions import LDAPException

if TYPE_CHECKING:
    from app.auth.ldap_auth import LdapConfig

logger = logging.getLogger(__name__)


def _escape_ldap_filter(value: str) -> str:
    """Escape special characters in LDAP filter values to prevent injection."""
    return (
        value.replace("\\", "\\5c")
        .replace("*", "\\2a")
        .replace("(", "\\28")
        .replace(")", "\\29")
        .replace("\x00", "\\00")
    )


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


def _connect_and_bind(
    cfg: LdapConfig, server: Server, user: str, password: str
) -> Connection | None:
    """Open a connection, negotiate encryption per ``cfg``, and bind.

    - ``use_ssl=True``: ``server`` already wraps the socket in TLS (``ldaps://``,
      handled by :func:`_get_ldap_server`) — bind directly.
    - ``use_tls=True`` (StartTLS over a plaintext ``ldap://`` connection): negotiate
      StartTLS explicitly and FAIL CLOSED if negotiation does not succeed, rather
      than silently falling through to a cleartext bind. ``cfg.use_tls`` used to be
      read into ``LdapConfig`` and then never consulted by the bind code — every
      StartTLS deployment was binding in cleartext regardless of the setting.
    - Neither: plain cleartext bind, unchanged.

    Returns:
        A bound ``Connection``, or ``None`` if TLS negotiation or the bind failed.
    """
    conn = Connection(server, user=user, password=password, auto_bind=False)
    try:
        if cfg.use_tls and not cfg.use_ssl and (not conn.open() or not conn.start_tls()):
            logger.error(
                "LDAP StartTLS negotiation failed; refusing to fall back to a "
                "cleartext bind (ldap_use_tls is enabled)"
            )
            _close_connection(conn, "failed-starttls")
            return None
        if not conn.bind():
            return None
        return conn
    except LDAPBindError:
        return None


def _bind_service_account(cfg: LdapConfig, server: Server) -> Connection | None:
    """Bind to LDAP server using service account."""
    conn = _connect_and_bind(cfg, server, cfg.bind_dn, cfg.bind_password)
    if conn is None:
        logger.error(
            "Failed to bind to LDAP server with service account. "
            "Check LDAP bind DN and password configuration, or LDAP TLS negotiation."
        )
        return None
    logger.debug("LDAP service account bind successful")
    return conn


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

    def _single_entry(search_filter: str, label: str):
        """Return the one matching entry, or None — never an arbitrary pick.

        A search filter that is too loose (or genuine directory duplicates) can
        return more than one entry. Silently binding as ``entries[0]`` in that case
        would authenticate the caller as whichever account the directory happened
        to list first — an authentication-ambiguity bug, and a deliberate one if an
        attacker with any directory write access can create a colliding entry. Fail
        closed instead: an ambiguous search is treated as "not found".
        """
        entries = bind_conn.entries
        if len(entries) > 1:
            logger.error(
                f"LDAP search by {label} ({search_filter!r}) matched {len(entries)} entries; "
                "refusing to authenticate against an ambiguous match"
            )
            return None
        return entries[0]

    if _do_search(search_filter, attributes):
        entry = _single_entry(search_filter, cfg.username_attr)
        if entry is not None:
            return entry
        return None

    # Fallback: search by email
    logger.debug(f"User not found by {cfg.username_attr}={ldap_username}, trying email search")
    email_filter = f"({cfg.email_attr}={_escape_ldap_filter(username)})"
    if _do_search(email_filter, attributes):
        return _single_entry(email_filter, cfg.email_attr)

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
