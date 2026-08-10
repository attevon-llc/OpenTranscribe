"""Gap-filling characterization tests for the ASR settings endpoints.

The existing ``backend/tests/test_asr_settings.py`` (68 tests, ungated) already
covers schema validation, the provider catalog, CRUD, activation, sharing,
factory resolution, connection testing, status, capabilities and admin
local-model set/get. This suite deliberately AVOIDS that overlap and pins the
remaining contract surface:

- ``401`` posture across the endpoint family (the existing suite only spot-checks POST).
- Malformed-UUID coercion (routes declare ``config_uuid: UUID`` → FastAPI 422).
- Encryption-failure / decrypt-failure ``500`` paths (mocked at the encryption
  boundary — the 9 sanitized error strings called out in the wave brief).
- The GPU-worker restart admin success ENVELOPE, with Celery control mocked so
  no real shutdown is broadcast to the live stack's GPU worker.

API keys are never echoed: the encrypted-credential endpoints are write-only and
that contract is re-pinned here against the mocked error paths.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock
from unittest.mock import patch

from fastapi import status

_BASE = "/api/asr-settings"
_ENC = "app.api.endpoints.asr_settings"


def _create_cloud_config(client, headers, *, name="Enc Cfg"):
    return client.post(
        _BASE,
        json={
            "name": name,
            "provider": "deepgram",
            "model_name": "nova-3",
            "api_key": "dg_fixture_key_xyz",
        },
        headers=headers,
    )


# ---------------------------------------------------------------------------
# 401 posture across the family
# ---------------------------------------------------------------------------


def test_providers_requires_auth(client):
    assert client.get(f"{_BASE}/providers").status_code == status.HTTP_401_UNAUTHORIZED


def test_local_models_requires_auth(client):
    assert client.get(f"{_BASE}/local-models").status_code == status.HTTP_401_UNAUTHORIZED


def test_status_requires_auth(client):
    assert client.get(f"{_BASE}/status").status_code == status.HTTP_401_UNAUTHORIZED


def test_list_requires_auth(client):
    assert client.get(_BASE).status_code == status.HTTP_401_UNAUTHORIZED


def test_local_model_active_requires_auth(client):
    assert client.get(f"{_BASE}/local-model/active").status_code == status.HTTP_401_UNAUTHORIZED


def test_restart_requires_auth(client):
    assert client.post(f"{_BASE}/local-model/restart").status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# Malformed UUID coercion (UUID-typed path params)
# ---------------------------------------------------------------------------


def test_get_config_malformed_uuid_422(client, user_token_headers):
    resp = client.get(f"{_BASE}/config/not-a-uuid", headers=user_token_headers)
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_get_api_key_malformed_uuid_422(client, user_token_headers):
    resp = client.get(f"{_BASE}/config/not-a-uuid/api-key", headers=user_token_headers)
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_get_config_unknown_uuid_404(client, user_token_headers):
    resp = client.get(f"{_BASE}/config/{uuid.uuid4()}", headers=user_token_headers)
    assert resp.status_code == status.HTTP_404_NOT_FOUND


def test_api_key_none_for_local_config(client, user_token_headers):
    """A local config has no key → endpoint returns {'api_key': None} (not 500)."""
    created = client.post(
        _BASE,
        json={"name": "Local NoKey", "provider": "local", "model_name": "tiny"},
        headers=user_token_headers,
    ).json()
    resp = client.get(f"{_BASE}/config/{created['uuid']}/api-key", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json() == {"api_key": None}


# ---------------------------------------------------------------------------
# Encryption-failure paths (mocked at the encryption boundary)
# ---------------------------------------------------------------------------


def test_create_encryption_unavailable_500(client, user_token_headers):
    """test_encryption() False → 'Encryption system is not working properly'."""
    with patch(f"{_ENC}.test_encryption", return_value=False):
        resp = _create_cloud_config(client, user_token_headers, name="EncDown")
    assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert resp.json()["detail"] == "Encryption system is not working properly"
    # Raw key never leaks into the error response
    assert "dg_fixture_key_xyz" not in resp.text


def test_create_encrypt_returns_none_500(client, user_token_headers):
    """encrypt_api_key() returning None → 'Failed to encrypt API key'."""
    with (
        patch(f"{_ENC}.test_encryption", return_value=True),
        patch(f"{_ENC}.encrypt_api_key", return_value=None),
    ):
        resp = _create_cloud_config(client, user_token_headers, name="EncNull")
    assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert resp.json()["detail"] == "Failed to encrypt API key"
    assert "dg_fixture_key_xyz" not in resp.text


def test_get_api_key_decrypt_failure_500(client, user_token_headers):
    """A stored key that fails to decrypt → 'Failed to decrypt API key' (owner path)."""
    created = _create_cloud_config(client, user_token_headers, name="DecryptFail").json()
    with patch(f"{_ENC}.decrypt_api_key", return_value=None):
        resp = client.get(f"{_BASE}/config/{created['uuid']}/api-key", headers=user_token_headers)
    assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert resp.json()["detail"] == "Failed to decrypt API key"


def test_test_saved_config_decrypt_failure_500(client, user_token_headers):
    """test-config decrypt failure → 'Failed to decrypt stored API key'."""
    created = _create_cloud_config(client, user_token_headers, name="DecryptStored").json()
    with patch(f"{_ENC}.decrypt_api_key", return_value=None):
        resp = client.post(f"{_BASE}/test-config/{created['uuid']}", headers=user_token_headers)
    assert resp.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
    assert resp.json()["detail"] == "Failed to decrypt stored API key"


# ---------------------------------------------------------------------------
# GPU-worker restart — admin success envelope (Celery control mocked)
# ---------------------------------------------------------------------------


def test_restart_admin_no_workers_envelope(client, super_admin_token_headers):
    """When inspect finds no GPU workers, a best-effort broadcast is sent and the
    envelope reports an empty worker list. Celery control is fully mocked so no
    real shutdown reaches the live stack."""
    fake_inspector = MagicMock()
    fake_inspector.ping.return_value = {}  # no workers
    fake_control = MagicMock()
    fake_control.inspect.return_value = fake_inspector

    with patch("app.core.celery.celery_app.control", fake_control):
        resp = client.post(f"{_BASE}/local-model/restart", headers=super_admin_token_headers)

    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["status"] == "restart_signaled"
    assert data["workers"] == []
    assert "model" in data
    fake_control.broadcast.assert_called_once_with("shutdown")


def test_restart_admin_with_workers_envelope(client, super_admin_token_headers):
    """When GPU workers are present, shutdown is targeted at them only and the
    envelope reports the discovered worker names + active task count."""
    fake_inspector = MagicMock()
    fake_inspector.ping.return_value = {"celery@gpu-worker-1": {"ok": "pong"}}
    fake_inspector.active.return_value = {"celery@gpu-worker-1": []}
    fake_control = MagicMock()
    fake_control.inspect.return_value = fake_inspector

    with patch("app.core.celery.celery_app.control", fake_control):
        resp = client.post(f"{_BASE}/local-model/restart", headers=super_admin_token_headers)

    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["status"] == "restart_signaled"
    assert data["workers"] == ["celery@gpu-worker-1"]
    assert data["active_tasks"] == 0
    fake_control.broadcast.assert_called_once_with("shutdown", destination=["celery@gpu-worker-1"])
