"""Gap-filling characterization tests for the user LLM settings endpoints.

The existing ``backend/tests/test_llm_settings.py`` is gated behind
``RUN_LLM_TESTS=true`` (it exercises model/schema/service internals with a
mocked DB) and does NOT run in the default ungated suite. The ownership/authz
``require_resource_owner`` details for this module are already snapshot-pinned in
``test_ownership_contracts.py``. This suite adds the ungated, live-DB coverage
the wave brief calls for: provider CATALOG + status/list shapes, create/test
VALIDATION (422/400), malformed/unknown UUID handling, the encryption-failure
``500`` paths (mocked at the encryption boundary), the connection-test envelope
(``validate_connection`` mocked — no real provider call), and the write-only
API-key contract.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

from fastapi import status

_BASE = "/api/llm-settings"
_MOD = "app.api.endpoints.llm_settings"


def _create_payload(name="Gap Cfg", **over):
    payload = {
        "name": name,
        "provider": "openai",
        "model_name": "gpt-4o-mini",
        "api_key": "sk-fixture-key-123",  # gitleaks:allow - fake fixture key
        "base_url": "https://api.openai.com/v1",
        "max_tokens": 2000,
        "temperature": "0.3",
    }
    payload.update(over)
    return payload


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_list_requires_auth(client):
    assert client.get(_BASE).status_code == status.HTTP_401_UNAUTHORIZED


def test_status_requires_auth(client):
    assert client.get(f"{_BASE}/status").status_code == status.HTTP_401_UNAUTHORIZED


def test_providers_no_auth_dependency(client):
    """COMMITTED BEHAVIOR: ``get_supported_providers`` declares no ``current_user``
    dependency, so the static catalog is served WITHOUT authentication (the
    capability gate does not add an auth dependency). Pinned so a refactor can't
    silently change the auth posture of this endpoint."""
    assert client.get(f"{_BASE}/providers").status_code == status.HTTP_200_OK


# ---------------------------------------------------------------------------
# Provider catalog
# ---------------------------------------------------------------------------


def test_providers_catalog_shape(client, user_token_headers):
    resp = client.get(f"{_BASE}/providers", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    providers = resp.json()["providers"]
    ids = {p["provider"] for p in providers}
    assert {"openai", "vllm", "ollama", "anthropic", "openrouter"} <= ids
    for p in providers:
        for key in ("default_model", "requires_api_key", "supports_custom_url", "description"):
            assert key in p


# ---------------------------------------------------------------------------
# Status / list (empty)
# ---------------------------------------------------------------------------


def test_status_empty(client, user_token_headers):
    data = client.get(f"{_BASE}/status", headers=user_token_headers).json()
    assert data["has_settings"] is False
    assert data["using_system_default"] is True
    assert data["total_configurations"] == 0


def test_list_empty(client, user_token_headers):
    data = client.get(_BASE, headers=user_token_headers).json()
    assert data["configurations"] == []
    assert data["total"] == 0
    assert data["active_configuration_id"] is None


# ---------------------------------------------------------------------------
# Create validation + duplicate
# ---------------------------------------------------------------------------


def test_create_invalid_provider_is_422(client, user_token_headers):
    resp = client.post(
        _BASE,
        json={"name": "x", "provider": "bogus", "model_name": "m"},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_create_bad_max_tokens_is_422(client, user_token_headers):
    resp = client.post(_BASE, json=_create_payload(max_tokens=0), headers=user_token_headers)
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_create_bad_temperature_is_422(client, user_token_headers):
    resp = client.post(_BASE, json=_create_payload(temperature="9.0"), headers=user_token_headers)
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_create_duplicate_name_is_400(client, user_token_headers):
    first = client.post(_BASE, json=_create_payload(name="Dupe"), headers=user_token_headers)
    assert first.status_code == status.HTTP_200_OK
    dup = client.post(_BASE, json=_create_payload(name="Dupe"), headers=user_token_headers)
    assert dup.status_code == status.HTTP_400_BAD_REQUEST
    assert "already exists" in dup.json()["detail"]


def test_create_key_never_returned(client, user_token_headers):
    resp = client.post(_BASE, json=_create_payload(name="KeyHidden"), headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["has_api_key"] is True
    assert "sk-fixture-key-123" not in resp.text


# ---------------------------------------------------------------------------
# Encryption-failure paths (mocked boundary)
# ---------------------------------------------------------------------------


def test_create_encryption_unavailable_500(client, user_token_headers):
    with patch(f"{_MOD}.test_encryption", return_value=False):
        resp = client.post(_BASE, json=_create_payload(name="EncDown"), headers=user_token_headers)
    assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert resp.json()["detail"] == "Encryption system is not working properly"
    assert "sk-fixture-key-123" not in resp.text


def test_create_encrypt_none_500(client, user_token_headers):
    with (
        patch(f"{_MOD}.test_encryption", return_value=True),
        patch(f"{_MOD}.encrypt_api_key", return_value=None),
    ):
        resp = client.post(_BASE, json=_create_payload(name="EncNull"), headers=user_token_headers)
    assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert resp.json()["detail"] == "Failed to encrypt API key"


def test_encryption_test_endpoint_ok(client, user_token_headers):
    resp = client.get(f"{_BASE}/encryption-test", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["status"] == "success"


def test_encryption_test_endpoint_failure_500(client, user_token_headers):
    with patch(f"{_MOD}.test_encryption", return_value=False):
        resp = client.get(f"{_BASE}/encryption-test", headers=user_token_headers)
    assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert resp.json()["detail"] == "Encryption system is not working properly"


# ---------------------------------------------------------------------------
# UUID handling (config_uuid is a plain str → get_by_uuid validates the format)
# ---------------------------------------------------------------------------


def test_get_config_malformed_uuid_400(client, user_token_headers):
    resp = client.get(f"{_BASE}/config/not-a-uuid", headers=user_token_headers)
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "Invalid UUID format" in resp.json()["detail"]


def test_get_config_unknown_uuid_404(client, user_token_headers):
    resp = client.get(f"{_BASE}/config/{uuid.uuid4()}", headers=user_token_headers)
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert resp.json()["detail"] == "LLM configuration not found"


# ---------------------------------------------------------------------------
# API-key retrieval (write-only contract)
# ---------------------------------------------------------------------------


def test_get_api_key_owner_round_trip(client, user_token_headers):
    """The owner can retrieve their own decrypted key via the dedicated endpoint
    (the only surface that returns key material) — and the round-trip matches."""
    created = client.post(
        _BASE, json=_create_payload(name="OwnerKey"), headers=user_token_headers
    ).json()
    resp = client.get(f"{_BASE}/config/{created['uuid']}/api-key", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["api_key"] == "sk-fixture-key-123"


def test_get_api_key_other_user_403(client, user_token_headers, other_user_auth_headers):
    created = client.post(
        _BASE, json=_create_payload(name="NotYours"), headers=user_token_headers
    ).json()
    resp = client.get(f"{_BASE}/config/{created['uuid']}/api-key", headers=other_user_auth_headers)
    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert resp.json()["detail"] == "Not authorized to access this configuration"


# ---------------------------------------------------------------------------
# Connection test (validate_connection mocked — no real provider call)
# ---------------------------------------------------------------------------


def test_test_connection_invalid_provider_is_422(client, user_token_headers):
    resp = client.post(
        f"{_BASE}/test",
        json={"provider": "bogus", "model_name": "m"},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_test_connection_success_envelope(client, user_token_headers):
    with patch(f"{_MOD}.LLMService.validate_connection", return_value=(True, "ok")):
        resp = client.post(
            f"{_BASE}/test",
            json={"provider": "openai", "model_name": "gpt-4o-mini", "api_key": "sk-x"},
            headers=user_token_headers,
        )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["success"] is True
    assert data["status"] == "success"
    assert "response_time_ms" in data


def test_test_connection_failure_envelope(client, user_token_headers):
    with patch(f"{_MOD}.LLMService.validate_connection", return_value=(False, "nope")):
        resp = client.post(
            f"{_BASE}/test",
            json={"provider": "openai", "model_name": "gpt-4o-mini", "api_key": "sk-x"},
            headers=user_token_headers,
        )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["success"] is False
    assert data["status"] == "failed"


def test_test_current_no_active_404(client, user_token_headers):
    resp = client.post(f"{_BASE}/test-current", headers=user_token_headers)
    assert resp.status_code == status.HTTP_404_NOT_FOUND
    assert "No active LLM configuration" in resp.json()["detail"]
