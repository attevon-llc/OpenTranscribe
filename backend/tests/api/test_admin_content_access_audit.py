"""``/api/admin/audit/content-access``: the super_admin switch for content auditing (#1277)."""

from __future__ import annotations

import pytest

from app.core.constants import AUDIT_CONTENT_ACCESS_SETTING_KEY
from app.services.system_settings_service import get_setting_bool

URL = "/api/admin/audit/content-access"

# Every test writes the same unique `system_settings` row.
pytestmark = pytest.mark.xdist_group("content_access_audit_setting")


@pytest.fixture
def audit_events(monkeypatch):
    from app.api.endpoints import admin_audit_content_access

    captured: list[dict] = []
    monkeypatch.setattr(
        admin_audit_content_access.audit_logger, "log", lambda **kw: captured.append(kw)
    )
    return captured


@pytest.mark.parametrize("method", ["get", "put"])
def test_only_a_super_admin_may_call(client, admin_token_headers, user_token_headers, method):
    kwargs = {"json": {"enabled": True}} if method == "put" else {}
    assert getattr(client, method)(URL, headers=admin_token_headers, **kwargs).status_code == 403
    assert getattr(client, method)(URL, headers=user_token_headers, **kwargs).status_code == 403


def test_the_switch_defaults_to_off_and_reports_its_cost(client, super_admin_token_headers):
    response = client.get(URL, headers=super_admin_token_headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled"] is False and body["active"] is False
    assert body["capability_available"] is True
    assert body["dedup_seconds"] == 900
    assert body["event_bytes_estimate"] > 0 and body["bytes_per_active_user_per_day_estimate"] > 0


def test_turning_it_on_and_off_is_stored_and_audited(
    client, db_session, super_admin_token_headers, audit_events
):
    on = client.put(URL, headers=super_admin_token_headers, json={"enabled": True})
    assert on.status_code == 200, on.text
    assert (on.json()["enabled"], on.json()["active"]) == (True, True)
    assert get_setting_bool(db_session, AUDIT_CONTENT_ACCESS_SETTING_KEY, False) is True

    off = client.put(URL, headers=super_admin_token_headers, json={"enabled": False})
    assert off.json()["enabled"] is False
    assert get_setting_bool(db_session, AUDIT_CONTENT_ACCESS_SETTING_KEY, True) is False

    assert [e["event_type"].value for e in audit_events] == ["admin.settings.change"] * 2
    assert [(e["details"]["from"], e["details"]["to"]) for e in audit_events] == [
        (False, True),
        (True, False),
    ]
    assert all(e["details"]["setting"] == AUDIT_CONTENT_ACCESS_SETTING_KEY for e in audit_events)


def test_an_edition_that_withholds_the_capability_is_reported_inactive(
    client, super_admin_token_headers, monkeypatch
):
    from app.api.endpoints import admin_audit_content_access

    client.put(URL, headers=super_admin_token_headers, json={"enabled": True})
    monkeypatch.setattr(admin_audit_content_access, "capability_enabled", lambda *a, **k: False)

    body = client.get(URL, headers=super_admin_token_headers).json()

    assert (body["enabled"], body["capability_available"], body["active"]) == (True, False, False)
