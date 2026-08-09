"""Characterization tests for the content-redaction settings endpoints.

Covers ``app/api/endpoints/redaction_settings.py`` — two routers:
- user prefs at ``/api/user-settings/redaction`` (GET/PUT/DELETE + /defaults)
- admin governance floor at ``/api/admin/redaction-policy`` (GET/POST update)

Pins: per-user round-trip (incl. list/bool/float fields), reset-to-defaults,
the admin enforcement floor surfacing in the user ``/defaults`` lock set
(force_pii → locked_categories, force_export_redacted → export_locked), admin
gates (401/403), and empty-update (400). All writes land on the
savepoint-isolated session and roll back; the suite-level leak check confirms no
SystemSettings/UserSetting drift.
"""

from __future__ import annotations

from fastapi import status

_USER = "/api/user-settings/redaction"
_ADMIN = "/api/admin/redaction-policy"


# ---------------------------------------------------------------------------
# Auth gates
# ---------------------------------------------------------------------------


def test_get_user_settings_unauthorized(client):
    assert client.get(_USER).status_code == status.HTTP_401_UNAUTHORIZED


def test_get_policy_unauthorized(client):
    assert client.get(_ADMIN).status_code == status.HTTP_401_UNAUTHORIZED


def test_get_policy_non_admin_forbidden(client, user_token_headers):
    resp = client.get(_ADMIN, headers=user_token_headers)
    assert resp.status_code == status.HTTP_403_FORBIDDEN
    assert resp.json()["detail"].startswith("Not enough permissions")


def test_update_policy_non_admin_forbidden(client, user_token_headers):
    resp = client.post(f"{_ADMIN}/update", json={"force_pii": True}, headers=user_token_headers)
    assert resp.status_code == status.HTTP_403_FORBIDDEN


# ---------------------------------------------------------------------------
# Per-user preferences
# ---------------------------------------------------------------------------


def test_get_user_settings_defaults_shape(client, user_token_headers):
    resp = client.get(_USER, headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    for key in (
        "enabled",
        "detectors",
        "categories",
        "pii_entities",
        "style",
        "custom_words",
        "allowlist",
        "toxicity_threshold",
        "redact_before_llm",
        "default_export_redacted",
    ):
        assert key in data
    assert isinstance(data["detectors"], list)
    assert isinstance(data["toxicity_threshold"], (int, float))


def test_update_user_bool_round_trip(client, user_token_headers):
    resp = client.put(_USER, json={"enabled": False}, headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["enabled"] is False
    # Persisted: a fresh GET reflects it
    assert client.get(_USER, headers=user_token_headers).json()["enabled"] is False


def test_update_user_list_field_round_trip(client, user_token_headers):
    resp = client.put(_USER, json={"custom_words": ["foo", "bar"]}, headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["custom_words"] == ["foo", "bar"]


def test_update_user_float_field_round_trip(client, user_token_headers):
    resp = client.put(_USER, json={"toxicity_threshold": 0.8}, headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["toxicity_threshold"] == 0.8


def test_update_user_partial_only_changes_given(client, user_token_headers):
    """Updating one field leaves the others at their defaults."""
    before = client.get(_USER, headers=user_token_headers).json()
    client.put(_USER, json={"style": before["style"]}, headers=user_token_headers)
    after = client.put(_USER, json={"enabled": False}, headers=user_token_headers).json()
    assert after["categories"] == before["categories"]


def test_reset_user_settings(client, user_token_headers):
    client.put(_USER, json={"enabled": False, "custom_words": ["x"]}, headers=user_token_headers)
    resp = client.delete(_USER, headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    assert "reset" in resp.json()["message"].lower()
    # Back to coded defaults
    restored = client.get(_USER, headers=user_token_headers).json()
    assert restored["custom_words"] == []


def test_user_defaults_endpoint_shape(client, user_token_headers):
    resp = client.get(f"{_USER}/defaults", headers=user_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    for key in ("locked_categories", "export_locked", "redact_before_llm_locked"):
        assert key in data


# ---------------------------------------------------------------------------
# Admin governance policy
# ---------------------------------------------------------------------------


def test_get_policy_admin_shape(client, super_admin_token_headers):
    resp = client.get(_ADMIN, headers=super_admin_token_headers)
    assert resp.status_code == status.HTTP_200_OK
    data = resp.json()
    for key in (
        "force_pii",
        "force_pii_entities",
        "force_toxicity",
        "force_toxicity_threshold",
        "force_profanity",
        "force_custom_words",
        "force_export_redacted",
        "force_redact_before_llm",
        "pii_use_gliner",
    ):
        assert key in data


def test_update_policy_empty_body_is_400(client, super_admin_token_headers):
    resp = client.post(f"{_ADMIN}/update", json={}, headers=super_admin_token_headers)
    assert resp.status_code == status.HTTP_400_BAD_REQUEST
    assert resp.json()["detail"] == "No fields provided to update"


def test_update_policy_bool_round_trip(client, super_admin_token_headers):
    resp = client.post(
        f"{_ADMIN}/update", json={"force_pii": True}, headers=super_admin_token_headers
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["force_pii"] is True
    assert client.get(_ADMIN, headers=super_admin_token_headers).json()["force_pii"] is True


def test_update_policy_list_round_trip(client, super_admin_token_headers):
    resp = client.post(
        f"{_ADMIN}/update",
        json={"force_custom_words": ["banned1", "banned2"]},
        headers=super_admin_token_headers,
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["force_custom_words"] == ["banned1", "banned2"]


def test_update_policy_float_round_trip(client, super_admin_token_headers):
    resp = client.post(
        f"{_ADMIN}/update",
        json={"force_toxicity_threshold": 0.9},
        headers=super_admin_token_headers,
    )
    assert resp.status_code == status.HTTP_200_OK
    assert resp.json()["force_toxicity_threshold"] == 0.9


# ---------------------------------------------------------------------------
# Admin floor ↔ user-defaults interplay (the enforcement floor)
# ---------------------------------------------------------------------------


def test_force_pii_surfaces_in_user_locked_categories(
    client, super_admin_token_headers, user_token_headers
):
    """Admin force_pii → the user's /defaults locks the 'pii' category."""
    client.post(f"{_ADMIN}/update", json={"force_pii": True}, headers=super_admin_token_headers)
    defaults = client.get(f"{_USER}/defaults", headers=user_token_headers).json()
    assert "pii" in defaults["locked_categories"]


def test_force_export_redacted_surfaces_as_export_locked(
    client, super_admin_token_headers, user_token_headers
):
    client.post(
        f"{_ADMIN}/update",
        json={"force_export_redacted": True},
        headers=super_admin_token_headers,
    )
    defaults = client.get(f"{_USER}/defaults", headers=user_token_headers).json()
    assert defaults["export_locked"] is True


def test_force_redact_before_llm_surfaces_as_locked(
    client, super_admin_token_headers, user_token_headers
):
    client.post(
        f"{_ADMIN}/update",
        json={"force_redact_before_llm": True},
        headers=super_admin_token_headers,
    )
    defaults = client.get(f"{_USER}/defaults", headers=user_token_headers).json()
    assert defaults["redact_before_llm_locked"] is True
