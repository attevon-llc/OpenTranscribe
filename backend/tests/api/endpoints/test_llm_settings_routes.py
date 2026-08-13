"""Behaviour tests for the LLM-settings routes that reach a provider (issue #431).

Five routes here fetch a **user-supplied URL server-side** (`/ollama/models`,
`/openai-compatible/models`, `/anthropic/models`) or open a provider connection
(`/test-config/{uuid}`, `/test-current`), and one is destructive (`DELETE /all`).
None of them had a test. What is pinned:

* **The SSRF guard fires before any fetch.** ``_assert_safe_llm_endpoint`` refuses a
  private/loopback ``base_url`` with a deliberately non-specific 400 (issue #284
  A0.1). ``LLM_ALLOW_PRIVATE_ENDPOINTS`` is monkeypatched in **both** directions so
  neither outcome depends on this deployment's ``.env``.
* **No test in this module makes a real network call.** The provider transport is
  replaced at the ``aiohttp.ClientSession`` boundary, so the handlers' own parsing,
  URL construction and error mapping still run for real; ids carrying ``stubbed``
  say so. The mock LLM server (``--with-mock-llm``) speaks the *chat completions*
  API, not ``/api/tags`` or Anthropic's ``/v1/models``, so it cannot stand in here.
* **Secrets stay opaque.** The stored key is only ever asserted *structurally* —
  that an ``Authorization``/``x-api-key`` header was produced and is not the
  literal ``None`` — never by value.
* **``DELETE /all`` is caller-scoped** and clears the active-config pointer. It is
  only ever run against configurations the test itself created.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi import status

_BASE = "/api/llm-settings"
_MOD = "app.api.endpoints.llm_settings"
_LOOPBACK = "http://127.0.0.1:11434"
_BLOCKED_DETAIL = (
    "The provided URL could not be used. It must be a publicly reachable http(s) address."
)


def _config_payload(name: str, **over):
    payload = {
        "name": name,
        "provider": "openai",
        "model_name": "gpt-4o-mini",
        "api_key": "sk-routes-fixture-key",  # gitleaks:allow - fake fixture value
        "base_url": "https://api.openai.com/v1",
        "max_tokens": 2000,
        "temperature": "0.3",
    }
    payload.update(over)
    return payload


def _create_config(client, headers, name: str, **over):
    resp = client.post(_BASE, json=_config_payload(name, **over), headers=headers)
    assert resp.status_code == status.HTTP_200_OK
    return resp.json()


def _stub_transport(status_code: int = 200, payload=None, text: str = ""):
    """Replace ``aiohttp.ClientSession`` with a transport that never leaves the process.

    Returns ``(session_class, requests)`` where ``requests`` collects
    ``(url, headers)`` for every GET the handler issued.
    """
    requests: list[tuple[str, dict]] = []

    class _Response:
        status = status_code

        async def json(self):
            if payload is None:
                raise ValueError("no JSON body")
            return payload

        async def text(self):
            return text

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    class _Session:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        def get(self, url, headers=None):
            requests.append((url, dict(headers or {})))
            return _Response()

    return _Session, requests


@pytest.fixture
def allow_private_endpoints(monkeypatch):
    """Opt this deployment into private LLM targets, as a LAN self-hoster would."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "LLM_ALLOW_PRIVATE_ENDPOINTS", True)


@pytest.fixture
def block_private_endpoints(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "LLM_ALLOW_PRIVATE_ENDPOINTS", False)


# ===========================================================================
# Unauthenticated
# ===========================================================================

_ROUTES = [
    ("DELETE", "/all"),
    ("GET", "/anthropic/models"),
    ("GET", "/encryption-test"),
    ("GET", "/ollama/models?base_url=https://example.invalid"),
    ("GET", "/openai-compatible/models?base_url=https://example.invalid"),
    ("POST", "/test-config/00000000-0000-0000-0000-000000000000"),
    ("POST", "/test-current"),
]


@pytest.mark.parametrize(("method", "path"), _ROUTES, ids=[f"{m} {p}" for m, p in _ROUTES])
def test_route_requires_authentication(client, method, path):
    resp = client.request(method, f"{_BASE}{path}")
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED


# ===========================================================================
# SSRF guard — no transport stub, because nothing should be fetched
# ===========================================================================


def test_ollama_models_refuses_a_loopback_base_url(
    client, user_token_headers, block_private_endpoints
):
    resp = client.get(
        f"{_BASE}/ollama/models", params={"base_url": _LOOPBACK}, headers=user_token_headers
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["detail"] == _BLOCKED_DETAIL


def test_openai_compatible_models_refuses_a_loopback_base_url(
    client, user_token_headers, block_private_endpoints
):
    resp = client.get(
        f"{_BASE}/openai-compatible/models",
        params={"base_url": _LOOPBACK},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["detail"] == _BLOCKED_DETAIL


def test_ollama_models_without_base_url_is_422(client, user_token_headers):
    resp = client.get(f"{_BASE}/ollama/models", headers=user_token_headers)
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


# ===========================================================================
# Model discovery against a stubbed transport
# ===========================================================================


def test_ollama_models_parses_a_stubbed_provider_listing(
    client, user_token_headers, allow_private_endpoints
):
    session, requests = _stub_transport(
        payload={"models": [{"name": "llama3.2:latest", "size": 42, "digest": "abc"}]}
    )
    with patch("aiohttp.ClientSession", session):
        resp = client.get(
            f"{_BASE}/ollama/models",
            params={"base_url": f"{_LOOPBACK}/v1"},
            headers=user_token_headers,
        )
    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    assert body["success"] is True
    assert body["total"] == 1
    assert body["models"][0]["name"] == "llama3.2:latest"
    assert body["models"][0]["display_name"] == "llama3.2"
    # The `/v1` suffix is stripped: Ollama's native listing lives at /api/tags.
    assert requests == [(f"{_LOOPBACK}/api/tags", {})]


def test_ollama_models_reports_a_stubbed_provider_http_error(
    client, user_token_headers, allow_private_endpoints
):
    session, _requests = _stub_transport(status_code=503, text="upstream down")
    with patch("aiohttp.ClientSession", session):
        resp = client.get(
            f"{_BASE}/ollama/models", params={"base_url": _LOOPBACK}, headers=user_token_headers
        )
    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    assert body["success"] is False
    assert body["models"] == []
    assert "HTTP 503" in body["message"]
    assert "upstream down" in body["message"]


def test_openai_compatible_models_parses_a_stubbed_openai_listing(
    client, user_token_headers, allow_private_endpoints
):
    session, requests = _stub_transport(
        payload={"object": "list", "data": [{"id": "gpt-4o-mini", "owned_by": "openai"}]}
    )
    with patch("aiohttp.ClientSession", session):
        resp = client.get(
            f"{_BASE}/openai-compatible/models",
            params={"base_url": _LOOPBACK},
            headers=user_token_headers,
        )
    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    assert body["success"] is True
    assert body["models"] == [
        {"name": "gpt-4o-mini", "id": "gpt-4o-mini", "owned_by": "openai", "created": 0}
    ]
    # No key supplied and none stored → no Authorization header is invented.
    assert requests == [(f"{_LOOPBACK}/v1/models", {})]


def test_openai_compatible_models_rejects_a_stubbed_unknown_payload_shape(
    client, user_token_headers, allow_private_endpoints
):
    session, _requests = _stub_transport(payload={"unexpected": True})
    with patch("aiohttp.ClientSession", session):
        resp = client.get(
            f"{_BASE}/openai-compatible/models",
            params={"base_url": _LOOPBACK},
            headers=user_token_headers,
        )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["success"] is False
    assert "Unexpected response format" in resp.json()["message"]


def test_openai_compatible_models_maps_a_stubbed_401_to_a_key_message(
    client, user_token_headers, allow_private_endpoints
):
    session, _requests = _stub_transport(status_code=401)
    with patch("aiohttp.ClientSession", session):
        resp = client.get(
            f"{_BASE}/openai-compatible/models",
            params={"base_url": _LOOPBACK},
            headers=user_token_headers,
        )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["success"] is False
    assert resp.json()["message"] == "Authentication failed: Invalid or missing API key"


def test_openai_compatible_models_authorizes_with_the_stored_key_from_config_id(
    client, user_token_headers, allow_private_endpoints
):
    """``config_id`` resolves and decrypts the stored key. Asserted structurally —
    the plaintext is never compared or echoed."""
    created = _create_config(client, user_token_headers, "StoredKeyDiscovery")
    session, requests = _stub_transport(payload={"data": []})
    with patch("aiohttp.ClientSession", session):
        resp = client.get(
            f"{_BASE}/openai-compatible/models",
            params={"base_url": _LOOPBACK, "config_id": created["uuid"]},
            headers=user_token_headers,
        )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["success"] is True
    assert len(requests) == 1
    sent = requests[0][1]["Authorization"]
    assert sent.startswith("Bearer ")
    assert sent != "Bearer None"
    assert len(sent) > len("Bearer ")


def test_anthropic_models_without_a_key_reports_the_requirement(client, user_token_headers):
    """Returns before any transport is created, so no stub is needed."""
    resp = client.get(f"{_BASE}/anthropic/models", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    assert body["success"] is False
    assert body["models"] == []
    assert body["message"] == "API key is required to fetch Anthropic models"


def test_anthropic_models_parses_a_stubbed_provider_listing(client, user_token_headers):
    session, requests = _stub_transport(
        payload={"data": [{"id": "claude-x", "display_name": "Claude X", "type": "model"}]}
    )
    with patch("aiohttp.ClientSession", session):
        resp = client.get(
            f"{_BASE}/anthropic/models",
            params={"api_key": "sk-ant-fixture"},  # gitleaks:allow - fake fixture value
            headers=user_token_headers,
        )
    assert resp.status_code == status.HTTP_200_OK
    body = resp.json()
    assert body["success"] is True
    assert body["models"] == [
        {"id": "claude-x", "display_name": "Claude X", "created_at": "", "type": "model"}
    ]
    url, sent = requests[0]
    assert url == "https://api.anthropic.com/v1/models"
    assert sent["anthropic-version"] == "2023-06-01"
    assert sent["x-api-key"] != "None"


# ===========================================================================
# DELETE /all — destructive, and only ever over rows this test created
# ===========================================================================


def test_delete_all_removes_only_the_callers_configurations(
    client, user_token_headers, other_user_auth_headers
):
    _create_config(client, user_token_headers, "MineOne")
    _create_config(client, user_token_headers, "MineTwo")
    theirs = _create_config(client, other_user_auth_headers, "TheirsOne")

    # Positive control: the listing really did hold two rows before the wipe.
    seeded = client.get(_BASE, headers=user_token_headers)
    assert seeded.status_code == status.HTTP_200_OK
    assert seeded.json()["total"] == 2

    wiped = client.delete(f"{_BASE}/all", headers=user_token_headers)
    assert wiped.status_code == status.HTTP_200_OK
    assert wiped.json() == {
        "detail": "All 2 configurations deleted successfully. Using system defaults."
    }

    mine = client.get(_BASE, headers=user_token_headers)
    assert mine.status_code == status.HTTP_200_OK
    assert mine.json()["configurations"] == []
    assert mine.json()["total"] == 0
    assert mine.json()["active_configuration_id"] is None

    survivor = client.get(f"{_BASE}/config/{theirs['uuid']}", headers=other_user_auth_headers)
    assert survivor.status_code == status.HTTP_200_OK
    assert survivor.json()["name"] == "TheirsOne"


def test_delete_all_with_nothing_stored_reports_zero(client, user_token_headers):
    resp = client.delete(f"{_BASE}/all", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json() == {
        "detail": "All 0 configurations deleted successfully. Using system defaults."
    }


def test_delete_all_clears_the_active_configuration_pointer(client, user_token_headers):
    """The first config created becomes active; after the wipe ``/test-current`` must
    report no active configuration rather than a dangling id."""
    _create_config(client, user_token_headers, "SoleActive")
    with patch(f"{_MOD}.LLMService.validate_connection", return_value=(True, "reachable")):
        before = client.post(f"{_BASE}/test-current", headers=user_token_headers)
    assert before.status_code == status.HTTP_200_OK

    wiped = client.delete(f"{_BASE}/all", headers=user_token_headers)
    assert wiped.status_code == status.HTTP_200_OK

    after = client.post(f"{_BASE}/test-current", headers=user_token_headers)
    assert after.status_code == status.HTTP_404_NOT_FOUND
    assert "No active LLM configuration" in after.json()["detail"]


# ===========================================================================
# /test-config/{uuid} and /test-current
# ===========================================================================


def test_test_config_unknown_uuid_is_404(client, user_token_headers):
    resp = client.post(
        f"{_BASE}/test-config/11111111-2222-3333-4444-555555555555", headers=user_token_headers
    )
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "LLM configuration not found"


def test_test_config_malformed_uuid_is_400(client, user_token_headers):
    resp = client.post(f"{_BASE}/test-config/not-a-uuid", headers=user_token_headers)
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "Invalid UUID format" in resp.json()["detail"]


def test_test_config_on_another_users_private_config_is_403(
    client, user_token_headers, other_user_auth_headers
):
    created = _create_config(client, user_token_headers, "NotSharedWithYou")
    resp = client.post(f"{_BASE}/test-config/{created['uuid']}", headers=other_user_auth_headers)
    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert resp.json()["detail"] == "Not authorized to access this configuration"


def test_test_config_success_persists_the_result_on_the_row(client, user_token_headers):
    created = _create_config(client, user_token_headers, "PersistOk")
    with patch(f"{_MOD}.LLMService.validate_connection", return_value=(True, "reachable")):
        resp = client.post(f"{_BASE}/test-config/{created['uuid']}", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["success"] is True
    assert resp.json()["status"] == "success"

    reread = client.get(f"{_BASE}/config/{created['uuid']}", headers=user_token_headers)
    assert reread.status_code == status.HTTP_200_OK
    assert reread.json()["test_status"] == "success"
    assert "reachable" in reread.json()["test_message"]
    assert reread.json()["last_tested"] is not None


def test_test_config_failure_persists_the_failed_status(client, user_token_headers):
    created = _create_config(client, user_token_headers, "PersistFail")
    with patch(f"{_MOD}.LLMService.validate_connection", return_value=(False, "refused")):
        resp = client.post(f"{_BASE}/test-config/{created['uuid']}", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["success"] is False

    reread = client.get(f"{_BASE}/config/{created['uuid']}", headers=user_token_headers)
    assert reread.status_code == status.HTTP_200_OK
    assert reread.json()["test_status"] == "failed"
    assert "refused" in reread.json()["test_message"]


def test_test_current_tests_the_active_config_and_records_it(client, user_token_headers):
    created = _create_config(client, user_token_headers, "ActiveOne")
    with patch(f"{_MOD}.LLMService.validate_connection", return_value=(True, "reachable")):
        resp = client.post(f"{_BASE}/test-current", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["status"] == "success"

    reread = client.get(f"{_BASE}/config/{created['uuid']}", headers=user_token_headers)
    assert reread.status_code == status.HTTP_200_OK
    assert reread.json()["test_status"] == "success"


def test_encryption_test_route_reports_a_working_cipher(client, user_token_headers):
    resp = client.get(f"{_BASE}/encryption-test", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json() == {
        "status": "success",
        "message": "Encryption system is working correctly",
    }
