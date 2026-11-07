"""API tests for GET /usage/me (savepoint-rolled-back).

These two routes (``app/api/endpoints/usage.py``) had zero real exercise. Pinned
real behavior confirmed from source:

- Cost is **omitted (None)**, never ``0.0``, for a model absent from the rate
  table (``estimate_cost_usd`` returns ``None`` and the endpoint leaves
  ``estimated_cost_usd``/the per-model ``estimated_cost_usd`` at ``None``, setting
  ``cost_incomplete=True``) — "an honest blank beats a confident $0.00", per the
  route's own docstring.
- ``rates_verified_on`` is always present, sourced from
  ``app.services.chat.pricing.RATES_VERIFIED_ON``.
- ``days`` is bounded by module constant ``MAX_WINDOW_DAYS = 365``
  (``Query(30, ge=1, le=MAX_WINDOW_DAYS)``); 366 is a 422.
"""

from __future__ import annotations

import uuid as uuid_mod
from datetime import UTC
from datetime import datetime

from app.models.usage_event import UsageEvent
from app.services.chat.pricing import RATES_VERIFIED_ON
from app.services.chat.usage import EVENT_TYPE_CHAT_TOKENS


def _make_event(
    db_session,
    user,
    *,
    provider: str = "anthropic",
    model: str = "claude-sonnet-4-5",
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    created_at: datetime | None = None,
) -> UsageEvent:
    total = prompt_tokens + completion_tokens
    event = UsageEvent(
        id=uuid_mod.uuid4(),
        user_id=user.id,
        event_type=EVENT_TYPE_CHAT_TOKENS,
        quantity=total,
        unit="tokens",
        event_metadata={
            "provider": provider,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
        created_at=created_at or datetime.now(UTC),
    )
    db_session.add(event)
    db_session.flush()
    return event


class TestUsageMe:
    def test_cross_user_isolation(
        self, client, db_session, normal_user, admin_user, user_token_headers
    ):
        _make_event(db_session, normal_user, model="mine-model")
        _make_event(db_session, admin_user, model="theirs-model")
        headers = {"Authorization": user_token_headers["Authorization"]}

        resp = client.get("/api/usage/me", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()

        models_seen = {row["model"] for row in body["by_model"]}
        assert "mine-model" in models_seen
        assert "theirs-model" not in models_seen

    def test_unpriced_model_omits_cost_not_zero(
        self, client, db_session, normal_user, user_token_headers
    ):
        _make_event(
            db_session,
            normal_user,
            provider="mystery-provider",
            model="ghost-model",
            prompt_tokens=1000,
            completion_tokens=500,
        )
        headers = {"Authorization": user_token_headers["Authorization"]}

        resp = client.get("/api/usage/me", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["cost_incomplete"] is True
        assert body["estimated_cost_usd"] is None

        bucket = next(row for row in body["by_model"] if row["model"] == "ghost-model")
        assert bucket["estimated_cost_usd"] is None

    def test_rates_verified_on_present(self, client, user_token_headers):
        headers = {"Authorization": user_token_headers["Authorization"]}
        resp = client.get("/api/usage/me", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["rates_verified_on"] == RATES_VERIFIED_ON

    def test_days_beyond_max_window_is_422(self, client, user_token_headers):
        headers = {"Authorization": user_token_headers["Authorization"]}
        resp = client.get("/api/usage/me", headers=headers, params={"days": 366})
        assert resp.status_code == 422

        within_bound = client.get("/api/usage/me", headers=headers, params={"days": 365})
        assert within_bound.status_code == 200


def _add(db_session, user, event_type: str, quantity: int, unit: str = "tokens", **meta) -> None:
    db_session.add(
        UsageEvent(
            id=uuid_mod.uuid4(),
            user_id=user.id,
            event_type=event_type,
            quantity=quantity,
            unit=unit,
            event_metadata=meta or None,
            created_at=datetime.now(UTC),
        )
    )
    db_session.flush()


class TestEveryEventType:
    """Issue #790: the usage API used to count only ``chat.tokens``."""

    def test_task_usage_and_non_token_events_are_returned(
        self, client, db_session, normal_user, user_token_headers
    ):
        _make_event(db_session, normal_user, prompt_tokens=100, completion_tokens=50)
        _add(
            db_session,
            normal_user,
            "summary.tokens",
            700,
            provider="openai",
            model="gpt-x",
            prompt_tokens=600,
            completion_tokens=100,
        )
        _add(db_session, normal_user, "speaker_id.tokens", 300, provider="openai", model="gpt-x")
        _add(db_session, normal_user, "transcription.hours", 2, unit="hours")
        headers = {"Authorization": user_token_headers["Authorization"]}

        body = client.get("/api/usage/me", headers=headers).json()

        kinds = {row["event_type"]: row for row in body["by_event_type"]}
        assert set(kinds) == {
            "chat.tokens",
            "summary.tokens",
            "speaker_id.tokens",
            "transcription.hours",
        }
        assert kinds["transcription.hours"]["unit"] == "hours"
        assert kinds["transcription.hours"]["quantity"] == 2
        features = {row["event_type"]: row["total_tokens"] for row in body["by_feature"]}
        assert features == {"chat.tokens": 150, "summary.tokens": 700, "speaker_id.tokens": 300}
        assert body["totals"]["total_tokens"] == 1150
        assert body["totals"]["messages"] == 1  # chat exchanges only
        assert body["totals"]["events"] == 3

    def test_chat_prep_calls_roll_up_under_the_chat_feature(
        self, client, db_session, normal_user, user_token_headers
    ):
        _make_event(db_session, normal_user, prompt_tokens=100, completion_tokens=50)
        _add(db_session, normal_user, "chat.prep.tokens", 40, provider="p", model="m")
        headers = {"Authorization": user_token_headers["Authorization"]}

        body = client.get("/api/usage/me", headers=headers).json()

        assert {r["event_type"]: r["total_tokens"] for r in body["by_feature"]} == {
            "chat.tokens": 190
        }
        assert body["totals"]["total_tokens"] == 190
        assert body["totals"]["messages"] == 1  # a prep call is not a chat exchange

    def test_a_local_model_is_free_and_a_named_one_is_unpriced(
        self, client, db_session, normal_user, user_token_headers
    ):
        _add(db_session, normal_user, "summary.tokens", 100, provider="vllm", model="m", local=True)
        headers = {"Authorization": user_token_headers["Authorization"]}
        body = client.get("/api/usage/me", headers=headers).json()
        assert body["estimated_cost_usd"] == 0.0
        assert body["cost_incomplete"] is False

        _add(db_session, normal_user, "chat.tokens", 100, provider="vllm", model="m")
        body = client.get("/api/usage/me", headers=headers).json()
        assert body["cost_incomplete"] is True

    def test_the_budget_shows_the_callers_day_against_the_limit(
        self, client, db_session, normal_user, user_token_headers
    ):
        from app.services.system_settings_service import set_setting

        set_setting(db_session, "llm.budget.user_daily_tokens", 5000)
        _add(db_session, normal_user, "summary.tokens", 1200)
        headers = {"Authorization": user_token_headers["Authorization"]}

        budget = client.get("/api/usage/me", headers=headers).json()["budget"]

        assert budget["user_daily_tokens"] == 5000
        assert budget["used_today"] == 1200
        assert budget["instance_limited"] is False
        assert "instance_used_this_month" not in budget  # spans every tenant
