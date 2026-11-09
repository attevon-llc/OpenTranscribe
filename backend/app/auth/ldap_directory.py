"""Directory access for LDAP / Active Directory: connect, bind, search, groups.

Everything that talks to the directory through ldap3 lives here, so the login
(``app.auth.ldap_auth``) and the read-only probes (``app.auth.ldap_probe``) share
one bind path and one set of group rules. ldap3's ``Server`` and ``Connection``
are module globals looked up at call time: tests replace exactly those two names.

ldap3 references used: ``Server(host, port=, use_ssl=, get_info=, connect_timeout=, tls=)``,
``ldap3.Tls(validate=, ca_certs_file=)`` and its ``wrap_socket`` (``ldap3/core/tls.py``),
``Connection(server, user=, password=, auto_bind=, receive_timeout=, auto_referrals=)``,
``Connection.open`` / ``start_tls`` / ``bind`` / ``search`` / ``unbind`` and
``ldap3.utils.conv.escape_filter_chars`` (RFC 4515), per the ldap3 2.9 documentation.
"""

from __future__ import annotations

import logging
import ssl
from typing import TYPE_CHECKING
from typing import Any

from ldap3 import ALL
from ldap3 import BASE
from ldap3 import Connection
from ldap3 import Server
from ldap3 import Tls
from ldap3.abstract.entry import Entry
from ldap3.core.exceptions import LDAPBindError
from ldap3.core.exceptions import LDAPException
from ldap3.core.exceptions import LDAPSocketReceiveError
from ldap3.core.exceptions import LDAPSSLConfigurationError
from ldap3.utils.conv import escape_filter_chars

if TYPE_CHECKING:
    from app.auth.ldap_auth import LdapConfig

logger = logging.getLogger(__name__)

#: Active Directory's LDAP_MATCHING_RULE_IN_CHAIN: matches nested group membership.
IN_CHAIN_MATCHING_RULE = "1.2.840.113556.1.4.1941"


def _escape_ldap_filter(value: str) -> str:
    """Escape a value for use inside an LDAP filter assertion (RFC 4515).

    Backslash, ``*``, ``(``, ``)`` and NUL become ``\\5c``, ``\\2a``, ``\\28``,
    ``\\29`` and ``\\00``; nothing else changes.

    Args:
        value: Untrusted text, for example what a user typed as a login name.

    Returns:
        The escaped text, safe to splice into a filter.
    """
    return str(escape_filter_chars(value))


class LdapCaFileError(LDAPSSLConfigurationError):
    """The configured CA bundle cannot be used, so no TLS connection may be made."""


class _HandshakeVerifiedTls(Tls):
    """An ldap3 ``Tls`` that checks the directory's identity inside the TLS handshake.

    ``Tls.wrap_socket`` (``ldap3/core/tls.py``) builds its ``SSLContext`` with
    ``check_hostname = False`` and, after the handshake, compares the certificate with the
    server host name through ``check_hostname()``. On Python 3.12, which removed
    ``ssl.match_hostname``, that comparison is ldap3's own backport
    (``ldap3/utils/tls_backport.py``), which matches DNS names only and so rejects every IP
    address, even one the certificate lists. ``Tls`` has no parameter for a ready-made
    context, so ``wrap_socket`` is replaced by the pattern the Python ``ssl`` documentation
    gives for clients: ``ssl.create_default_context`` (``CERT_REQUIRED`` and
    ``check_hostname``) and ``server_hostname`` on ``SSLContext.wrap_socket``. OpenSSL then
    matches an IP literal against the certificate's IP subjectAltNames and a name against its
    DNS ones. ldap3 calls ``wrap_socket`` itself for LDAPS (``BaseStrategy.open``) and for
    StartTLS (``Tls._start_tls``), so both transports share this check.

    Attributes:
        server_name: The name the certificate must carry, or ``""`` for the address being
            connected to (``connection.server.host``).
    """

    def __init__(self, *, server_name: str = "", ca_certs_file: str | None = None) -> None:
        super().__init__(validate=ssl.CERT_REQUIRED, ca_certs_file=ca_certs_file)
        self.server_name = server_name

    def wrap_socket(self, connection: Any, do_handshake: bool = False) -> None:
        """Wrap ``connection.socket`` in TLS, refusing a certificate not valid for the identity.

        Args:
            connection: The ldap3 ``Connection`` whose open socket is upgraded.
            do_handshake: Passed to ``do_handshake_on_connect``. The identity check runs in
                the handshake, so it happens whenever the handshake does.

        Raises:
            ssl.SSLError: The chain does not lead to a trusted CA, or the certificate is not
                valid for the identity (``ssl.SSLCertVerificationError``).
        """
        context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=self.ca_certs_file)
        connection.socket = context.wrap_socket(
            connection.socket,
            server_side=False,
            do_handshake_on_connect=do_handshake,
            server_hostname=self.server_name or connection.server.host,
        )


def _build_tls(cfg: LdapConfig) -> Tls:
    """Describe how the directory's certificate is checked.

    ldap3's ``Tls`` defaults to ``validate=ssl.CERT_NONE`` (``ldap3/core/tls.py``,
    ``Tls.__init__``) and a ``Server`` without one gets exactly that for both LDAPS and
    StartTLS (``Connection.start_tls`` builds a bare ``Tls()`` when the server has none),
    so a ``Tls`` is always passed. Unless verification is off, it is a
    :class:`_HandshakeVerifiedTls`: ``CERT_REQUIRED`` against the system CAs (or only
    ``ca_cert_file``) and the identity ``tls_server_name`` or, by default, the address in
    ``cfg.server``, whether that is a host name or an IP address.

    Args:
        cfg: Resolved LDAP configuration (``ca_cert_file``, ``tls_server_name``,
            ``tls_skip_verify``).

    Returns:
        The ``Tls`` object for the ``Server``.

    Raises:
        LdapCaFileError: ``ca_cert_file`` is set but missing, unreadable or not a PEM
            bundle. Verification is never silently dropped.
    """
    if cfg.tls_skip_verify:
        return Tls(validate=ssl.CERT_NONE)
    ca_file = cfg.ca_cert_file.strip()
    if ca_file:
        try:
            ssl.create_default_context(cafile=ca_file)
        except (OSError, ssl.SSLError) as exc:
            logger.error("The LDAP CA certificate file %r cannot be used: %s", ca_file, exc)
            raise LdapCaFileError(
                "The configured LDAP CA certificate file is missing, unreadable or not a PEM bundle"
            ) from exc
    return _HandshakeVerifiedTls(
        server_name=cfg.tls_server_name.strip(), ca_certs_file=ca_file or None
    )


def _get_ldap_server(cfg: LdapConfig) -> Server:
    """Build the ldap3 server object for one login or probe.

    Args:
        cfg: Resolved LDAP configuration.

    Returns:
        An ldap3 ``Server``. Construction opens no socket. Its ``ssl`` attribute is the
        effective transport: a URL scheme in ``cfg.server`` overrides ``use_ssl``. Its
        ``tls`` verifies the directory's certificate unless ``cfg.tls_skip_verify``.

    Raises:
        LdapCaFileError: ``cfg.ca_cert_file`` is set but unusable.
    """
    return Server(
        cfg.server,
        port=cfg.port,
        use_ssl=cfg.use_ssl,
        get_info=ALL,
        connect_timeout=cfg.timeout,
        tls=_build_tls(cfg),
    )


CLEARTEXT_REFUSAL = (
    "ldap:// URL without StartTLS would send credentials in cleartext; "
    "use ldaps:// or enable StartTLS"
)


def transport_problem(cfg: LdapConfig, server: Server) -> str | None:
    """Describe why binding over ``server`` is unacceptable, or ``None`` if it is fine.

    Transport security is decided from the EFFECTIVE server (``server.ssl``), not from
    ``cfg.use_ssl``: ldap3 lets the URL scheme override the ``use_ssl`` argument, so an
    ``ldap://`` address with ``use_ssl=True`` yields a plaintext server.
    """
    if server.ssl or cfg.use_tls:
        return None
    if cfg.use_ssl:
        return (
            f"ldap_use_ssl is enabled but the server address is not ldaps://. {CLEARTEXT_REFUSAL}"
        )
    return CLEARTEXT_REFUSAL


def _close_connection(conn: Connection | None, name: str) -> None:
    """Close ``conn`` whatever its state; never raise.

    ldap3's ``unbind()`` is what closes the socket (``Connection.unbind`` sends the
    unbind request and calls the strategy's ``close``), and it is a no-op on a connection
    that is already closed, so it is sent even when the bind never succeeded.

    Args:
        conn: The connection, or ``None`` when nothing was opened.
        name: Role label used in the debug log (``"user"``, ``"service"``, ...).
    """
    if conn is None:
        return
    try:
        conn.unbind()
    except (LDAPException, OSError) as exc:
        logger.debug(
            "Ignored an error while closing the %s LDAP connection: %s", name, exc, exc_info=True
        )


def _connect_and_bind(
    cfg: LdapConfig, server: Server, user: str, password: str
) -> Connection | None:
    """Open a connection and bind as ``user``, refusing any insecure transport.

    The transport gate (:func:`transport_problem`) runs first. With TLS already on the
    socket (``server.ssl``) the bind is sent directly; otherwise StartTLS must be
    negotiated before the bind, and a failed negotiation refuses the bind outright.
    An empty ``user`` and ``password`` attempt an anonymous bind.

    Args:
        cfg: Resolved LDAP configuration (``use_tls`` and ``timeout`` are read).
        server: The server object; only its ``ssl`` attribute is consulted.
        user: Bind DN.
        password: Bind password.

    Returns:
        The bound connection, or ``None`` when the bind was refused for any reason.

    Raises:
        LDAPException: Any ldap3 error other than a bind rejection or a read timeout,
            including a socket that cannot be opened.
    """
    problem = transport_problem(cfg, server)
    if problem is not None:
        logger.error("LDAP bind refused before connecting: %s", problem)
        return None

    if cfg.tls_skip_verify:
        logger.warning(
            "LDAP TLS certificate verification is DISABLED (ldap_tls_skip_verify): the "
            "directory server's identity is not checked on this connection"
        )

    # Referrals are not followed: ldap3 would open a second connection to a host named in
    # the reply, and a StartTLS referral target is upgraded with a default, non-verifying
    # Tls (ldap3/strategy/base.py create_referral_connection -> Connection.start_tls).
    conn = Connection(
        server,
        user=user,
        password=password,
        auto_bind=False,
        receive_timeout=cfg.timeout,
        auto_referrals=False,
    )
    bound = False
    try:
        if not server.ssl:
            # The gate guarantees use_tls here: upgrade before any credential is sent.
            # ldap3's open() returns None on success and raises on failure
            # (ldap3/strategy/base.py BaseStrategy.open), so its result is not a verdict.
            conn.open()
            if not conn.start_tls():
                logger.error(
                    "StartTLS negotiation with the LDAP server failed; bind refused "
                    "(no fallback to a cleartext bind is attempted)"
                )
                return None
        bound = bool(conn.bind())
    except LDAPSocketReceiveError:
        logger.error("The LDAP server did not answer within %s s; bind refused", cfg.timeout)
    except LDAPBindError:
        pass
    finally:
        if not bound:
            _close_connection(conn, "refused")
    return conn if bound else None


def _bind_service_account(cfg: LdapConfig, server: Server) -> Connection | None:
    """Bind the configured read-only service account.

    Args:
        cfg: Resolved LDAP configuration.
        server: Server object from :func:`_get_ldap_server`.

    Returns:
        The bound service connection, or ``None`` when the bind was refused.
    """
    conn = _connect_and_bind(cfg, server, cfg.bind_dn, cfg.bind_password)
    if conn is None:
        logger.error(
            "LDAP service account bind was refused: check the bind DN, its password and "
            "the TLS settings"
        )
        return None
    logger.debug("LDAP service account bound as %s", cfg.bind_dn)
    return conn


def _run_user_search(
    cfg: LdapConfig,
    bind_conn: Connection,
    search_filter: str,
    attributes: list[str],
    base_attributes: list[str],
) -> list[Any]:
    """Run one user search and return every entry it produced.

    A server that rejects an optional attribute (typically ``memberOf``) is asked again
    with the base attributes only.
    """
    try:
        bind_conn.search(
            search_base=cfg.search_base, search_filter=search_filter, attributes=attributes
        )
    except LDAPException as exc:
        text = str(exc)
        if "memberOf" not in text and "invalid attribute" not in text.lower():
            raise
        logger.info(
            "LDAP server rejected an optional attribute (%s); searching again without it", exc
        )
        bind_conn.search(
            search_base=cfg.search_base,
            search_filter=search_filter,
            attributes=base_attributes,
        )
    return list(bind_conn.entries)


def _search_ldap_user(
    cfg: LdapConfig,
    bind_conn: Connection,
    username: str,
    ldap_username: str,
    extra_attributes: list[str] | None = None,
) -> Entry | None:
    """Find exactly one directory entry for a login name.

    The configured filter is tried with ``ldap_username``; only when it finds nothing is
    the full ``username`` tried against the email attribute. More than one match on
    either search is refused rather than resolved.

    Args:
        cfg: Resolved LDAP configuration.
        bind_conn: A bound (service-account) connection.
        username: The name exactly as typed.
        ldap_username: The typed name up to its first ``@``.
        extra_attributes: Further attributes to request, after the standard ones.

    Returns:
        The single matching entry, or ``None`` when there is none or the match is
        ambiguous.

    Raises:
        LDAPException: A search failure other than a rejected optional attribute.
    """
    base_attributes = [cfg.username_attr, cfg.email_attr, cfg.name_attr]
    attributes = list(base_attributes)
    if cfg.group_attr:
        attributes.append(cfg.group_attr)
    attributes.extend(extra_attributes or [])

    filters = (
        cfg.user_search_filter.format(username=_escape_ldap_filter(ldap_username)),
        f"({cfg.email_attr}={_escape_ldap_filter(username)})",
    )
    for search_filter in filters:
        entries = _run_user_search(cfg, bind_conn, search_filter, attributes, base_attributes)
        if len(entries) > 1:
            logger.error(
                "LDAP filter %s matched %d entries; refusing an ambiguous login",
                search_filter,
                len(entries),
            )
            return None
        if entries:
            found: Entry = entries[0]
            return found
    return None


def _get_user_groups(cfg: LdapConfig, user_entry: Any) -> list[str]:
    """Read the entry's group attribute as a list of strings.

    Args:
        cfg: Resolved LDAP configuration (``group_attr``).
        user_entry: The directory entry.

    Returns:
        The group values; empty when the attribute is unset, absent or has no value.
    """
    attr = cfg.group_attr
    if not attr or attr not in user_entry:
        return []
    value = user_entry[attr].value
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return [str(value)]


def _is_member_of_groups(user_groups: list[str], required_groups: list[str]) -> bool:
    """Whether any required group is one of the user's groups.

    Comparison is the whole DN, trimmed and case-folded; no DN normalisation.

    Args:
        user_groups: Groups read from the user's entry.
        required_groups: Groups to look for. Empty means there is no requirement.

    Returns:
        ``True`` for an empty requirement or any match, else ``False``.
    """
    if not required_groups:
        return True
    held = {group.strip().lower() for group in user_groups}
    return any(group.strip().lower() in held for group in required_groups)


def _parse_group_list(value: str) -> list[str]:
    """Split a semicolon-separated list of group DNs.

    Semicolons, not commas, delimit the list because every DN contains commas.

    Args:
        value: The configured list.

    Returns:
        The non-empty, trimmed entries (empty for a blank value).
    """
    if not value or not value.strip():
        return []
    return [part.strip() for part in value.split(";") if part.strip()]


def _search_recursive_group_membership(
    bind_conn: Connection, user_dn: str, group_dns: list[str]
) -> bool:
    """Ask the directory whether ``user_dn`` is a nested member of any group.

    Uses Active Directory's in-chain matching rule; other directories never match.
    A directory error on one group means "not a member of that one".

    Args:
        bind_conn: A bound connection.
        user_dn: The user's DN.
        group_dns: Candidate group DNs, checked in order.

    Returns:
        ``True`` as soon as one group reports the user as a (nested) member.
    """
    escaped_user = _escape_ldap_filter(user_dn)
    for group_dn in group_dns:
        try:
            bind_conn.search(
                search_base=group_dn,
                search_filter="(objectClass=*)",
                search_scope=BASE,
                attributes=["distinguishedName"],
            )
            if not bind_conn.entries:
                continue
            chain_filter = (
                f"(&(distinguishedName={_escape_ldap_filter(group_dn)})"
                f"(member:{IN_CHAIN_MATCHING_RULE}:={escaped_user}))"
            )
            bind_conn.search(search_base=group_dn, search_filter=chain_filter, search_scope=BASE)
            if bind_conn.entries:
                logger.debug("%s is a nested member of %s", user_dn, group_dn)
                return True
        except LDAPException as exc:
            logger.debug("Nested-group lookup of %s failed (%s); not a member", group_dn, exc)
    return False


def _check_group_access(
    cfg: LdapConfig,
    bind_conn: Connection,
    user_dn: str,
    user_groups: list[str],
    username: str,
) -> bool:
    """Apply the ``user_groups`` login requirement.

    Args:
        cfg: Resolved LDAP configuration.
        bind_conn: A bound connection, used only for the recursive lookup.
        user_dn: The user's DN.
        user_groups: Groups read from the user's entry.
        username: Name used in log lines.

    Returns:
        ``True`` when no group is required or the user belongs to one of them.
    """
    required = _parse_group_list(cfg.user_groups)
    if not required:
        return True
    if _is_member_of_groups(user_groups, required):
        return True
    if cfg.recursive_groups:
        if _search_recursive_group_membership(bind_conn, user_dn, required):
            return True
        logger.warning(
            "LDAP user %s belongs to none of the required groups, directly or nested", username
        )
        return False
    logger.warning(
        "LDAP user %s belongs to none of the required groups (has %s; requires %s)",
        username,
        user_groups,
        required,
    )
    return False
