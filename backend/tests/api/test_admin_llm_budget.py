"""``/api/admin/llm-budget``: the admin's LLM token limits (issue #790, D13)."""

from __future__ import annotations

from unittest.mock import patch

from app.models.system_settings import SystemSettings

URL = "/api/admin/llm-budget"


def test_a_user_cannot_read_or_change_the_limits(client, user_token_headers):
    assert client.get(URL, headers=user_token_headers).status_code == 403
    response = client.put(URL, json={"user_daily_tokens": 1}, headers=user_token_headers)
    assert response.status_code == 403


def test_defaults_are_unlimited(client, admin_token_headers):
    body = client.get(URL, headers=admin_token_headers).json()
    assert body["user_daily_tokens"] == 0
    assert body["instance_monthly_tokens"] == 0
    assert body["local_unlimited"] is False


def test_an_admin_sets_the_limits_and_the_change_is_audited(
    client, db_session, admin_token_headers
):
    with patch("app.api.endpoints.admin_llm_budget.audit_logger") as audit:
        response = client.put(
            URL,
            json={"user_daily_tokens": 50_000, "instance_monthly_tokens": 2_000_000},
            headers=admin_token_headers,
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user_daily_tokens"] == 50_000
    assert body["instance_monthly_tokens"] == 2_000_000
    stored = {
        row.key: row.value
        for row in db_session.query(SystemSettings).filter(SystemSettings.key.like("llm.budget.%"))
    }
    assert stored["llm.budget.user_daily_tokens"] == "50000"
    assert stored["llm.budget.instance_monthly_tokens"] == "2000000"
    details = audit.log.call_args.kwargs["details"]
    assert details == {
        "area": "llm_budget",
        "changes": {"user_daily_tokens": 50_000, "instance_monthly_tokens": 2_000_000},
    }

    # Unlimited again is a 0, not a missing value.
    back = client.put(URL, json={"user_daily_tokens": 0}, headers=admin_token_headers).json()
    assert back["user_daily_tokens"] == 0
    assert back["instance_monthly_tokens"] == 2_000_000


def test_a_negative_limit_is_rejected(client, admin_token_headers):
    response = client.put(URL, json={"user_daily_tokens": -1}, headers=admin_token_headers)
    assert response.status_code == 422


def test_an_empty_update_is_rejected(client, admin_token_headers):
    assert client.put(URL, json={}, headers=admin_token_headers).status_code == 400
