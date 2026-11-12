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


USERS_URL = "/api/admin/llm-budget/users"


def _token_event(db, user, *, quantity, model="claude-sonnet-5", provider="anthropic", when=None):
    from app.models.usage_event import UsageEvent

    event = UsageEvent(
        user_id=user.id,
        event_type="chat.tokens",
        quantity=quantity,
        unit="tokens",
        event_metadata={
            "provider": provider,
            "model": model,
            "prompt_tokens": quantity - 10,
            "completion_tokens": 10,
        },
    )
    if when is not None:
        event.created_at = when
    db.add(event)
    db.commit()


def test_a_user_cannot_read_per_user_spend(client, user_token_headers):
    assert client.get(USERS_URL, headers=user_token_headers).status_code == 403


def test_an_anonymous_caller_cannot_read_per_user_spend(client):
    assert client.get(USERS_URL).status_code == 401


def test_an_admin_sees_each_users_tokens_and_estimated_cost(
    client, db_session, admin_token_headers, normal_user
):
    # No llm_token_budget row: counters exist only while a limit is set, and unlimited is the
    # default. Tokens must then come from the recorded usage events.
    _token_event(db_session, normal_user, quantity=1000)
    _token_event(db_session, normal_user, quantity=500, model="no-such-model-xyz")

    body = client.get(USERS_URL, headers=admin_token_headers).json()

    row = next(r for r in body["users"] if r["user_uuid"] == str(normal_user.uuid))
    assert row["email"] == normal_user.email
    assert row["tokens_today"] == 1500
    assert row["tokens_this_month"] == 1500
    assert row["estimated_cost_usd"] is not None
    assert row["estimated_cost_usd"] > 0
    # The unpriced model must be flagged, not counted as $0.
    assert row["cost_incomplete"] is True
    assert body["rates_verified_on"]
    # The internal integer key, and anything identifying a conversation, file or prompt, are absent.
    assert set(row) == {
        "user_uuid",
        "email",
        "full_name",
        "tokens_today",
        "tokens_this_month",
        "estimated_cost_usd",
        "cost_incomplete",
    }


def test_an_enforcement_counter_wins_over_events_for_its_day(
    client, db_session, admin_token_headers, normal_user
):
    from datetime import UTC
    from datetime import datetime

    from app.models.llm_token_budget import SCOPE_USER_DAY
    from app.models.llm_token_budget import LLMTokenBudget

    db_session.add(
        LLMTokenBudget(
            scope=SCOPE_USER_DAY,
            user_id=normal_user.id,
            period=datetime.now(UTC).strftime("%Y-%m-%d"),
            used=1700,
        )
    )
    db_session.commit()
    _token_event(db_session, normal_user, quantity=1000)

    body = client.get(USERS_URL, headers=admin_token_headers).json()

    row = next(r for r in body["users"] if r["user_uuid"] == str(normal_user.uuid))
    assert row["tokens_today"] == 1700
    assert row["tokens_this_month"] == 1700


def test_events_from_a_previous_month_are_not_counted(
    client, db_session, admin_token_headers, normal_user
):
    from datetime import UTC
    from datetime import datetime
    from datetime import timedelta

    now = datetime.now(UTC)
    last_month = now.replace(day=1) - timedelta(days=2)
    _token_event(db_session, normal_user, quantity=1000, when=last_month)

    body = client.get(USERS_URL, headers=admin_token_headers).json()

    assert all(r["user_uuid"] != str(normal_user.uuid) for r in body["users"])
