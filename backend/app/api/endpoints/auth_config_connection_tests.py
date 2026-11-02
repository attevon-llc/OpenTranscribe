"""Connection testing for the authentication-configuration API.

``POST /{category}/test`` validates an LDAP or OIDC configuration without saving
it. Split from ``auth_config.py`` by concern: the config CRUD/audit handlers
there never touch the network, while everything here dials an admin-supplied
host (``_test_oidc_connection`` fetches an admin-supplied URL, so it runs the
outbound-URL safety check). The super_admin gate and the route's position in
the registration order are unchanged.
"""

import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import status
from fastapi.concurrency import run_in_threadpool

from app.api.endpoints.auth import get_current_active_superuser
from app.core.exceptions import DomainError
from app.models.user import User
from app.schemas.auth_config import AuthMethodTestResponse

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/{category}/test", response_model=AuthMethodTestResponse)
async def test_auth_connection(
    category: str,
    config: dict[str, Any],
    current_user: User = Depends(get_current_active_superuser),
) -> AuthMethodTestResponse:
    """Test connection for LDAP or OIDC.

    Tests the provided configuration without saving it. Useful for
    validating settings before applying them.

    Args:
        category: Configuration category (ldap or oidc)
        config: Configuration values to test

    Returns:
        Test result with success status and message

    Raises:
        HTTPException: If test is not supported for the category
    """
    logger.info(f"Auth connection test for '{category}' by super admin {current_user.email}")

    if category == "ldap":
        # ldap3 is blocking; keep it off the event loop (issue #320).
        ldap_result: AuthMethodTestResponse = await run_in_threadpool(_test_ldap_connection, config)
        return ldap_result
    elif category == "oidc":
        return await _test_oidc_connection(config)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Connection test not supported for category: {category}",
        )


def _test_ldap_connection(config: dict[str, Any]) -> AuthMethodTestResponse:
    """Test LDAP connection with provided configuration.

    Synchronous: ``ldap3`` binds are blocking with a 10 s connect timeout, so the
    caller must run this off the event loop (issue #320).

    Args:
        config: LDAP configuration to test

    Returns:
        Test result with success status, message, and optional details
    """
    # Imported in their own try: the except clauses below name LDAPException, which
    # is unbound when the import is what failed.
    try:
        import ldap3
        from ldap3.core.exceptions import LDAPException
    except ImportError:
        return AuthMethodTestResponse(
            success=False,
            message="LDAP library (ldap3) is not installed",
        )

    try:
        server_address = config.get("ldap_server", "")
        port = config.get("ldap_port", 636)
        use_ssl = config.get("ldap_use_ssl", True)

        if not server_address:
            return AuthMethodTestResponse(
                success=False,
                message="LDAP server address is required",
            )

        logger.info(f"Testing LDAP connection to {server_address}:{port} (SSL={use_ssl})")

        server = ldap3.Server(
            server_address,
            port=port,
            use_ssl=use_ssl,
            get_info=ldap3.ALL,
            connect_timeout=10,
        )

        bind_dn = config.get("ldap_bind_dn", "")
        bind_password = config.get("ldap_bind_password", "")

        if bind_dn and bind_password:
            # Try authenticated bind
            conn = ldap3.Connection(
                server,
                user=bind_dn,
                password=bind_password,
                auto_bind=True,
                receive_timeout=10,
            )
        else:
            # Try anonymous bind
            conn = ldap3.Connection(
                server,
                auto_bind=True,
                receive_timeout=10,
            )

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

        conn.unbind()

        logger.info(f"LDAP connection test successful to {server_address}")

        return AuthMethodTestResponse(
            success=True,
            message="LDAP connection successful",
            details={"server_info": server_info},
        )

    except LDAPException as e:
        logger.warning(f"LDAP connection test failed: {e}")
        return AuthMethodTestResponse(
            success=False,
            message="LDAP connection failed. Please verify server address, port, and credentials.",
        )
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
