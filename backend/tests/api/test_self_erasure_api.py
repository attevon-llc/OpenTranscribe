"""``/users/me/erasure-request`` and ``/admin/self-erasure`` (issue #1354)."""

from __future__ import annotations

import pytest

from app.core.constants import GDPR_SELF_ERASURE_ENABLED_KEY
from app.services.system_settings_service import set_setting

URL = "/api/users/me/erasure-request"
GOOD = {"confirm": "ERASE", "password": "password123"}


@pytest.fixture(autouse=True)
def journal_dir(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)


@pytest.fixture()
def enabled(db_session):
    set_setting(db_session, GDPR_SELF_ERASURE_ENABLED_KEY, True)


def test_status_reports_disabled_by_default(client, user_token_headers):
    r = client.get(URL, headers=user_token_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["request"] is None
    assert body["grace_days"] == 14


def test_request_is_404_when_disabled(client, user_token_headers):
    r = client.post(URL, json=GOOD, headers=user_token_headers)
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "not_found"


def test_request_returns_a_receipt_then_status_shows_it(client, user_token_headers, enabled):
    r = client.post(URL, json=GOOD, headers=user_token_headers)
    assert r.status_code == 201, r.text
    receipt = r.json()
    assert receipt["status"] == "scheduled"
    assert len(receipt["request_digest"]) == 64
    assert receipt["scheduled_for"] > receipt["requested_at"]

    status = client.get(URL, headers=user_token_headers).json()
    assert status["enabled"] is True
    assert status["request"]["ledger_uuid"] == receipt["ledger_uuid"]


def test_a_wrong_password_is_refused_and_creates_nothing(client, user_token_headers, enabled):
    r = client.post(URL, json={"confirm": "ERASE", "password": "nope"}, headers=user_token_headers)
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "reauth_required"
    assert client.get(URL, headers=user_token_headers).json()["request"] is None


def test_the_confirmation_word_is_required(client, user_token_headers, enabled):
    r = client.post(
        URL, json={"confirm": "yes", "password": "password123"}, headers=user_token_headers
    )
    assert r.status_code == 422


def test_a_second_request_is_a_conflict(client, user_token_headers, enabled):
    assert client.post(URL, json=GOOD, headers=user_token_headers).status_code == 201
    r = client.post(URL, json=GOOD, headers=user_token_headers)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "request_exists"


def test_the_last_super_admin_is_refused(client, super_admin_token_headers, enabled):
    r = client.post(
        URL,
        json={"confirm": "ERASE", "password": "superadminpass"},
        headers=super_admin_token_headers,
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "last_super_admin"


def test_cancel_withdraws_the_request(client, user_token_headers, enabled):
    receipt = client.post(URL, json=GOOD, headers=user_token_headers).json()
    r = client.delete(f"{URL}/{receipt['ledger_uuid']}", headers=user_token_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    assert client.get(URL, headers=user_token_headers).json()["request"] is None


def test_another_users_request_cannot_be_cancelled(
    client, user_token_headers, other_user_auth_headers, enabled
):
    receipt = client.post(URL, json=GOOD, headers=user_token_headers).json()
    r = client.delete(f"{URL}/{receipt['ledger_uuid']}", headers=other_user_auth_headers)
    assert r.status_code == 404
    assert client.get(URL, headers=user_token_headers).json()["request"] is not None


def test_anonymous_callers_are_refused(client):
    assert client.get(URL).status_code == 401


def test_admin_settings_round_trip_and_clamp(client, super_admin_token_headers, user_token_headers):
    admin = "/api/admin/self-erasure"
    assert client.get(admin, headers=user_token_headers).status_code == 403
    r = client.put(
        admin, json={"enabled": True, "grace_days": 7}, headers=super_admin_token_headers
    )
    assert r.status_code == 200
    assert r.json()["enabled"] is True
    assert r.json()["grace_days"] == 7
    too_long = client.put(admin, json={"grace_days": 60}, headers=super_admin_token_headers)
    assert too_long.status_code == 422
    assert client.get(admin, headers=super_admin_token_headers).json()["grace_days"] == 7
