"""Trusted-proxy client-IP resolution (#284 A0.5).

Three call sites disagreed about who a request came from:

* the rate limiter honoured `RATE_LIMIT_TRUSTED_PROXIES` but took the FIRST
  `X-Forwarded-For` entry — client-controlled when more than one proxy is in front;
* `endpoints/auth.py::_get_client_info` used `request.client.host`, so audited logins
  recorded the reverse proxy instead of the user;
* `middleware/audit.py` trusted `X-Forwarded-For` and `X-Real-IP` **unconditionally**, so
  any client could forge the address in the security audit trail.

All three now share `app.utils.client_ip.resolve_client_ip`.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest


def _request(peer: str | None, **headers):
    """Minimal Request stand-in exposing .client and .headers."""
    return SimpleNamespace(
        client=SimpleNamespace(host=peer) if peer else None,
        headers={k.replace("_", "-"): v for k, v in headers.items()},
    )


@pytest.fixture(autouse=True)
def _restore_resolver_module():
    """Reload the resolver back to the real config after each test.

    `_module_with_proxies` reloads `app.utils.client_ip` so its module-level
    `_TRUSTED_NETWORKS` picks up a patched allowlist. Without restoring it, the last
    test's fake proxy list would leak into every other test that resolves a client IP.
    Note it reloads only `client_ip`, never `app.core.config` — reloading config would
    replace the `settings` OBJECT and desync every module holding a reference to it.
    """
    yield
    import app.utils.client_ip as client_ip

    importlib.reload(client_ip)


def _module_with_proxies(monkeypatch, trusted: str):
    """Reload the resolver with a given trusted-proxy allowlist."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "RATE_LIMIT_TRUSTED_PROXIES", trusted)
    import app.utils.client_ip as client_ip

    return importlib.reload(client_ip)


# ── No trusted proxies: forwarding headers must be ignored entirely ──────────────


def test_forwarded_header_ignored_without_trusted_proxies(monkeypatch):
    """The spoofing fix: an unconfigured deployment must not believe the header."""
    mod = _module_with_proxies(monkeypatch, "")
    req = _request("203.0.113.9", X_Forwarded_For="1.2.3.4")

    assert mod.resolve_client_ip(req) == "203.0.113.9"


def test_real_ip_header_ignored_without_trusted_proxies(monkeypatch):
    mod = _module_with_proxies(monkeypatch, "")
    req = _request("203.0.113.9", X_Real_IP="1.2.3.4")

    assert mod.resolve_client_ip(req) == "203.0.113.9"


def test_direct_peer_used_when_no_headers(monkeypatch):
    mod = _module_with_proxies(monkeypatch, "")
    assert mod.resolve_client_ip(_request("198.51.100.7")) == "198.51.100.7"


def test_unknown_when_no_client(monkeypatch):
    mod = _module_with_proxies(monkeypatch, "")
    assert mod.resolve_client_ip(_request(None)) == "unknown"


# ── With trusted proxies ─────────────────────────────────────────────────────────


def test_forwarded_honored_from_trusted_proxy(monkeypatch):
    mod = _module_with_proxies(monkeypatch, "10.0.0.0/8")
    req = _request("10.0.0.5", X_Forwarded_For="203.0.113.9")

    assert mod.resolve_client_ip(req) == "203.0.113.9"


def test_forwarded_ignored_from_untrusted_peer(monkeypatch):
    """A direct client sending X-Forwarded-For must not pick its own identity."""
    mod = _module_with_proxies(monkeypatch, "10.0.0.0/8")
    req = _request("203.0.113.50", X_Forwarded_For="1.2.3.4")

    assert mod.resolve_client_ip(req) == "203.0.113.50"


def test_client_cannot_spoof_by_prepending_entries(monkeypatch):
    """The core A0.5 fix.

    Chain is `client-supplied, real-client, proxy2`. Taking the FIRST entry returns the
    forged value; walking right-to-left past trusted hops returns the real client.
    """
    mod = _module_with_proxies(monkeypatch, "10.0.0.0/8")
    req = _request("10.0.0.5", X_Forwarded_For="1.2.3.4, 203.0.113.9, 10.0.0.9")

    assert mod.resolve_client_ip(req) == "203.0.113.9"


def test_multiple_trusted_hops_are_skipped(monkeypatch):
    mod = _module_with_proxies(monkeypatch, "10.0.0.0/8,172.16.0.0/12")
    req = _request("10.0.0.5", X_Forwarded_For="198.51.100.7, 172.16.0.1, 10.0.0.9")

    assert mod.resolve_client_ip(req) == "198.51.100.7"


def test_all_trusted_chain_falls_back_to_peer(monkeypatch):
    """A request that genuinely originated inside the trusted set."""
    mod = _module_with_proxies(monkeypatch, "10.0.0.0/8")
    req = _request("10.0.0.5", X_Forwarded_For="10.0.0.7, 10.0.0.9")

    assert mod.resolve_client_ip(req) == "10.0.0.5"


def test_real_ip_honored_only_from_trusted_proxy(monkeypatch):
    mod = _module_with_proxies(monkeypatch, "10.0.0.0/8")
    req = _request("10.0.0.5", X_Real_IP="203.0.113.9")

    assert mod.resolve_client_ip(req) == "203.0.113.9"


def test_malformed_forwarded_entry_falls_back_to_peer(monkeypatch):
    mod = _module_with_proxies(monkeypatch, "10.0.0.0/8")
    req = _request("10.0.0.5", X_Forwarded_For="not-an-ip")

    assert mod.resolve_client_ip(req) == "10.0.0.5"


def test_ipv6_proxy_and_client(monkeypatch):
    mod = _module_with_proxies(monkeypatch, "2001:db8::/32")
    req = _request("2001:db8::1", X_Forwarded_For="2606:4700::1111")

    assert mod.resolve_client_ip(req) == "2606:4700::1111"


@pytest.mark.parametrize("bad", ["not-a-cidr", "999.999.999.999", ""])
def test_invalid_trusted_proxy_entries_are_skipped(monkeypatch, bad):
    """A typo in the allowlist must not crash startup or trust everything."""
    mod = _module_with_proxies(monkeypatch, f"{bad},10.0.0.0/8")
    req = _request("10.0.0.5", X_Forwarded_For="203.0.113.9")

    assert mod.resolve_client_ip(req) == "203.0.113.9"


def test_single_host_entry_without_cidr(monkeypatch):
    mod = _module_with_proxies(monkeypatch, "10.0.0.5")
    req = _request("10.0.0.5", X_Forwarded_For="203.0.113.9")

    assert mod.resolve_client_ip(req) == "203.0.113.9"


# ── All three consumers share the resolver ───────────────────────────────────────


def test_audit_middleware_uses_shared_resolver():
    import inspect

    from app.middleware.audit import AuditMiddleware

    source = inspect.getsource(AuditMiddleware._get_client_ip)
    assert "resolve_client_ip" in source

    # Check the code, not the docstring — which legitimately names the header while
    # explaining the bug this replaced.
    body = source.split('"""')[-1]
    assert "request.headers" not in body, "audit must not re-implement header parsing"


def test_auth_client_info_uses_shared_resolver():
    import inspect

    from app.api.endpoints.auth import _get_client_info

    assert "resolve_client_ip" in inspect.getsource(_get_client_info)


def test_rate_limiter_uses_shared_resolver():
    import inspect

    from app.auth import rate_limit

    assert "resolve_client_ip" in inspect.getsource(rate_limit._get_key_func)
