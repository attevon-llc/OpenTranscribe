"""Resolved trusted-header (reverse-proxy) configuration — database first, env second.

Frozen and built once per request, like :class:`~app.auth.oidc.config.OIDCConfig`:
an admin saving the Proxy tab must not change the allowlist an in-flight assertion
is being validated against.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.auth.header_trust import TrustedNetwork
from app.auth.header_trust import parse_trusted_proxies

logger = logging.getLogger(__name__)

#: The header an authenticating proxy conventionally uses for the user's address.
DEFAULT_EMAIL_HEADER = "X-Forwarded-Email"
#: Display name for a JIT-created account. Absent = fall back to the email local part.
DEFAULT_NAME_HEADER = "X-Forwarded-User"
#: Group names. Deliberately **no default**: a group header is a privilege input,
#: and reading one nobody configured would let a proxy that happens to forward
#: ``X-Forwarded-Groups`` start driving in-app group membership silently.
DEFAULT_GROUPS_HEADER = ""
#: Multi-value separator inside the groups header. Comma matches oauth2-proxy and
#: Authelia; a deployment brokering LDAP DNs (which contain commas) sets ``;``.
DEFAULT_GROUPS_SEPARATOR = ","
#: Header carrying ``user``/``admin``. Empty means the role header is OFF, which is
#: the point: privilege over HTTP is opt-in here, not the default it is elsewhere.
DEFAULT_ROLE_HEADER = ""
#: Header carrying the shared secret, when one is configured.
SHARED_SECRET_HEADER = "X-OpenTranscribe-Proxy-Secret"  # noqa: S105 # nosec B105


@dataclass(frozen=True)
class ProxyConfig:
    """Immutable trusted-header configuration."""

    enabled: bool = False
    #: Comma-separated CIDR allowlist, as configured. Empty = trust nobody, which is
    #: the fail-closed state the whole feature is built around.
    trusted_proxies: str = ""
    email_header: str = DEFAULT_EMAIL_HEADER
    name_header: str = DEFAULT_NAME_HEADER
    groups_header: str = DEFAULT_GROUPS_HEADER
    groups_separator: str = DEFAULT_GROUPS_SEPARATOR
    role_header: str = DEFAULT_ROLE_HEADER
    shared_secret: str = ""
    #: Comma-separated email domain allowlist. Empty admits every domain — the same
    #: upgrade-safe reading ``oidc_allowed_groups`` uses. Comma rather than the
    #: semicolon the group lists use, because a domain contains no commas and its
    #: nearest neighbour here (``proxy_trusted_proxies``) is comma-delimited.
    allowed_domains: str = ""
    #: Whether an unknown identity may be created on the fly. Off = the account must
    #: already exist (invitation / SCIM / admin-created), which is the posture a
    #: deployment wants once it has provisioning.
    jit_provisioning: bool = True

    @property
    def networks(self) -> list[TrustedNetwork]:
        """The parsed allowlist. Empty list = refuse every header assertion."""
        return parse_trusted_proxies(self.trusted_proxies, label="proxy trusted proxy")

    @classmethod
    def from_db(cls, db) -> ProxyConfig:
        """Resolve every key through ``DynamicAuthSettings`` (DB > .env > default).

        Args:
            db: Request-scoped session.

        Returns:
            The effective configuration.
        """
        from app.core.auth_settings import get_auth_settings

        auth = get_auth_settings(db)
        return cls(
            enabled=bool(auth.proxy_enabled),
            trusted_proxies=str(auth.proxy_trusted_proxies or ""),
            email_header=str(auth.proxy_email_header or DEFAULT_EMAIL_HEADER),
            name_header=str(auth.proxy_name_header or DEFAULT_NAME_HEADER),
            groups_header=str(auth.proxy_groups_header or ""),
            groups_separator=str(auth.proxy_groups_separator or DEFAULT_GROUPS_SEPARATOR),
            role_header=str(auth.proxy_role_header or ""),
            shared_secret=str(auth.proxy_shared_secret or ""),
            allowed_domains=str(auth.proxy_allowed_domains or ""),
            jit_provisioning=bool(auth.proxy_jit_provisioning),
        )
