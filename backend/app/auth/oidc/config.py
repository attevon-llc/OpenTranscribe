"""Resolved OpenID Connect configuration (database first, environment second).

``OIDCConfig`` is created once per request and passed to every helper in this
package. It is frozen: nothing mutates global provider state mid-flow, so an admin
saving the auth tab cannot change the issuer an in-flight login is validating
against.
"""

import logging
from dataclasses import dataclass

from app.core.config import settings as env_settings

logger = logging.getLogger(__name__)

#: Providers built on a realm model put realm roles in ``realm_access.roles``.
#: Others do not: Authentik and Okta use a flat ``groups`` claim, Entra uses
#: ``roles``. The dotted path is configurable so the admin-role mapping works
#: anywhere.
DEFAULT_ROLES_CLAIM = "realm_access.roles"

#: Minimum scopes for an OIDC login. Providers that carry group membership in a
#: dedicated scope (Authentik's ``goauthentik.io/api``-style scopes, Okta's
#: ``groups``) need it appended here or the roles claim will be absent from the
#: token.
DEFAULT_OIDC_SCOPES = "openid email profile"

#: The one scope that is not optional. Without it the provider issues no ID token,
#: and the ID token is the only thing this RP will authenticate on
#: (:mod:`app.auth.oidc.claims`). Enforced in ``get_authorization_url`` rather than
#: trusted to an admin's edit of ``oidc_scopes``.
REQUIRED_SCOPE = "openid"


@dataclass(frozen=True)
class OIDCConfig:
    """Immutable OIDC configuration resolved from database or environment."""

    enabled: bool = False
    server_url: str = ""
    internal_url: str = ""
    realm: str = "opentranscribe"
    client_id: str = ""
    client_secret: str = ""
    callback_url: str = ""
    admin_role: str = "admin"
    timeout: int = 30
    use_pkce: bool = True
    verify_issuer: bool = True
    # Audience validation is what stops a token minted for another client of the
    # same IdP being accepted here, so its default is the ON position — matching
    # core/config.py:OIDC_VERIFY_AUDIENCE. A token-validation control must never
    # default open.
    verify_audience: bool = True
    audience: str = ""
    discovery_url: str = ""
    issuer: str = ""
    roles_claim: str = DEFAULT_ROLES_CLAIM
    scopes: str = DEFAULT_OIDC_SCOPES

    @classmethod
    def from_env(cls) -> "OIDCConfig":
        """Create config from environment variables only."""
        return cls(
            enabled=env_settings.OIDC_ENABLED,
            server_url=env_settings.OIDC_SERVER_URL,
            internal_url=env_settings.OIDC_INTERNAL_URL,
            realm=env_settings.OIDC_REALM,
            client_id=env_settings.OIDC_CLIENT_ID,
            client_secret=env_settings.OIDC_CLIENT_SECRET,
            callback_url=env_settings.OIDC_CALLBACK_URL,
            admin_role=env_settings.OIDC_ADMIN_ROLE,
            timeout=env_settings.OIDC_TIMEOUT,
            use_pkce=env_settings.OIDC_USE_PKCE,
            verify_issuer=env_settings.OIDC_VERIFY_ISSUER,
            verify_audience=env_settings.OIDC_VERIFY_AUDIENCE,
            audience=env_settings.OIDC_AUDIENCE,
            discovery_url=env_settings.OIDC_DISCOVERY_URL,
            issuer=env_settings.OIDC_ISSUER,
            roles_claim=env_settings.OIDC_ROLES_CLAIM or DEFAULT_ROLES_CLAIM,
            scopes=env_settings.OIDC_SCOPES or DEFAULT_OIDC_SCOPES,
        )

    @classmethod
    def from_db(cls, db) -> "OIDCConfig":
        """Create config from database with env fallback.

        Uses ``DynamicAuthSettings``, which checks DB > .env > coded default.
        """
        from app.core.auth_settings import get_auth_settings

        auth = get_auth_settings(db)

        def _get(key: str, default):
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
            enabled=_get_bool("oidc_enabled", env_settings.OIDC_ENABLED),
            server_url=str(_get("oidc_server_url", env_settings.OIDC_SERVER_URL) or ""),
            internal_url=str(_get("oidc_internal_url", env_settings.OIDC_INTERNAL_URL) or ""),
            realm=str(_get("oidc_realm", env_settings.OIDC_REALM) or "opentranscribe"),
            client_id=str(_get("oidc_client_id", env_settings.OIDC_CLIENT_ID) or ""),
            client_secret=str(_get("oidc_client_secret", env_settings.OIDC_CLIENT_SECRET) or ""),
            callback_url=str(_get("oidc_callback_url", env_settings.OIDC_CALLBACK_URL) or ""),
            admin_role=str(_get("oidc_admin_role", env_settings.OIDC_ADMIN_ROLE) or "admin"),
            timeout=_get_int("oidc_timeout", env_settings.OIDC_TIMEOUT),
            use_pkce=_get_bool("oidc_use_pkce", env_settings.OIDC_USE_PKCE),
            verify_issuer=_get_bool("oidc_verify_issuer", env_settings.OIDC_VERIFY_ISSUER),
            verify_audience=_get_bool("oidc_verify_audience", env_settings.OIDC_VERIFY_AUDIENCE),
            audience=str(_get("oidc_audience", env_settings.OIDC_AUDIENCE) or ""),
            discovery_url=str(_get("oidc_discovery_url", env_settings.OIDC_DISCOVERY_URL) or ""),
            issuer=str(_get("oidc_issuer", env_settings.OIDC_ISSUER) or ""),
            roles_claim=str(
                _get("oidc_roles_claim", env_settings.OIDC_ROLES_CLAIM or DEFAULT_ROLES_CLAIM)
                or DEFAULT_ROLES_CLAIM
            ),
            scopes=str(
                _get("oidc_scopes", env_settings.OIDC_SCOPES or DEFAULT_OIDC_SCOPES)
                or DEFAULT_OIDC_SCOPES
            ),
        )
