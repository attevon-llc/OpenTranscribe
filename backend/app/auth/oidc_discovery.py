"""OIDC provider metadata (``.well-known``) and JWKS fetching, TTL-cached.

Why this module exists (issue #353): the OIDC integration advertised "Keycloak or any
OpenID Connect provider" but built every endpoint by concatenating
``<server>/realms/<realm>/protocol/openid-connect/...``. That path shape is Keycloak's
alone — an Authentik deployment got redirected to
``https://idp/realms/default/protocol/openid-connect/auth`` and was handed a 404. Every
conforming provider publishes its real endpoints in a discovery document, so fetching
that document is the portable way to find them.

Caching is not a micro-optimisation here. ``get_keycloak_jwks`` refetched the **full**
key set on every single token validation; against a third-party IdP that rate-limits
(or that simply is not a container on the same Docker network) that turns each login
into an extra round trip and, eventually, a 429.

Failure policy: every function degrades to ``None`` rather than raising. The caller
falls back to the realm-derived URLs, so a discovery blip can never take down an
otherwise-working Keycloak deployment, and no exception from here can escape into the
login path.

SSRF: the discovery URL is admin-supplied, so it goes through
:func:`app.utils.url_validation.assert_safe_outbound_url` — but with
``allow_private=True``, because an IdP on the LAN or on the compose network
(``http://keycloak:8080``) is the normal case for this deployment, not an attack.
"""

from __future__ import annotations

import logging
import threading
from typing import Any
from urllib.parse import urlparse
from urllib.parse import urlunparse

import httpx
from cachetools import TTLCache

from app.utils.url_validation import assert_safe_outbound_url

logger = logging.getLogger(__name__)

#: Providers rotate signing keys and move endpoints rarely; 15 minutes bounds the
#: staleness window while removing the per-validation refetch entirely.
DISCOVERY_CACHE_TTL_SECONDS = 900

_lock = threading.Lock()
_discovery_cache: TTLCache[str, dict[str, Any]] = TTLCache(
    maxsize=16, ttl=DISCOVERY_CACHE_TTL_SECONDS
)
_jwks_cache: TTLCache[str, dict[str, Any]] = TTLCache(maxsize=16, ttl=DISCOVERY_CACHE_TTL_SECONDS)


def _cache_get(cache: TTLCache[str, dict[str, Any]], key: str) -> dict[str, Any] | None:
    # The sync FastAPI threadpool and the Celery worker pool share one process, so
    # TTLCache (not thread-safe) must be guarded.
    with _lock:
        hit: dict[str, Any] | None = cache.get(key)
    return hit


def _cache_put(cache: TTLCache[str, dict[str, Any]], key: str, value: dict[str, Any]) -> None:
    with _lock:
        cache[key] = value


def clear_discovery_caches() -> None:
    """Drop cached metadata and keys (admin config change, or test isolation)."""
    with _lock:
        _discovery_cache.clear()
        _jwks_cache.clear()


async def _fetch_json(url: str, timeout: float, purpose: str) -> dict[str, Any] | None:
    """GET *url* and return its JSON object, or None on any failure."""
    try:
        assert_safe_outbound_url(url, purpose=purpose, allow_private=True)
    except Exception as exc:
        # assert_safe_outbound_url raises HTTPException; a 400 must not surface from
        # inside a login redirect, so it is logged and treated as "unavailable".
        logger.warning("Refused %s fetch of %r: %s", purpose, url, exc)
        return None

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPError as exc:
        logger.warning("Failed %s fetch of %r: %s", purpose, url, exc)
        return None
    except ValueError as exc:
        logger.warning("Malformed JSON from %s at %r: %s", purpose, url, exc)
        return None

    if not isinstance(data, dict):
        logger.warning("Unexpected %s payload from %r: not a JSON object", purpose, url)
        return None
    return data


async def fetch_discovery_document(
    discovery_url: str, timeout: float = 10.0
) -> dict[str, Any] | None:
    """Fetch and cache an OIDC provider's ``.well-known/openid-configuration``.

    Args:
        discovery_url: Absolute URL of the provider metadata document.
        timeout: Per-request timeout in seconds.

    Returns:
        The parsed metadata, or None when it could not be fetched. Only successes are
        cached, so a transient failure is retried on the next login.
    """
    if not discovery_url:
        return None

    cached = _cache_get(_discovery_cache, discovery_url)
    if cached is not None:
        return cached

    document = await _fetch_json(discovery_url, timeout, "OIDC discovery")
    if document is None:
        return None

    if not document.get("authorization_endpoint") or not document.get("token_endpoint"):
        # A document missing the core endpoints is worse than useless: caching it would
        # pin a broken config for the whole TTL.
        logger.warning(
            "OIDC discovery document at %r is missing authorization/token endpoints",
            discovery_url,
        )
        return None

    _cache_put(_discovery_cache, discovery_url, document)
    logger.info("Loaded OIDC discovery document from %s", discovery_url)
    return document


async def fetch_jwks(jwks_uri: str, timeout: float = 10.0) -> dict[str, Any] | None:
    """Fetch and cache a provider's JWKS.

    Args:
        jwks_uri: Absolute URL of the JSON Web Key Set.
        timeout: Per-request timeout in seconds.

    Returns:
        The parsed key set, or None when it could not be fetched.
    """
    if not jwks_uri:
        return None

    cached = _cache_get(_jwks_cache, jwks_uri)
    if cached is not None:
        return cached

    jwks = await _fetch_json(jwks_uri, timeout, "OIDC JWKS")
    if jwks is None or not jwks.get("keys"):
        if jwks is not None:
            logger.warning("JWKS at %r contains no keys", jwks_uri)
        return None

    _cache_put(_jwks_cache, jwks_uri, jwks)
    return jwks


def to_internal(url: str, public_base: str, internal_base: str) -> str:
    """Re-point a discovered endpoint at the internal (compose-network) host.

    The browser must follow the *public* authorization URL, while the backend reaches
    the same IdP over the Docker network. Discovery only ever advertises the public
    endpoints, so back-channel URLs get their scheme+netloc swapped for the internal
    base's.

    A URL on a different host than ``public_base`` is returned untouched — the internal
    base only fronts the configured IdP, so rewriting a third-party host would send the
    request to the wrong server.

    Args:
        url: The endpoint URL to rewrite.
        public_base: The IdP's browser-facing base URL (may be empty).
        internal_base: The backend-facing base URL (empty disables rewriting).

    Returns:
        The rewritten URL, or *url* unchanged.
    """
    if not url or not internal_base:
        return url

    internal = urlparse(internal_base)
    if not internal.scheme or not internal.netloc:
        logger.warning("Internal URL %r is not absolute; leaving %r unchanged", internal_base, url)
        return url

    parsed = urlparse(url)
    if public_base:
        public = urlparse(public_base)
        if public.netloc and public.netloc != parsed.netloc:
            return url

    return urlunparse(parsed._replace(scheme=internal.scheme, netloc=internal.netloc))
