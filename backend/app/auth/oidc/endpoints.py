"""Where the provider's OIDC endpoints come from.

Two sources, in order: the provider's own ``.well-known/openid-configuration``
document when a discovery URL is configured, and otherwise the realm-derived URL
shape that predates discovery support (issue #353). The realm form is kept exactly
as it was so every existing realm-based deployment behaves identically —
``tests/unit/test_oidc_discovery.py::TestRealmFallbackUnchanged`` pins the six URLs
byte for byte.
"""

import logging
from typing import Any

from app.auth.oidc.config import OIDCConfig
from app.auth.oidc.discovery import fetch_discovery_document
from app.auth.oidc.discovery import to_internal

logger = logging.getLogger(__name__)


def _get_realm_urls(cfg: OIDCConfig, internal: bool = False) -> dict:
    """Build the realm endpoint URLs — the no-discovery fallback.

    This is a single vendor's URL shape and is named for what it is. It stays the
    default so every existing realm-based deployment behaves exactly as before;
    providers with a different layout supply a discovery URL instead (see
    :func:`resolve_endpoints`).

    Args:
        cfg: Resolved OIDC configuration.
        internal: If True, use the internal URL for back-channel calls.
    """
    base_url = cfg.internal_url if internal and cfg.internal_url else cfg.server_url

    base = f"{base_url}/realms/{cfg.realm}"
    return {
        # The browser follows this one, so it is always the public server URL.
        "authorization": f"{cfg.server_url}/realms/{cfg.realm}/protocol/openid-connect/auth",
        "token": f"{base}/protocol/openid-connect/token",
        "userinfo": f"{base}/protocol/openid-connect/userinfo",
        "logout": f"{base}/protocol/openid-connect/logout",
        "certs": f"{base}/protocol/openid-connect/certs",
        # Issuer is an identity, not a reachable endpoint — never the internal host.
        "issuer": f"{cfg.server_url}/realms/{cfg.realm}",
    }


def _endpoints_from_discovery(cfg: OIDCConfig, document: dict[str, Any], internal: bool) -> dict:
    """Map an OIDC discovery document onto our endpoint names.

    The module docstring's "discovery only ever advertises the public endpoints"
    assumption holds for a provider with a fixed configured frontend URL, but not
    for one that builds its metadata from the request it was fetched with
    (Authentik). ``oidc_discovery_url`` has to be the *internal* (compose-network)
    host for the backend to reach it at all, so such a provider echoes that host back
    in ``authorization_endpoint`` too — a URL the browser can never resolve. Re-point
    it at ``server_url`` unconditionally (independent of the ``internal`` flag this
    call was made with) whenever it matches ``internal_url``; a provider that already
    returned a public host is untouched, since :func:`to_internal` only rewrites a
    netloc match.
    """
    endpoints = {
        "authorization": str(document.get("authorization_endpoint") or ""),
        "token": str(document.get("token_endpoint") or ""),
        "userinfo": str(document.get("userinfo_endpoint") or ""),
        "logout": str(document.get("end_session_endpoint") or ""),
        "certs": str(document.get("jwks_uri") or ""),
        "issuer": cfg.issuer or str(document.get("issuer") or ""),
    }
    if cfg.internal_url and cfg.server_url:
        endpoints["authorization"] = to_internal(
            endpoints["authorization"], cfg.internal_url, cfg.server_url
        )

    if not internal or not cfg.internal_url:
        return endpoints

    # Same split as the realm builder: the authorization URL is browser-facing and
    # the issuer is an identity string; everything else is a back-channel call that
    # must resolve on the compose network.
    return {
        name: url
        if name in ("authorization", "issuer")
        else to_internal(url, cfg.server_url, cfg.internal_url)
        for name, url in endpoints.items()
    }


async def resolve_endpoints(cfg: OIDCConfig, internal: bool = False) -> dict:
    """Resolve the provider's OIDC endpoints, preferring discovery metadata.

    Args:
        cfg: Resolved OIDC configuration.
        internal: If True, back-channel endpoints use the internal (compose-network)
            host. The authorization endpoint stays public either way.

    Returns:
        Dict with ``authorization``, ``token``, ``userinfo``, ``logout``, ``certs``
        and ``issuer``. Falls back to the realm-derived URLs when no discovery URL is
        set or the document cannot be fetched.
    """
    if cfg.discovery_url:
        document = await fetch_discovery_document(cfg.discovery_url, timeout=float(cfg.timeout))
        if document:
            return _endpoints_from_discovery(cfg, document, internal)
        logger.warning(
            "OIDC discovery failed for %s — falling back to realm-derived endpoints",
            cfg.discovery_url,
        )
    return _get_realm_urls(cfg, internal=internal)
