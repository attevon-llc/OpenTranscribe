"""Pluggable external token-verifier registry (cloud-edition seam).

The community edition registers nothing here: the registry stays empty and
``get_current_user`` takes its normal local-JWT path with zero overhead. The
commercial cloud edition registers a managed-IdP verifier at startup;
``get_current_user`` then offers each bearer token to the registered verifiers
before falling back to local JWT validation.

Contract (covered by ``CLOUD_SEAM_VERSION`` in app.auth.constants):
  - Verifiers MUST be fast and networkless on the hot path (local signature
    verification against a cached key) — they run on every request.
  - ``verify`` returns ``None`` for "not my token / invalid" so the next
    verifier (or the local path) gets a chance. It must not raise for
    ordinary non-matching tokens.
  - A crashing verifier is contained: errors are logged and treated as None,
    so a broken cloud layer can never take down local authentication.
"""

import logging
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Optional
from typing import Protocol

from fastapi import Request

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExternalIdentity:
    """Verified identity extracted from an external provider's token.

    ``org_id``/``org_role`` carry tenant context (e.g. a managed IdP's nested
    org claim). ``is_admin`` means PLATFORM admin and should stay False for
    customer org roles — org-admin is a per-tenant capability, not a platform
    role (privilege separation).

    ``email_verified`` MUST be True only when the IdP asserts the address is
    verified. It defaults to False (fail-closed): JIT provisioning refuses to
    link an external identity to an EXISTING local account by email match
    unless the email is verified — otherwise anyone who can register the
    victim's address at the IdP could take over the local account
    (self-serve IdPs do not gate registration the way an admin-run
    Keycloak/LDAP does).
    """

    provider: str
    external_id: str
    email: str
    full_name: Optional[str] = None
    org_id: Optional[str] = None
    org_role: Optional[str] = None
    is_admin: bool = False
    email_verified: bool = False
    raw_claims: dict[str, Any] = field(default_factory=dict)


class TokenVerifier(Protocol):
    """Protocol for external token verifiers (implemented by the cloud layer)."""

    def verify(self, token: str, request: Request) -> Optional[ExternalIdentity]:
        """Return the verified identity, or None if this token isn't ours/valid."""
        ...


_verifiers: dict[str, TokenVerifier] = {}


def register_verifier(auth_type: str, verifier: TokenVerifier) -> None:
    """Register an external token verifier for an auth type (idempotent)."""
    if auth_type in _verifiers:
        logger.warning(f"Replacing already-registered token verifier for '{auth_type}'")
    _verifiers[auth_type] = verifier
    logger.info(f"Registered external token verifier: {auth_type}")


def unregister_verifier(auth_type: str) -> None:
    """Remove a registered verifier (primarily for tests)."""
    _verifiers.pop(auth_type, None)


def has_verifiers() -> bool:
    """True when any external verifier is registered (cloud edition)."""
    return bool(_verifiers)


def get_registered_providers() -> list[str]:
    """Auth types with a registered external verifier (for /auth/methods)."""
    return sorted(_verifiers)


def verify_external_token(token: str, request: Request) -> Optional[ExternalIdentity]:
    """Offer a bearer token to every registered verifier, first match wins.

    Exceptions are contained per-verifier so external-auth problems can never
    break local authentication.
    """
    for auth_type, verifier in _verifiers.items():
        try:
            identity = verifier.verify(token, request)
        except Exception:
            logger.exception(f"External token verifier '{auth_type}' raised; treating as no-match")
            continue
        if identity is not None:
            return identity
    return None
