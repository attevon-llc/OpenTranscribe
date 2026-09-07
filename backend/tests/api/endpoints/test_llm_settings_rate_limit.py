"""Rate-limit coverage for the LLM connection-test / model-discovery handlers (issue #676).

`POST /llm-settings/test`, `GET /llm-settings/ollama/models` and
`GET /llm-settings/openai-compatible/models` make a server-side outbound request to a
caller-supplied `base_url`, gated only on `get_current_active_user` (no admin gate) on a
router that had **no rate limit at all** — an authenticated user could drive unbounded
outbound requests from the backend container. The SSRF *target* (private IP / DNS
rebinding / redirect) was already hardened before this issue
(`_assert_safe_llm_endpoint` / `_pin_llm_endpoint` in `app.api.endpoints.llm_settings`,
backed by `app.utils.url_validation`'s DNS-pinning); what this closes is the *volume*
bound. See the issue for the full history.

These tests flip the module-level `limiter` (normally disabled under
`RATE_LIMIT_ENABLED=false` in `tests/conftest.py`) on for the duration of the test only,
and reset its storage on the way out so no count leaks into another test file.
"""

from __future__ import annotations

import pytest
from fastapi import status

from app.auth.rate_limit import limiter
from app.core.config import settings

_BASE = "/api/llm-settings"
_LOOPBACK = "http://127.0.0.1:11434"


@pytest.fixture
def rate_limiting_enabled():
    """Turn the real slowapi limiter on for one test, then clean up after it.

    `RATE_LIMIT_ENABLED=false` in `tests/conftest.py` builds the module-level
    `limiter` singleton with `enabled=False` at import time — every other test in
    the suite relies on that so it isn't rate-limited by accident. `Limiter.enabled`
    is a plain mutable attribute (not baked into the decorator), so it can be
    flipped per-test; `limiter.reset()` clears the storage bucket the flipped-on
    window created so the next test (rate-limited or not) starts clean.
    """
    was_enabled = limiter.enabled
    limiter.enabled = True
    try:
        yield
    finally:
        limiter.enabled = was_enabled
        try:
            limiter.reset()
        except Exception:
            # `.reset()` talks to the configured Redis storage directly, unlike a
            # normal rate-limit check (which degrades to in-memory on its own, see
            # `_create_limiter`'s docstring). Host test runs routinely have no
            # Redis reachable at `settings.REDIS_URL` (dev's is auth'd on a
            # non-default port; conftest deliberately does not wire it up — see
            # its Redis note). Deployments where the limiter really did run
            # against Redis during the test still had their state cleared by the
            # *successful* branch above; this only guards the common "no Redis on
            # the host" case from failing an otherwise-passing test at teardown.
            pass


def _hit_ollama_models(client, headers):
    return client.get(
        f"{_BASE}/ollama/models",
        params={"base_url": _LOOPBACK},
        headers=headers,
    )


def test_ollama_models_is_rate_limited_per_user(client, user_token_headers, rate_limiting_enabled):
    """The Nth+1 request within the window is refused with 429, not fetched.

    Each call 400s on the SSRF guard (loopback, private endpoints not allowed) without
    ever reaching the network — so this isolates the rate limit itself. The limit
    string is baked into the `@limiter.limit(...)` decorator at import time from
    `RATE_LIMIT_LLM_OUTBOUND_PER_MINUTE`'s coded default (10/minute), so the test
    exercises that default rather than a monkeypatched value.
    """
    limit = settings.RATE_LIMIT_LLM_OUTBOUND_PER_MINUTE
    responses = [_hit_ollama_models(client, user_token_headers) for _ in range(limit)]
    for resp in responses:
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text

    blocked = _hit_ollama_models(client, user_token_headers)
    assert blocked.status_code == status.HTTP_429_TOO_MANY_REQUESTS, blocked.text


def test_openai_compatible_models_is_rate_limited_per_user(
    client, user_token_headers, rate_limiting_enabled
):
    limit = settings.RATE_LIMIT_LLM_OUTBOUND_PER_MINUTE
    for _ in range(limit):
        resp = client.get(
            f"{_BASE}/openai-compatible/models",
            params={"base_url": _LOOPBACK},
            headers=user_token_headers,
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text

    blocked = client.get(
        f"{_BASE}/openai-compatible/models",
        params={"base_url": _LOOPBACK},
        headers=user_token_headers,
    )
    assert blocked.status_code == status.HTTP_429_TOO_MANY_REQUESTS, blocked.text


def test_llm_test_connection_is_rate_limited_per_user(
    client, user_token_headers, rate_limiting_enabled
):
    """`POST /test` against a loopback `base_url` is refused by `_assert_safe_llm_endpoint`
    before any outbound call, exercising the same limiter without a real network hop.
    """
    limit = settings.RATE_LIMIT_LLM_OUTBOUND_PER_MINUTE
    payload = {
        "provider": "openai",
        "model_name": "does-not-matter",
        "api_key": "sk-fake",
        "base_url": _LOOPBACK,
    }
    for _ in range(limit):
        resp = client.post(f"{_BASE}/test", headers=user_token_headers, json=payload)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text

    blocked = client.post(f"{_BASE}/test", headers=user_token_headers, json=payload)
    assert blocked.status_code == status.HTTP_429_TOO_MANY_REQUESTS, blocked.text


def test_rate_limit_bucket_is_keyed_per_user_not_globally(
    client, user_token_headers, admin_token_headers, rate_limiting_enabled
):
    """A second authenticated user is not punished by the first user's usage.

    Both requests originate from the same TestClient (same IP), so this only passes
    if the bucket key is the resolved user id (`user_or_ip_key`) rather than the
    shared IP — the exact gap #676 flags behind an unconfigured
    `RATE_LIMIT_TRUSTED_PROXIES` (issue #668).
    """
    limit = settings.RATE_LIMIT_LLM_OUTBOUND_PER_MINUTE
    for _ in range(limit):
        resp = _hit_ollama_models(client, user_token_headers)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST, resp.text

    # user_token_headers' bucket is now exhausted...
    assert _hit_ollama_models(client, user_token_headers).status_code == (
        status.HTTP_429_TOO_MANY_REQUESTS
    )

    # ...but a different authenticated user still gets served.
    other_resp = _hit_ollama_models(client, admin_token_headers)
    assert other_resp.status_code == status.HTTP_400_BAD_REQUEST, other_resp.text


def test_llm_allow_private_endpoints_flag_still_works_under_rate_limiting(
    client, user_token_headers, rate_limiting_enabled, monkeypatch
):
    """The `LLM_ALLOW_PRIVATE_ENDPOINTS` escape hatch for self-hosted local LLMs is
    untouched by adding a rate limit — a single allowed request is not rejected.
    """
    monkeypatch.setattr(settings, "LLM_ALLOW_PRIVATE_ENDPOINTS", True)

    from unittest.mock import patch

    class _Response:
        status = 200

        async def json(self):
            return {"models": []}

        async def text(self):
            return ""

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    class _Session:
        def __init__(self, *_a, **_kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        def get(self, *_a, **_kw):
            return _Response()

    with patch("aiohttp.ClientSession", _Session):
        resp = _hit_ollama_models(client, user_token_headers)
    assert resp.status_code == status.HTTP_200_OK, resp.text
    assert resp.json()["success"] is True
