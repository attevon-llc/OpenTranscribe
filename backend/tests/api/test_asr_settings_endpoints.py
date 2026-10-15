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


def test_get_config_unknown_uuid_404(client, user_token_headers):
    resp = client.get(f"{_BASE}/config/{uuid.uuid4()}", headers=user_token_headers)
    assert resp.status_code == status.HTTP_404_NOT_FOUND


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


def test_the_key_reveal_route_is_removed_for_every_kind_of_config(client, user_token_headers):
    """No saved key can be read back (#1246): the owner-only reveal route is gone, so a
    cloud config, a local one and a malformed id all answer 404 (not 200, 422 or 500)."""
    cloud = _create_cloud_config(client, user_token_headers, name="NoReveal").json()
    local = client.post(
        _BASE,
        json={"name": "Local NoKey", "provider": "local", "model_name": "tiny"},
        headers=user_token_headers,
    ).json()
    for ident in (cloud["uuid"], local["uuid"], "not-a-uuid"):
        resp = client.get(f"{_BASE}/config/{ident}/api-key", headers=user_token_headers)
        assert resp.status_code == status.HTTP_404_NOT_FOUND, ident
        assert "dg_fixture_key_xyz" not in resp.text


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


def test_restart_admin_no_workers_is_refused_without_a_broadcast(client, super_admin_token_headers):
    """When inspect finds no Whisper worker the request is refused (409) and nothing is signalled:
    a bare ``shutdown`` would reach every worker in the deployment. Celery control is fully
    mocked so no real shutdown reaches the live stack."""
    fake_inspector = MagicMock()
    fake_inspector.ping.return_value = {}  # no workers
    fake_control = MagicMock()
    fake_control.inspect.return_value = fake_inspector

    with patch("app.core.celery.celery_app.control", fake_control):
        resp = client.post(f"{_BASE}/local-model/restart", headers=super_admin_token_headers)

    assert resp.status_code == status.HTTP_409_CONFLICT
    assert resp.json()["detail"]["code"] == "gpu_worker_not_found"
    fake_control.broadcast.assert_not_called()


def test_restart_admin_with_workers_envelope(client, super_admin_token_headers):
    """When Whisper workers are present, shutdown is targeted at them only and the
    envelope reports the discovered worker names + active task count."""
    fake_inspector = MagicMock()
    fake_inspector.ping.return_value = {"gpu-transcription@w1": {"ok": "pong"}}
    fake_inspector.active.return_value = {"gpu-transcription@w1": []}
    fake_control = MagicMock()
    fake_control.inspect.return_value = fake_inspector

    with patch("app.core.celery.celery_app.control", fake_control):
        resp = client.post(f"{_BASE}/local-model/restart", headers=super_admin_token_headers)

    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["status"] == "restart_signaled"
    assert data["workers"] == ["gpu-transcription@w1"]
    assert data["active_tasks"] == 0
    assert data["restart_requested_at"]
    fake_control.broadcast.assert_called_once_with("shutdown", destination=["gpu-transcription@w1"])
