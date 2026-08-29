"""Characterization tests for the admin engine-settings endpoints.

Covers ``app/api/endpoints/engine_settings.py`` — the admin-only DB-backed
engine settings (issue #193 boundary-correction knobs) plus the Redis-backed
``/metrics`` reader. The existing ``tests/test_engine_settings.py`` already pins
the ``boundary_smoothing_enabled`` toggle round-trip; this suite fills the gaps:
admin gates (401/403), the float-valued boundary settings round-trip + bounds
(422), the non-boolean string keys, unknown-key reset (400), empty-update (400),
and the ``/metrics`` graceful-empty contract.

All mutations land on the savepoint-isolated ``db_session`` and roll back at
teardown, so no SystemSettings row survives a test (verified by the suite-level
leak check). No real GPU worker or Redis is exercised.
"""

from __future__ import annotations

import pytest
from fastapi import status

from app.services.system_settings_service import get_setting

_BASE = "/api/admin/engine-settings"

# This file and test_engine_settings.py both write engine.boundary_* keys through the
# same POST /update endpoint with no coordination between them — under `-n auto` two
# workers inserting overlapping keys in different orders can deadlock on the
# system_settings_key_key unique index (issue #389).
pytestmark = pytest.mark.xdist_group("engine_system_settings")


# ---------------------------------------------------------------------------
# Admin gates
# ---------------------------------------------------------------------------


def test_get_engine_settings_unauthorized(client):
    """No token → 401."""
    assert client.get(_BASE).status_code == status.HTTP_401_UNAUTHORIZED


def test_get_engine_settings_non_admin_forbidden(client, user_token_headers):
    """A normal user is rejected with the canonical admin-gate detail."""
    resp = client.get(_BASE, headers=user_token_headers)
    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert resp.json()["detail"].startswith("Not enough permissions")


def test_update_engine_settings_non_admin_forbidden(client, user_token_headers):
    resp = client.post(
        f"{_BASE}/update",
        json={"boundary_smoothing_enabled": True},
        headers=user_token_headers,
    )
    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert resp.json()["detail"].startswith("Not enough permissions")


def test_metrics_non_admin_forbidden(client, user_token_headers):
    resp = client.get(f"{_BASE}/metrics", headers=user_token_headers)
    assert resp.status_code == status.HTTP_403_FORBIDDEN


def test_reset_engine_setting_non_admin_forbidden(client, user_token_headers):
    resp = client.delete(f"{_BASE}/boundary_smoothing_enabled", headers=user_token_headers)
    assert resp.status_code == status.HTTP_403_FORBIDDEN


# ---------------------------------------------------------------------------
# GET — shape & all keys
# ---------------------------------------------------------------------------


def test_get_engine_settings_exposes_all_keys(client, super_admin_token_headers):
    """Every engine key is returned with a {value, source} envelope."""
    resp = client.get(_BASE, headers=super_admin_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    expected = {
        "transcriber_backend",
        "diarizer_backend",
        "boundary_smoothing_enabled",
        "boundary_acoustic_recheck_enabled",
        "boundary_acoustic_cosine_margin",
        "boundary_acoustic_max_word_dur",
    }
    assert set(data) == expected
    for entry in data.values():
        assert "value" in entry
        assert entry["source"] in ("db", "env", "default")


def test_get_string_setting_default(client, super_admin_token_headers):
    """String-valued keys coerce as plain strings (not bool/float)."""
    data = client.get(_BASE, headers=super_admin_token_headers).json()
    assert isinstance(data["transcriber_backend"]["value"], str)
    assert isinstance(data["diarizer_backend"]["value"], str)


def test_get_float_settings_are_floats(client, super_admin_token_headers):
    data = client.get(_BASE, headers=super_admin_token_headers).json()
    assert isinstance(data["boundary_acoustic_cosine_margin"]["value"], float)
    assert isinstance(data["boundary_acoustic_max_word_dur"]["value"], float)


# ---------------------------------------------------------------------------
# Float boundary settings round-trip + bounds
# ---------------------------------------------------------------------------


def test_set_cosine_margin_persists(client, super_admin_token_headers, db_session):
    """Float setting writes the engine.* key and reflects as a db override."""
    resp = client.post(
        f"{_BASE}/update",
        json={"boundary_acoustic_cosine_margin": 0.2},
        headers=super_admin_token_headers,
    )
    assert resp.status_code == status.HTTP_200_OK
    entry = resp.json()["boundary_acoustic_cosine_margin"]
    assert entry["value"] == 0.2
    assert entry["source"] == "db"
    assert get_setting(db_session, "engine.boundary_acoustic_cosine_margin") == "0.2"


def test_set_max_word_dur_persists(client, super_admin_token_headers, db_session):
    resp = client.post(
        f"{_BASE}/update",
        json={"boundary_acoustic_max_word_dur": 1.5},
        headers=super_admin_token_headers,
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["boundary_acoustic_max_word_dur"]["value"] == 1.5


def test_set_acoustic_recheck_bool_persists(client, super_admin_token_headers, db_session):
    resp = client.post(
        f"{_BASE}/update",
        json={"boundary_acoustic_recheck_enabled": True},
        headers=super_admin_token_headers,
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["boundary_acoustic_recheck_enabled"]["value"] is True
    assert get_setting(db_session, "engine.boundary_acoustic_recheck_enabled") == "true"


def test_cosine_margin_above_max_is_422(client, super_admin_token_headers):
    """cosine_margin Field is ge=0.0 le=1.0 → 422 above the ceiling."""
    resp = client.post(
        f"{_BASE}/update",
        json={"boundary_acoustic_cosine_margin": 1.5},
        headers=super_admin_token_headers,
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_cosine_margin_below_min_is_422(client, super_admin_token_headers):
    resp = client.post(
        f"{_BASE}/update",
        json={"boundary_acoustic_cosine_margin": -0.1},
        headers=super_admin_token_headers,
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_max_word_dur_above_max_is_422(client, super_admin_token_headers):
    """max_word_dur Field is ge=0.1 le=5.0."""
    resp = client.post(
        f"{_BASE}/update",
        json={"boundary_acoustic_max_word_dur": 9.0},
        headers=super_admin_token_headers,
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_max_word_dur_below_min_is_422(client, super_admin_token_headers):
    resp = client.post(
        f"{_BASE}/update",
        json={"boundary_acoustic_max_word_dur": 0.0},
        headers=super_admin_token_headers,
    )
    assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_set_string_backend_persists(client, super_admin_token_headers, db_session):
    resp = client.post(
        f"{_BASE}/update",
        json={"transcriber_backend": "whisperx"},
        headers=super_admin_token_headers,
    )
    assert resp.status_code == status.HTTP_200_OK
    entry = resp.json()["transcriber_backend"]
    assert entry["value"] == "whisperx"
    assert entry["source"] == "db"


def test_unknown_transcriber_backend_is_400(client, super_admin_token_headers):
    """E5: transcriber_backend had NO validation at all — any string returned 200
    and persisted, unlike diarizer_backend six lines away in the same handler."""
    resp = client.post(
        f"{_BASE}/update",
        json={"transcriber_backend": "anything"},
        headers=super_admin_token_headers,
    )
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "Unknown transcriber_backend" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Update validation
# ---------------------------------------------------------------------------


def test_update_empty_body_is_400(client, super_admin_token_headers):
    resp = client.post(f"{_BASE}/update", json={}, headers=super_admin_token_headers)
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["detail"] == "No fields provided to update"


def test_update_multiple_fields_at_once(client, super_admin_token_headers):
    """A multi-field update writes every provided key."""
    resp = client.post(
        f"{_BASE}/update",
        json={
            "boundary_smoothing_enabled": False,
            "boundary_acoustic_cosine_margin": 0.1,
        },
        headers=super_admin_token_headers,
    )
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    assert data["boundary_smoothing_enabled"]["value"] is False
    assert data["boundary_acoustic_cosine_margin"]["value"] == 0.1


# ---------------------------------------------------------------------------
# Reset (DELETE)
# ---------------------------------------------------------------------------


def test_reset_unknown_key_is_400(client, super_admin_token_headers):
    resp = client.delete(f"{_BASE}/bogus_key", headers=super_admin_token_headers)
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert "Unknown engine setting key 'bogus_key'" in resp.json()["detail"]


def test_reset_known_key_with_no_override_is_204(client, super_admin_token_headers):
    """DELETE on a key with no DB row is still a no-op 204 (idempotent)."""
    resp = client.delete(
        f"{_BASE}/boundary_acoustic_recheck_enabled", headers=super_admin_token_headers
    )
    assert resp.status_code == status.HTTP_204_NO_CONTENT


def test_reset_float_setting_round_trip(client, super_admin_token_headers, db_session):
    """Set a float override then reset it back to env/default."""
    client.post(
        f"{_BASE}/update",
        json={"boundary_acoustic_cosine_margin": 0.3},
        headers=super_admin_token_headers,
    )
    assert get_setting(db_session, "engine.boundary_acoustic_cosine_margin") == "0.3"

    resp = client.delete(
        f"{_BASE}/boundary_acoustic_cosine_margin", headers=super_admin_token_headers
    )
    assert resp.status_code == status.HTTP_204_NO_CONTENT
    assert get_setting(db_session, "engine.boundary_acoustic_cosine_margin") is None

    after = client.get(_BASE, headers=super_admin_token_headers).json()
    assert after["boundary_acoustic_cosine_margin"]["source"] in ("env", "default")


# ---------------------------------------------------------------------------
# Metrics reader
# ---------------------------------------------------------------------------


def test_metrics_returns_dict(client, super_admin_token_headers):
    """The metrics reader returns a dict (empty when no workers reported)."""
    resp = client.get(f"{_BASE}/metrics", headers=super_admin_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert isinstance(resp.json(), dict)


def test_metrics_unauthorized(client):
    assert client.get(f"{_BASE}/metrics").status_code == status.HTTP_401_UNAUTHORIZED
