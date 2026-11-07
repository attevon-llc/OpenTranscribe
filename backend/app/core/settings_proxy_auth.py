"""Trusted-header (reverse-proxy) authentication settings.

Split out of ``app.core.config`` (which was at its size cap) as one cohesive concern.
``Settings`` inherits these fields, so ``settings.PROXY_*`` and the environment variable
names are unchanged. The env source, ``env_ignore_empty`` and ``extra="ignore"`` come from
``Settings``' own ``model_config``; this class is never instantiated on its own.
"""

from pydantic_settings import BaseSettings


class ProxyAuthSettings(BaseSettings):
    # An authenticating reverse proxy asserts the identity in a header. Everything
    # here is also settable in the admin UI (category "proxy"), which wins; these are
    # the .env fallbacks and, for PROXY_ENABLED/PROXY_TRUSTED_PROXIES, what the
    # startup guard in main.py can see before a database session exists.
    PROXY_ENABLED: bool = False
    # Comma-separated IPs/CIDRs allowed to assert identity headers. EMPTY MEANS
    # REFUSE EVERY ASSERTION — the fail-closed state the feature is built around.
    PROXY_TRUSTED_PROXIES: str = ""
    PROXY_EMAIL_HEADER: str = "X-Forwarded-Email"
    PROXY_NAME_HEADER: str = "X-Forwarded-User"
    # No default: a groups header drives in-app group membership, so reading one
    # nobody configured would hand a proxy privilege it was never granted.
    PROXY_GROUPS_HEADER: str = ""
    PROXY_GROUPS_SEPARATOR: str = ","
    # Opt-in, and capped at 'admin' by app/auth/proxy/assertion.py. Empty = off.
    PROXY_ROLE_HEADER: str = ""
    # Optional defence in depth: a constant-time-compared value the proxy must send
    # in X-OpenTranscribe-Proxy-Secret, so an allowlisted-but-misconfigured proxy is
    # not by itself sufficient for takeover.
    PROXY_SHARED_SECRET: str = ""
    # Comma-separated email domain allowlist. Empty admits every domain.
    PROXY_ALLOWED_DOMAINS: str = ""
    PROXY_JIT_PROVISIONING: bool = True
