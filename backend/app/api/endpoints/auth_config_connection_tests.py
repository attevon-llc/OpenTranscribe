"""Connection testing for the authentication-configuration API.

``POST /{category}/test`` validates an LDAP or OIDC configuration without saving
it. Split from ``auth_config.py`` by concern: the config CRUD/audit handlers
there never touch the network, while everything here dials an admin-supplied
host (``_test_oidc_connection`` fetches an admin-supplied URL, so it runs the
outbound-URL safety check). The super_admin gate and the route's position in
the registration order are unchanged.
"""

import logging
from dataclasses import replace
from typing import TYPE_CHECKING
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_active_superuser
from app.core.config import settings
from app.core.env import is_truthy
from app.core.exceptions import DomainError
from app.db.base import get_db
from app.models.user import User
from app.schemas.auth_config import LDAP_TIMEOUT_MAX_SECONDS
from app.schemas.auth_config import LDAP_TIMEOUT_MIN_SECONDS
from app.schemas.auth_config import AuthMethodTestResponse
from app.services.auth_config_service import SENSITIVE_NO_CHANGE_VALUES

if TYPE_CHECKING:
    from app.auth.ldap_auth import LdapConfig

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/{category}/test", response_model=AuthMethodTestResponse)
async def test_auth_connection(
    category: str,
    config: dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> AuthMethodTestResponse:
    """Test connection for LDAP or OIDC.

    Tests the provided configuration without saving it. Useful for
    validating settings before applying them. A secret the panel cannot show
    (the saved LDAP bind password) is taken from the saved configuration when
    the form leaves it blank; see ``_test_ldap_connection``.

    Args:
        category: Configuration category (ldap or oidc)
        config: Configuration values to test
        db: Database session, read for the saved LDAP bind password

    Returns:
        Test result with success status and message

    Raises:
        HTTPException: If test is not supported for the category
    """
    logger.info(f"Auth connection test for '{category}' by super admin {current_user.email}")

    if category == "ldap":
        # ldap3 is blocking; keep it off the event loop (issue #320).
        ldap_result: AuthMethodTestResponse = await run_in_threadpool(
            _test_ldap_connection, config, db
        )
        return ldap_result
    elif category == "oidc":
        return await _test_oidc_connection(config)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Connection test not supported for category: {category}",
        )


_LDAP_TEST_FAILED = "LDAP connection failed. Please verify server address, port, and credentials."
_LDAP_TARGET_CHANGED_MESSAGE = (
    "Enter the bind password to test a server, transport or bind DN that differs "
    "from the saved LDAP configuration. No connection was attempted."
)
_LDAP_CA_FILE_UNUSABLE = (
    "The LDAP CA certificate file is missing, unreadable or not a PEM bundle. "
    "No connection was attempted."
)


def _flag(value: Any) -> bool:
    """Read a submitted boolean; anything but a clear yes is off (verification stays on)."""
    return value if isinstance(value, bool) else is_truthy(str(value or ""))


#: Fields that decide WHERE, and over what, the saved bind password would be sent.
_BIND_TARGET_FIELDS = (
    "server",
    "use_ssl",
    "use_tls",
    "ca_cert_file",
    "tls_server_name",
    "tls_skip_verify",
    "bind_dn",
)


def _same_bind_target(candidate: "LdapConfig", saved: "LdapConfig") -> bool:
    """Whether ``candidate`` would send its bind password where ``saved`` does."""
    if str(candidate.port) != str(saved.port):
        return False
    return all(getattr(candidate, f) == getattr(saved, f) for f in _BIND_TARGET_FIELDS)


def _test_ldap_connection(
    config: dict[str, Any], db: Session | None = None
) -> AuthMethodTestResponse:
    """Test LDAP connection with provided configuration.

    Binds through login's own ``_connect_and_bind``, so the test negotiates
    encryption exactly as a real login would: with ``ldap_use_tls`` it upgrades to
    StartTLS and refuses to bind if that fails. A separate bind here once sent the
    service-account password over plaintext on a StartTLS deployment, and could
    report success for a configuration login then refused.

    Synchronous: ``ldap3`` binds are blocking, bounded by the submitted ``ldap_timeout``
    (10 s when absent; a value outside the saved setting's range is refused, not clamped),
    so the caller must run this off the event loop (issue #320).

    The saved bind password is never sent to the browser, so the panel submits the
    field blank (older clients echo a placeholder). Then the password is resolved as
    a login resolves it: the saved, decrypted value, else ``LDAP_BIND_PASSWORD``.
    Testing with an anonymous bind instead reported a refusal for a configuration
    every login accepted. A typed password always wins, so new credentials can be
    tested before saving. The saved password is only used when the submitted server,
    port, transport and bind DN match the saved ones: otherwise the button would send
    a secret nobody can read back to whatever host was typed in.

    Args:
        config: LDAP configuration to test
        db: Database session for the saved configuration; ``None`` reads the
            environment, as a login without a session does.

    Returns:
        Test result with success status, message, and optional details
    """
    # Imported in their own try: the except clauses below name LDAPException, which
    # is unbound when the import is what failed.
    try:
        from ldap3.core.exceptions import LDAPException

        from app.auth.ldap_auth import LdapConfig
        from app.auth.ldap_auth import _resolve_login_config
        from app.auth.ldap_directory import LdapCaFileError
        from app.auth.ldap_directory import _close_connection
        from app.auth.ldap_directory import _connect_and_bind
        from app.auth.ldap_directory import _get_ldap_server
        from app.auth.ldap_directory import transport_problem
    except ImportError:
        return AuthMethodTestResponse(
            success=False,
            message="LDAP library (ldap3) is not installed",
        )

    try:
        server_address = config.get("ldap_server", "")
        port = config.get("ldap_port", 636)
        use_ssl = config.get("ldap_use_ssl", True)
        use_tls = bool(config.get("ldap_use_tls", False))

        if not server_address:
            return AuthMethodTestResponse(
                success=False,
                message="LDAP server address is required",
            )

        logger.info(
            f"Testing LDAP connection to {server_address}:{port} (SSL={use_ssl}, StartTLS={use_tls})"
        )

        bind_dn = config.get("ldap_bind_dn") or ""
        submitted_password = config.get("ldap_bind_password")
        use_saved_password = bool(bind_dn) and (
            submitted_password is None or submitted_password in SENSITIVE_NO_CHANGE_VALUES
        )
        bind_password = "" if use_saved_password else (submitted_password or "")

        try:
            timeout = int(config.get("ldap_timeout") or 10)
        except (TypeError, ValueError):
            timeout = 0
        if not LDAP_TIMEOUT_MIN_SECONDS <= timeout <= LDAP_TIMEOUT_MAX_SECONDS:
            return AuthMethodTestResponse(
                success=False,
                message=(
                    f"LDAP timeout must be a whole number of seconds between "
                    f"{LDAP_TIMEOUT_MIN_SECONDS} and {LDAP_TIMEOUT_MAX_SECONDS}"
                ),
            )

        cfg = LdapConfig(
            server=server_address,
            port=port,
            use_ssl=use_ssl,
            use_tls=use_tls,
            # An unset TLS field falls back to the environment, as it does at login.
            ca_cert_file=str(config.get("ldap_ca_cert_file") or settings.LDAP_CA_CERT_FILE or ""),
            tls_server_name=str(
                config.get("ldap_tls_server_name") or settings.LDAP_TLS_SERVER_NAME or ""
            ),
            tls_skip_verify=(
                settings.LDAP_TLS_SKIP_VERIFY
                if config.get("ldap_tls_skip_verify") is None
                else _flag(config.get("ldap_tls_skip_verify"))
            ),
            bind_dn=bind_dn,
            bind_password=bind_password,
            timeout=timeout,
        )
        if use_saved_password:
            saved = _resolve_login_config(db)
            if _same_bind_target(cfg, saved):
                cfg = replace(cfg, bind_password=saved.bind_password)
            elif saved.bind_password:
                return AuthMethodTestResponse(success=False, message=_LDAP_TARGET_CHANGED_MESSAGE)
        if not (cfg.bind_dn and cfg.bind_password):
            cfg = replace(cfg, bind_dn="", bind_password="")  # anonymous bind
        server = _get_ldap_server(cfg)
        problem = transport_problem(cfg, server)
        if problem is not None:
            logger.warning("LDAP connection test refused: %s", problem)
            return AuthMethodTestResponse(success=False, message=problem)
        conn = _connect_and_bind(cfg, server, cfg.bind_dn, cfg.bind_password)
        if conn is None:
            logger.warning("LDAP connection test failed: bind or StartTLS negotiation refused")
            return AuthMethodTestResponse(success=False, message=_LDAP_TEST_FAILED)

        # Get server info
        server_info = {}
        if server.info:
            server_info = {
                "vendor_name": str(server.info.vendor_name) if server.info.vendor_name else None,
                "vendor_version": (
                    str(server.info.vendor_version) if server.info.vendor_version else None
                ),
                "naming_contexts": (
                    [str(nc) for nc in server.info.naming_contexts]
                    if server.info.naming_contexts
                    else []
                ),
            }

        _close_connection(conn, "connection-test")

        logger.info(f"LDAP connection test successful to {server_address}")

        return AuthMethodTestResponse(
            success=True,
            message="LDAP connection successful",
            details={"server_info": server_info},
        )

    except LdapCaFileError:
        logger.warning("LDAP connection test refused: the CA certificate file is unusable")
        return AuthMethodTestResponse(success=False, message=_LDAP_CA_FILE_UNUSABLE)
    except LDAPException as e:
        logger.warning(f"LDAP connection test failed: {e}")
        return AuthMethodTestResponse(success=False, message=_LDAP_TEST_FAILED)
    except Exception as e:
        logger.exception(f"LDAP connection test error: {e}")
        return AuthMethodTestResponse(
            success=False,
            message="LDAP connection failed due to an unexpected error. Check server logs for details.",
        )


def _roles_claim_advertised(configured_roles_claim: str, claims_supported: Any) -> str:
    """Cross-reference the configured roles claim against the discovery document.

    Best-effort only, and says so via ``"unknown"``: ``claims_supported`` is an
    OPTIONAL discovery field (OIDC Discovery §3), several real providers omit it
    entirely, and only the top-level segment of a dotted path (``realm_access`` of
    ``realm_access.roles``) is a claim NAME — the rest is a JSON path into it,
    which a claims list was never going to enumerate.

    Args:
        configured_roles_claim: The admin's configured dotted claim path.
        claims_supported: The discovery document's ``claims_supported`` value, if
            the provider sent one.

    Returns:
        ``"yes"``, ``"no"``, or ``"unknown"``.
    """
    if not isinstance(claims_supported, list) or not claims_supported:
        return "unknown"
    if not configured_roles_claim:
        return "unknown"
    top_level = configured_roles_claim.split(".", 1)[0]
    return "yes" if top_level in claims_supported else "no"


async def _test_oidc_connection(config: dict[str, Any]) -> AuthMethodTestResponse:
    """Test the OIDC provider connection with the supplied configuration.

    Resolves the metadata URL exactly the way the login path does — the explicit
    discovery URL when one is configured, the realm form otherwise. It previously
    always built the realm form, so on a provider that does not serve that URL shape
    this button reported a failure for a configuration that was in fact correct, and
    on a realm provider it tested a URL the login path might not use.

    Args:
        config: OIDC configuration to test

    Returns:
        Test result with success status, message, and optional details
    """
    # Same reason as the LDAP test: ``except httpx.ConnectError`` needs httpx bound.
    try:
        import httpx
    except ImportError:
        return AuthMethodTestResponse(
            success=False,
            message="HTTP client library (httpx) is not installed",
        )

    try:
        from app.utils.url_validation import assert_safe_outbound_url

        server_url = config.get("oidc_server_url", "")
        realm = config.get("oidc_realm", "opentranscribe")
        discovery_url = (config.get("oidc_discovery_url") or "").strip()

        if not discovery_url and not server_url:
            return AuthMethodTestResponse(
                success=False,
                message="Provide either a discovery URL or a server URL",
            )

        if discovery_url:
            well_known_url = discovery_url
        else:
            # Remove trailing slash if present
            server_url = server_url.rstrip("/")
            well_known_url = f"{server_url}/realms/{realm}/.well-known/openid-configuration"

        # This endpoint fetches a super_admin-supplied URL, which is a classic
        # SSRF primitive — it had no guard at all. Private targets stay allowed
        # because an IdP on the LAN or the compose network is a legitimate
        # deployment; what this blocks is cloud instance metadata and friends.
        try:
            assert_safe_outbound_url(
                well_known_url,
                purpose="OIDC discovery test-connection",
                allow_private=True,
            )
        except (HTTPException, DomainError):
            return AuthMethodTestResponse(
                success=False,
                message="That URL is not an allowed outbound target.",
            )

        logger.info(f"Testing OIDC connection to {well_known_url}")

        async with httpx.AsyncClient(timeout=10.0, verify=True) as client:
            response = await client.get(well_known_url)

            if response.status_code == 200:
                oidc_config = response.json()

                # Extract relevant endpoints for display
                details: dict[str, Any] = {
                    "issuer": oidc_config.get("issuer"),
                    "authorization_endpoint": oidc_config.get("authorization_endpoint"),
                    "token_endpoint": oidc_config.get("token_endpoint"),
                    "userinfo_endpoint": oidc_config.get("userinfo_endpoint"),
                    "supported_grant_types": oidc_config.get("grant_types_supported", [])[
                        :5
                    ],  # Limit for readability
                    "supported_scopes": oidc_config.get("scopes_supported", [])[:10],
                }

                # P1.2: this is the only signal available before a real login has
                # happened. `claims_supported` is OPTIONAL per Discovery §3 — a
                # provider that omits it (Authentik does) leaves the check
                # "unknown", not "not advertised", since silence isn't a claim.
                claims_supported = oidc_config.get("claims_supported")
                details["claims_supported"] = (
                    claims_supported[:30] if isinstance(claims_supported, list) else None
                )
                configured_roles_claim = (config.get("oidc_roles_claim") or "").strip()
                details["configured_roles_claim"] = configured_roles_claim or None
                details["roles_claim_advertised"] = _roles_claim_advertised(
                    configured_roles_claim, claims_supported
                )

                logger.info(f"OIDC connection test successful to {server_url}")

                return AuthMethodTestResponse(
                    success=True,
                    message="OIDC connection successful",
                    details=details,
                )
            else:
                logger.warning(
                    f"OIDC connection test failed with status {response.status_code}: "
                    f"{response.text[:200]}"
                )
                return AuthMethodTestResponse(
                    success=False,
                    message=f"The identity provider returned HTTP status {response.status_code}. Check server logs for details.",
                )

    except httpx.ConnectError as e:
        logger.warning(f"OIDC connection test failed: {e}")
        return AuthMethodTestResponse(
            success=False,
            message="Could not connect to the identity provider. Please verify the server URL and network connectivity.",
        )
    except httpx.TimeoutException:
        return AuthMethodTestResponse(
            success=False,
            message="Connection to the identity provider timed out",
        )
    except Exception as e:
        logger.exception(f"OIDC connection test error: {e}")
        return AuthMethodTestResponse(
            success=False,
            message="OIDC connection failed due to an unexpected error. Check server logs for details.",
        )
