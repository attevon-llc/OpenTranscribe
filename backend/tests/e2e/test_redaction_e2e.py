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
    # Snapshot current settings so the live dev user's preferences survive the test
    before = api_helper.get("/api/user-settings/redaction")
    assert isinstance(before.get("enabled"), bool)
    assert before["style"] in ("label", "asterisks", "first_letter", "blur")

    try:
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

        # Reset wipes stored prefs -> coded defaults (redaction is opt-out: disabled)
        resp = _delete(backend_url, token, "/api/user-settings/redaction")
        assert resp.status_code == 200, resp.text
        after = api_helper.get("/api/user-settings/redaction")
        assert after["style"] == "label"
        assert after["enabled"] is False  # DEFAULT_REDACTION_ENABLED
        assert "pii" not in after["categories"]  # PII is opt-in by default
    finally:
        # Restore the user's pre-test settings (dev data must not be mutated)
        restore = {
            k: before[k]
            for k in ("enabled", "categories", "style", "custom_words", "pii_entities")
            if k in before
        }
        _put(backend_url, token, "/api/user-settings/redaction", restore)


def test_system_defaults_expose_locked_set(api_helper, token):
    defaults = api_helper.get("/api/user-settings/redaction/defaults")
    assert "available_detectors" in defaults
    assert "locked_categories" in defaults
    assert isinstance(defaults["locked_categories"], list)


def test_admin_policy_force_pii(api_helper, backend_url, token):
    """Enabling force_pii locks the PII category for every user — and must be undone.

    ``force_pii`` is a stack-wide admin policy, not per-test state: left ON it changes
    what every other user and every later test sees in the transcript, which is a
    persistent change to dev data. The clear used to sit at the end of the happy path, so
    the ``locked_categories`` assertion in the middle — the one most likely to fail —
    would leave the whole dev stack force-redacting PII.
    """
    resp = api_helper.post("/api/admin/redaction-policy/update", {"force_pii": True})
    assert resp.get("force_pii") is True

    try:
        defaults = api_helper.get("/api/user-settings/redaction/defaults")
        assert "pii" in defaults["locked_categories"]
    finally:
        cleared = api_helper.post("/api/admin/redaction-policy/update", {"force_pii": False})
        assert cleared.get("force_pii") is False


def test_transcript_redact_toggle_is_owner_honored(api_helper, token, owned_transcribed_file):
    """The owner can request the unredacted original of a file this suite created.

    Two things were wrong with the previous version, and the second hid the first.

    It listed ``/api/files?limit=1`` and skipped when the deployment had none — the
    dev-data dependency this suite is being cured of. But it also never requested the
    ``token`` fixture, so ``api_helper`` carried no bearer credential: the listing came
    back 401, ``items`` was empty, and the test **skipped on every run against a stack
    that was full of files**. It has therefore never exercised the redact toggle at all.

    Depending on ``owned_transcribed_file`` fixes both: the file is one this session
    uploaded and will delete, and its presence is a fixture guarantee rather than
    something to branch on. ``token`` is requested explicitly so ``api_helper`` is
    authenticated — without it this reverts to asserting on an error body.
    """
    file_uuid = owned_transcribed_file["uuid"]
    detail = api_helper.get(f"/api/files/{file_uuid}?redact=false")
    assert detail.get("uuid") == file_uuid, f"redact=false did not return the file: {detail}"
    assert detail.get("transcript_segments"), (
        "the owner asking for the unredacted original must receive the transcript"
    )
