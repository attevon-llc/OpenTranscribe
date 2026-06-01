"""End-to-end tests for content-redaction settings + enforcement (requires dev stack).

Exercises the real API surface against the running backend:
  - per-user redaction settings GET/PUT/DELETE
  - system defaults expose the admin-forced/locked set
  - admin governance policy GET/update
  - (if a completed file exists) the transcript ?redact toggle is owner-gated

Run: cd backend && pytest -m e2e tests/e2e/test_redaction_e2e.py -v
Creds (conftest): admin@example.com / password.
"""

from __future__ import annotations

import pytest
import requests
from conftest import TEST_ADMIN_EMAIL
from conftest import TEST_ADMIN_PASSWORD

pytestmark = pytest.mark.e2e


def _put(backend_url: str, token: str, endpoint: str, data: dict) -> requests.Response:
    return requests.put(
        f"{backend_url}{endpoint}",
        json=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=30,
    )


def _delete(backend_url: str, token: str, endpoint: str) -> requests.Response:
    return requests.delete(
        f"{backend_url}{endpoint}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )


@pytest.fixture()
def token(api_helper) -> str:
    result = api_helper.login(TEST_ADMIN_EMAIL, TEST_ADMIN_PASSWORD)
    assert "access_token" in result, f"login failed: {result}"
    return str(result["access_token"])


def test_user_redaction_settings_roundtrip(api_helper, backend_url, token):
    # Defaults
    settings = api_helper.get("/api/user-settings/redaction")
    assert settings["enabled"] is True
    assert settings["style"] in ("label", "asterisks", "first_letter", "blur")

    # Update style + custom words
    resp = _put(
        backend_url,
        token,
        "/api/user-settings/redaction",
        {"style": "asterisks", "custom_words": ["Bluefin"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["style"] == "asterisks"
    assert "Bluefin" in body["custom_words"]

    # Reset
    resp = _delete(backend_url, token, "/api/user-settings/redaction")
    assert resp.status_code == 200, resp.text
    after = api_helper.get("/api/user-settings/redaction")
    assert after["style"] == "label"  # back to default


def test_system_defaults_expose_locked_set(api_helper):
    defaults = api_helper.get("/api/user-settings/redaction/defaults")
    assert "available_detectors" in defaults
    assert "locked_categories" in defaults
    assert isinstance(defaults["locked_categories"], list)


def test_admin_policy_force_pii(api_helper, backend_url, token):
    # Enable force_pii, verify it reflects in policy + user defaults locked set, then clear.
    resp = api_helper.post("/api/admin/redaction-policy/update", {"force_pii": True})
    assert resp.get("force_pii") is True

    defaults = api_helper.get("/api/user-settings/redaction/defaults")
    assert "pii" in defaults["locked_categories"]

    # Clear the force flag (cleanup).
    resp = api_helper.post("/api/admin/redaction-policy/update", {"force_pii": False})
    assert resp.get("force_pii") is False


def test_transcript_redact_toggle_if_file_exists(api_helper):
    """If a completed file exists, the redact toggle must be owner-honored."""
    files = api_helper.get("/api/files?limit=1")
    items = files.get("items") or files.get("media_files") or []
    if not items:
        pytest.skip("no files available to exercise the transcript redact toggle")
    file_uuid = items[0].get("uuid") or items[0].get("id")
    # Owner can request the original (redact=false) without error.
    detail = api_helper.get(f"/api/files/{file_uuid}?redact=false")
    assert "transcript_segments" in detail or "id" in detail
