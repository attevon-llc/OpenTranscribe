"""The authorization-code flow: PKCE, the authorization URL, token exchange, logout.

**Deliberately still hand-rolled httpx, not Authlib's ``AsyncOAuth2Client`` (#33).**
HANDOFF.md's mapping table listed this module as an Authlib target alongside
``claims.py``; after implementing the claims.py swap and inspecting
``AsyncOAuth2Client`` in detail, the two modules turned out not to carry the same
risk/benefit shape and only one was worth doing.

``claims.py`` verified signatures with python-jose, an unmaintained library with a
history of algorithm-confusion CVEs — swapping the actual crypto library for a
maintained one (joserfc) closes real exposure. This module does not verify anything
cryptographically; PKCE generation and the token-exchange POST are RFC 7636-compliant,
already covered by ``TestValidatePkceCodeVerifier``/``TestGeneratePkcePair``, and have
no CVE history to escape. Swapping it for ``AsyncOAuth2Client`` would trade tested code
for a wire-protocol behavior change with real regression risk against real IdPs — its
default ``token_endpoint_auth_method`` is not necessarily the ``client_secret_post``
body-encoding this module posts today, and it knows nothing about
``OIDCStateStore`` (our own Redis-backed state, not Authlib's) or the internal/public
URL split (``discovery.py``/``endpoints.py``) — all of which would need re-deriving on
top of it for zero net simplification. ``discovery.py`` and ``endpoints.py`` are
unchanged for the same reason: they carry the SSRF guard, TTL cache, and that URL
split, none of which Authlib's own metadata loading knows about either.
"""

import base64
import hashlib
import logging
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from app.auth.oidc.config import DEFAULT_OIDC_SCOPES
from app.auth.oidc.config import REQUIRED_SCOPE
from app.auth.oidc.config import OIDCConfig
from app.auth.oidc.discovery import fetch_jwks
from app.auth.oidc.endpoints import resolve_endpoints

logger = logging.getLogger(__name__)

# PKCE Constants (RFC 7636)
PKCE_CODE_VERIFIER_MIN_LENGTH = 43
PKCE_CODE_VERIFIER_MAX_LENGTH = 128
PKCE_CODE_VERIFIER_LENGTH = 64  # Recommended length for security
# RFC 7636 Section 4.1: unreserved characters for code verifier
PKCE_UNRESERVED_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)

#: Set once the "openid was missing from the configured scopes" line has been logged.
#: The correction itself happens on every request; the log would otherwise repeat on
#: every login attempt for as long as the misconfiguration lasts.
_missing_openid_scope_logged = False


@dataclass
class OIDCTokens:
    """Tokens returned from the provider's token endpoint."""

    access_token: str
    refresh_token: str
    id_token: str
    expires_in: int
    token_type: str


def validate_pkce_code_verifier(code_verifier: str) -> bool:
    """Validate that a PKCE code verifier meets RFC 7636 requirements.

    RFC 7636 Section 4.1 specifies:
    - Length: 43-128 characters
    - Characters: Only unreserved characters (A-Z, a-z, 0-9, -, ., _, ~)
    """
    if not code_verifier:
        logger.warning("PKCE code_verifier is empty")
        return False

    if len(code_verifier) < PKCE_CODE_VERIFIER_MIN_LENGTH:
        logger.warning(
            f"PKCE code_verifier too short: {len(code_verifier)} chars "
            f"(min: {PKCE_CODE_VERIFIER_MIN_LENGTH})"
        )
        return False

    if len(code_verifier) > PKCE_CODE_VERIFIER_MAX_LENGTH:
        logger.warning(
            f"PKCE code_verifier too long: {len(code_verifier)} chars "
            f"(max: {PKCE_CODE_VERIFIER_MAX_LENGTH})"
        )
        return False

    invalid_chars = set(code_verifier) - PKCE_UNRESERVED_CHARS
    if invalid_chars:
        logger.warning(f"PKCE code_verifier contains invalid characters: {invalid_chars!r}")
        return False

    return True


def generate_pkce_pair() -> tuple[str, str]:
    """Generate a PKCE code verifier and code challenge pair (RFC 7636)."""
    random_bytes = secrets.token_bytes(48)
    code_verifier = base64.urlsafe_b64encode(random_bytes).rstrip(b"=").decode("ascii")
    sha256_digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(sha256_digest).rstrip(b"=").decode("ascii")

    logger.debug(
        f"Generated PKCE pair: verifier length={len(code_verifier)}, "
        f"challenge length={len(code_challenge)}"
    )
    return code_verifier, code_challenge


def normalize_scopes(scopes: str) -> str:
    """Return *scopes* with ``openid`` guaranteed present.

    An admin can edit ``oidc_scopes`` to anything, and without ``openid`` the
    provider issues **no ID token** — which, since the access-token fallback was
    removed, means every login fails at validation with nothing to point at. Forcing
    the scope in is cheaper than diagnosing that, and OIDC Core §3.1.2.1 requires it
    for an authentication request anyway.
    """
    global _missing_openid_scope_logged

    parts = (scopes or DEFAULT_OIDC_SCOPES).split()
    if REQUIRED_SCOPE in parts:
        return " ".join(parts)

    if not _missing_openid_scope_logged:
        logger.warning(
            "Configured OIDC scopes %r omit %r; adding it. Without it the provider "
            "returns no ID token and every login would fail validation.",
            scopes,
            REQUIRED_SCOPE,
        )
        _missing_openid_scope_logged = True
    return " ".join([REQUIRED_SCOPE, *parts])


async def get_authorization_url(
    state: str, cfg: OIDCConfig | None = None
) -> tuple[str, str | None]:
    """Generate the authorization URL for OIDC login.

    Supports PKCE (RFC 7636) for OAuth 2.1 compliance.

    Args:
        state: Random state parameter for CSRF protection.
        cfg: Resolved OIDC configuration (if None, loads from env).

    Returns:
        ``(authorization_url, code_verifier or None if PKCE disabled)``.
    """
    if cfg is None:
        cfg = OIDCConfig.from_env()

    urls = await resolve_endpoints(cfg)
    params = {
        "client_id": cfg.client_id,
        "redirect_uri": cfg.callback_url,
        "response_type": "code",
        "scope": normalize_scopes(cfg.scopes),
        "state": state,
    }

    code_verifier = None
    if cfg.use_pkce:
        code_verifier, code_challenge = generate_pkce_pair()
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
        logger.debug("PKCE enabled: added code_challenge to authorization URL")

    return f"{urls['authorization']}?{urlencode(params)}", code_verifier


async def exchange_code_for_tokens(
    code: str,
    code_verifier: str | None = None,
    cfg: OIDCConfig | None = None,
) -> OIDCTokens | None:
    """Exchange an authorization code for tokens.

    Args:
        code: Authorization code from the provider's callback.
        code_verifier: PKCE code verifier (required if PKCE was used).
        cfg: Resolved OIDC configuration (if None, loads from env).
    """
    if cfg is None:
        cfg = OIDCConfig.from_env()

    urls = await resolve_endpoints(cfg, internal=True)

    token_data = {
        "grant_type": "authorization_code",
        "client_id": cfg.client_id,
        "client_secret": cfg.client_secret,
        "code": code,
        "redirect_uri": cfg.callback_url,
    }

    if code_verifier:
        if not validate_pkce_code_verifier(code_verifier):
            logger.error("PKCE code_verifier validation failed - rejecting token exchange")
            return None
        token_data["code_verifier"] = code_verifier
        logger.debug("PKCE enabled: added code_verifier to token exchange request")

    async with httpx.AsyncClient(timeout=cfg.timeout) as client:
        try:
            response = await client.post(urls["token"], data=token_data)
            response.raise_for_status()
            data = response.json()

            return OIDCTokens(
                access_token=data["access_token"],
                refresh_token=data.get("refresh_token", ""),
                id_token=data.get("id_token", ""),
                expires_in=data.get("expires_in", 300),
                token_type=data.get("token_type", "Bearer"),
            )
        except httpx.HTTPError as e:
            logger.error(f"Failed to exchange code for tokens: {e}")
            return None


async def get_oidc_jwks(cfg: OIDCConfig | None = None) -> dict | None:
    """Fetch the provider's public keys (JWKS), served from the TTL cache."""
    if cfg is None:
        cfg = OIDCConfig.from_env()

    urls = await resolve_endpoints(cfg, internal=True)
    return await fetch_jwks(urls["certs"], timeout=float(cfg.timeout))


async def call_federated_logout(
    oidc_refresh_token: str,
    cfg: OIDCConfig | None = None,
) -> bool:
    """Terminate the federated session at the provider.

    Uses the stored provider refresh token for a back-channel logout so the user's
    IdP session is ended when they log out of OpenTranscribe (issue #125).

    Note this message shape (``client_id`` + ``client_secret`` + ``refresh_token``
    POSTed to ``end_session_endpoint``) is a realm-provider legacy extension, not
    OIDC RP-Initiated Logout 1.0 — which is a front-channel redirect carrying
    ``id_token_hint``. Replacing it is Phase 3g of ``plans/oidc-conformance-plan.md``;
    ``refresh_token.oidc_id_token`` is stored server-side ready for it.

    Args:
        oidc_refresh_token: Decrypted provider refresh token.
        cfg: Resolved OIDC configuration. If None, loads from env.

    Returns:
        True if the federated session was terminated. Failure is non-fatal — the
        local session is always cleared regardless.
    """
    if cfg is None:
        cfg = OIDCConfig.from_env()

    urls = await resolve_endpoints(cfg, internal=True)
    if not urls.get("logout"):
        # A provider whose metadata omits end_session_endpoint has no back-channel
        # logout; the local session is still cleared by the caller.
        logger.info("Provider advertises no end-session endpoint — skipping federated logout")
        return False

    logout_data = {
        "client_id": cfg.client_id,
        "client_secret": cfg.client_secret,
        "refresh_token": oidc_refresh_token,
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(urls["logout"], data=logout_data)
            if response.status_code in (200, 204):
                logger.info("OIDC federated logout successful")
                return True
            logger.warning(
                f"OIDC logout returned unexpected status {response.status_code}: "
                f"{response.text[:200]}"
            )
            return False
    except httpx.TimeoutException:
        logger.warning("OIDC logout timed out (5s) — local session still cleared")
        return False
    except httpx.HTTPError as e:
        logger.warning(f"OIDC logout HTTP error: {e} — local session still cleared")
        return False
    except Exception as e:
        logger.error(f"Unexpected error during OIDC logout: {type(e).__name__}: {e}")
        return False
