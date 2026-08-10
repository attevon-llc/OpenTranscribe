"""Trusted-header (reverse-proxy) authentication — ``auth_type='proxy'``.

An authenticating reverse proxy (oauth2-proxy, Authelia, Cloudflare Access, an
enterprise SSO gateway) terminates authentication and asserts the resulting identity
in a request header. This package decides whether to believe it.

Layout, one module per stage:

- :mod:`config` — ``ProxyConfig``, resolved DB > .env > coded default.
- :mod:`assertion` — trust check, shared secret, admission, role cap. Produces a
  ``ProxyAssertion`` or refuses.
- :mod:`provisioning` — ``sync_proxy_user_to_db``: JIT creation, account linking,
  approval state, and reconciliation through the shared IdP group mapper.

The trust check itself lives one level up in :mod:`app.auth.header_trust`, because
PKI's ``pki_mode='header'`` is the same decision about a subject DN instead of an
email address. One implementation, two callers.
"""

from app.auth.proxy.assertion import REFUSAL_DETAIL
from app.auth.proxy.assertion import ProxyAssertion
from app.auth.proxy.assertion import extract_proxy_assertion
from app.auth.proxy.config import ProxyConfig
from app.auth.proxy.provisioning import sync_proxy_user_to_db

__all__ = [
    "REFUSAL_DETAIL",
    "ProxyAssertion",
    "ProxyConfig",
    "extract_proxy_assertion",
    "sync_proxy_user_to_db",
]
