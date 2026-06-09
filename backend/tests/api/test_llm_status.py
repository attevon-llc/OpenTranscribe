"""Characterization tests for the LLM status endpoints.

Covers ``app/api/endpoints/llm_status.py`` mounted at ``/api/llm``:
- ``GET  /llm/status``           (per-user availability shape)
- ``POST /llm/test-connection``  (active-config connection probe)

The former ``GET /llm/providers`` handler was removed (it called a nonexistent
``LLMService`` method and always 500'd, and had no live frontend caller). The
canonical provider catalog lives at ``GET /api/llm-settings/providers`` and is
covered by ``test_llm_settings_endpoints.py``.

External provider reachability is mocked at the service boundary
(``is_llm_available`` / ``LLMService.create_from_settings`` /
``LLMService.validate_connection``) — no real HTTP/SDK calls are made, matching
the wave convention. The unconfigured path needs no mock because tests run with
``LLM_PROVIDER`` unset, so ``create_from_settings`` returns ``None`` naturally.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

from fastapi import status

_BASE = "/api/llm"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_status_unauthorized(client):
    assert client.get(f"{_BASE}/status").status_code == status.HTTP_401_UNAUTHORIZED


def test_providers_route_removed(client, user_token_headers):
    """The dead ``GET /api/llm/providers`` handler was removed; the path no longer
    routes (404). The real catalog is ``GET /api/llm-settings/providers``."""
    assert (
        client.get(f"{_BASE}/providers", headers=user_token_headers).status_code
        == status.HTTP_404_NOT_FOUND
    )


def test_test_connection_unauthorized(client):
    assert client.post(f"{_BASE}/test-connection").status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# GET /status
# ---------------------------------------------------------------------------


def test_status_unconfigured_shape(client, user_token_headers, normal_user):
    """With no LLM configured, status reports unavailable with the canonical shape."""
    resp = client.get(f"{_BASE}/status", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["available"] is False
    assert data["provider"] is None
    assert data["model"] is None
    assert data["user_id"] == normal_user.id
    assert "not available" in data["message"]


def test_status_available_includes_provider_and_model(client, user_token_headers):
    """When available, the configured provider/model are surfaced (mocked boundary)."""
    fake_service = MagicMock()
    fake_service.config.provider.value = "openai"
    fake_service.config.model = "gpt-4o-mini"

    with (
        patch("app.api.endpoints.llm_status.is_llm_available", return_value=True),
        patch(
            "app.api.endpoints.llm_status.LLMService.create_from_settings",
            return_value=fake_service,
        ),
    ):
        resp = client.get(f"{_BASE}/status", headers=user_token_headers)

    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["available"] is True
    assert data["provider"] == "openai"
    assert data["model"] == "gpt-4o-mini"
    fake_service.close.assert_called_once()


def test_status_available_but_unconfigured_service(client, user_token_headers):
    """is_llm_available True but create_from_settings None → 'not configured' branch."""
    with (
        patch("app.api.endpoints.llm_status.is_llm_available", return_value=True),
        patch(
            "app.api.endpoints.llm_status.LLMService.create_from_settings",
            return_value=None,
        ),
    ):
        resp = client.get(f"{_BASE}/status", headers=user_token_headers)

    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["available"] is True
    assert data["provider"] is None
    assert data["message"] == "LLM service is not configured"


def test_status_swallows_availability_error(client, user_token_headers):
    """An unexpected error from the availability check degrades to available=False."""
    with patch(
        "app.api.endpoints.llm_status.is_llm_available",
        side_effect=RuntimeError("boom"),
    ):
        resp = client.get(f"{_BASE}/status", headers=user_token_headers)

    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["available"] is False
    assert data["provider"] is None


# ---------------------------------------------------------------------------
# POST /test-connection
# ---------------------------------------------------------------------------


def test_test_connection_unconfigured(client, user_token_headers):
    """No active config → success False with the canonical 'not configured' body."""
    resp = client.post(f"{_BASE}/test-connection", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["success"] is False
    assert data["message"] == "No LLM service configured"
    assert data["details"] == "Please configure an LLM provider in your settings"


def test_test_connection_success(client, user_token_headers):
    """A configured service whose validation succeeds returns success True."""
    fake_service = MagicMock()
    fake_service.config.provider.value = "ollama"
    fake_service.config.model = "llama3.2:latest"
    fake_service.validate_connection.return_value = (True, "Connection successful")

    with patch(
        "app.api.endpoints.llm_status.LLMService.create_from_settings",
        return_value=fake_service,
    ):
        resp = client.post(f"{_BASE}/test-connection", headers=user_token_headers)

    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["success"] is True
    assert data["message"] == "Connection successful"
    assert data["provider"] == "ollama"
    assert data["model"] == "llama3.2:latest"
    fake_service.close.assert_called_once()


def test_test_connection_failure(client, user_token_headers):
    """A configured service whose validation fails returns success False (still 200)."""
    fake_service = MagicMock()
    fake_service.config.provider.value = "vllm"
    fake_service.config.model = "gpt-oss"
    fake_service.validate_connection.return_value = (False, "unreachable")

    with patch(
        "app.api.endpoints.llm_status.LLMService.create_from_settings",
        return_value=fake_service,
    ):
        resp = client.post(f"{_BASE}/test-connection", headers=user_token_headers)

    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["success"] is False
    assert data["message"] == "unreachable"
