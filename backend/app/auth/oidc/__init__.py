"""OpenID Connect authentication — provider-neutral.

Endpoints come from the provider's discovery document when a discovery URL is
configured, and from the realm-derived ``/realms/<realm>/protocol/openid-connect/...``
layout otherwise (issue #353). PKCE (RFC 7636) is on by default.

This package replaced a single 900-line module named for one vendor. The split is by
protocol stage, and each file stays under the repo's ~300-line ceiling:

* :mod:`config` — ``OIDCConfig``, resolved DB > .env > coded default.
* :mod:`discovery` — fetching and caching ``.well-known/openid-configuration``+JWKS.
* :mod:`endpoints` — discovery-or-realm endpoint resolution.
* :mod:`flow` — PKCE, authorization URL, token exchange, federated logout.
* :mod:`claims` — ID-token verification and claim extraction.
* :mod:`provisioning` — just-in-time user creation, linking and reconciliation.
"""

from app.auth.oidc.claims import ID_TOKEN_SIGNING_ALGORITHMS
from app.auth.oidc.claims import OIDCUserData
from app.auth.oidc.claims import safe_signing_algorithms
from app.auth.oidc.claims import validate_token
from app.auth.oidc.config import DEFAULT_OIDC_SCOPES
from app.auth.oidc.config import DEFAULT_ROLES_CLAIM
from app.auth.oidc.config import OIDCConfig
from app.auth.oidc.endpoints import resolve_endpoints
from app.auth.oidc.flow import OIDCTokens
from app.auth.oidc.flow import call_federated_logout
from app.auth.oidc.flow import exchange_code_for_tokens
from app.auth.oidc.flow import generate_pkce_pair
from app.auth.oidc.flow import get_authorization_url
from app.auth.oidc.flow import get_oidc_jwks
from app.auth.oidc.flow import normalize_scopes
from app.auth.oidc.flow import validate_pkce_code_verifier
from app.auth.oidc.provisioning import sync_oidc_user_to_db

__all__ = [
    "DEFAULT_OIDC_SCOPES",
    "DEFAULT_ROLES_CLAIM",
    "ID_TOKEN_SIGNING_ALGORITHMS",
    "OIDCConfig",
    "OIDCTokens",
    "OIDCUserData",
    "call_federated_logout",
    "exchange_code_for_tokens",
    "generate_pkce_pair",
    "get_authorization_url",
    "get_oidc_jwks",
    "normalize_scopes",
    "resolve_endpoints",
    "safe_signing_algorithms",
    "sync_oidc_user_to_db",
    "validate_pkce_code_verifier",
    "validate_token",
]
