"""``/api/admin/telemetry``: the opt-in usage report's switch and preview (issue #1019)."""

from __future__ import annotations

import pytest

from app.services import telemetry

# Every test writes the same unique `system_settings` rows.
pytestmark = pytest.mark.xdist_group("telemetry_setting")

URL = "/api/admin/telemetry"


@pytest.fixture
def allowed(monkeypatch):
    for name in ("DO_NOT_TRACK", "OPENTRANSCRIBE_TELEMETRY", "OPENTRANSCRIBE_DEMO_MODE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("OPENTRANSCRIBE_TELEMETRY_ENDPOINT", raising=False)
    monkeypatch.setenv("OPENTRANSCRIBE_TELEMETRY_PROJECT_KEY", "phc_test_key")


@pytest.fixture
def audit_events(monkeypatch):
    from app.api.endpoints import admin_telemetry

    captured: list[dict] = []
    monkeypatch.setattr(admin_telemetry.audit_logger, "log", lambda **kw: captured.append(kw))
    return captured


@pytest.mark.parametrize(("method", "kwargs"), [("get", {}), ("put", {"json": {"enabled": True}})])
def test_a_plain_admin_is_denied(client, admin_token_headers, allowed, method, kwargs):
    response = getattr(client, method)(URL, headers=admin_token_headers, **kwargs)

    assert response.status_code == 403


def test_the_status_is_off_and_the_preview_is_the_real_request(
    client, super_admin_token_headers, db_session, allowed
):
    response = client.get(URL, headers=super_admin_token_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["sending"] is False
    assert body["off_reasons"] == []
    assert body["last_sent_at"] is None
    assert body["endpoint"] == "https://eu.i.posthog.com/i/v0/e/"
    assert body["preview"] == telemetry.build_request(db_session, telemetry.PREVIEW_INSTALL_ID)


def test_turning_it_on_and_off(
    client, super_admin_token_headers, db_session, allowed, audit_events
):
    on = client.put(URL, headers=super_admin_token_headers, json={"enabled": True})
    install_id = telemetry.read_state(db_session)["install_id"]
    off = client.put(URL, headers=super_admin_token_headers, json={"enabled": False})

    assert on.status_code == 200
    assert on.json()["enabled"] is True
    assert on.json()["sending"] is True
    assert on.json()["preview"]["distinct_id"] == install_id
    assert off.status_code == 200
    assert off.json()["enabled"] is False
    assert telemetry.read_state(db_session)["install_id"] is None
    assert [e["details"] for e in audit_events] == [
        {"setting": "telemetry.enabled", "value": True},
        {"setting": "telemetry.enabled", "value": False},
    ]


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        ({"DO_NOT_TRACK": "1"}, "do_not_track"),
        ({"OPENTRANSCRIBE_DEMO_MODE": "1"}, "demo"),
        ({"OPENTRANSCRIBE_TELEMETRY": "off"}, "env_disabled"),
    ],
)
def test_turning_it_on_is_refused_with_the_reason(
    client, super_admin_token_headers, db_session, allowed, monkeypatch, env, reason
):
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    response = client.put(URL, headers=super_admin_token_headers, json={"enabled": True})
    status = client.get(URL, headers=super_admin_token_headers).json()

    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "telemetry_refused", "reasons": [reason]}
    assert status["enabled"] is False
    assert status["off_reasons"] == [reason]


def test_no_receiver_key_refuses(client, super_admin_token_headers, allowed, monkeypatch):
    monkeypatch.setenv("OPENTRANSCRIBE_TELEMETRY_PROJECT_KEY", "")

    response = client.put(URL, headers=super_admin_token_headers, json={"enabled": True})

    assert response.status_code == 409
    assert response.json()["detail"]["reasons"] == ["no_receiver"]


def test_the_refusal_carries_the_switches_codes_never_the_exceptions_text(
    client, super_admin_token_headers, allowed, monkeypatch
):
    # The raise names a reason the switches do not: only an echo of the exception shows "demo".
    def refuse(_db, _enabled):
        raise telemetry.TelemetryRefusedError([telemetry.REASON_DEMO])

    monkeypatch.setenv("DO_NOT_TRACK", "1")
    monkeypatch.setattr(telemetry, "set_enabled", refuse)

    response = client.put(URL, headers=super_admin_token_headers, json={"enabled": True})

    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "telemetry_refused", "reasons": ["do_not_track"]}
    assert telemetry.REASON_DEMO not in response.text
