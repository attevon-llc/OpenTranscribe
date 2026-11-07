"""``POST /api/chat/context/estimate`` says so when it does not know the context window.

It used to substitute 8192 whenever no LLM service could be built and return it as the model's
real window: no log, no flag. The same number, silently assumed, under-measured four evaluation
runs (issue #533), and the picker computed a confident percentage against it.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.api.endpoints.chat.messages import _ESTIMATED_TOKENS_PER_FILE
from app.core.constants import LLM_CONTEXT_WINDOW_MAX
from app.core.constants import LLM_CONTEXT_WINDOW_MIN
from app.services.llm_service import LLMConfigFailure
from app.services.llm_service import LLMConfigurationError

URL = "/api/chat/context/estimate"
SCOPE: dict[str, list[str]] = {
    "file_uuids": [],
    "collection_uuids": [],
    "tag_names": [],
    "speakers": [],
}
FROM_USER = "app.services.llm_service.LLMService.create_from_user_settings"
COUNT = "app.api.endpoints.chat.messages.count_scope_files"
FILES = 10
ESTIMATED = FILES * _ESTIMATED_TOKENS_PER_FILE


@pytest.fixture(autouse=True)
def _fixed_file_count():
    """Pin the scope size so the percentage assertions do not depend on the test database."""
    with patch(COUNT, return_value=FILES):
        yield


def _no_llm():
    return patch(FROM_USER, return_value=None)


def _llm_with_window(window: int):
    return patch(FROM_USER, return_value=SimpleNamespace(user_context_window=window))


def _unusable():
    return patch(FROM_USER, side_effect=LLMConfigurationError(LLMConfigFailure.CREDENTIALS))


def test_unknown_model_reports_null_not_8192(client, auth_headers):
    with _no_llm():
        response = client.post(URL, json=SCOPE, headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {
        "file_count": FILES,
        "estimated_tokens": ESTIMATED,
        "context_window": None,
        "context_window_source": "unknown",
        "context_window_unknown_reason": "llm_not_configured",
        "pct": None,
        "warning_level": "unknown",
    }


def test_unusable_config_is_reported_as_unusable_not_as_a_400(client, auth_headers):
    with _unusable():
        response = client.post(URL, json=SCOPE, headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["context_window"] is None
    assert body["context_window_source"] == "unknown"
    assert body["context_window_unknown_reason"] == "llm_config_unusable"
    assert body["warning_level"] == "unknown"


def test_caller_assumption_is_used_and_labelled(client, auth_headers):
    with _no_llm():
        response = client.post(
            URL, params={"assumed_context_window": 32000}, json=SCOPE, headers=auth_headers
        )

    body = response.json()
    assert body["context_window"] == 32000
    assert body["context_window_source"] == "assumed"
    # The reason is kept so the caller can still tell the window is not the model's own.
    assert body["context_window_unknown_reason"] == "llm_not_configured"
    assert body["pct"] == round(ESTIMATED / 32000 * 100, 1)
    assert body["warning_level"] == "ok"


def test_assumption_never_overrides_a_known_window(client, auth_headers):
    with _llm_with_window(60000):
        response = client.post(
            URL, params={"assumed_context_window": 8192}, json=SCOPE, headers=auth_headers
        )

    body = response.json()
    assert body["context_window"] == 60000
    assert body["context_window_source"] == "model"
    assert body["context_window_unknown_reason"] is None
    assert body["pct"] == round(ESTIMATED / 60000 * 100, 1)


@pytest.mark.parametrize(
    ("window", "level"),
    [(ESTIMATED * 10, "ok"), (int(ESTIMATED / 0.8), "warn"), (ESTIMATED - 1, "over")],
)
def test_known_window_keeps_the_warning_thresholds(client, auth_headers, window, level):
    with _llm_with_window(window):
        response = client.post(URL, json=SCOPE, headers=auth_headers)

    assert response.json()["warning_level"] == level


@pytest.mark.parametrize("bad", [LLM_CONTEXT_WINDOW_MIN - 1, LLM_CONTEXT_WINDOW_MAX + 1])
def test_out_of_range_assumption_is_422(client, auth_headers, bad):
    with _no_llm():
        response = client.post(
            URL, params={"assumed_context_window": bad}, json=SCOPE, headers=auth_headers
        )

    assert response.status_code == 422


def test_unknown_window_is_logged_with_reason_and_no_scope_content(client, auth_headers, caplog):
    scope = {**SCOPE, "tag_names": ["board-minutes-secret-tag"]}
    with _unusable(), caplog.at_level(logging.WARNING, logger="app.api.endpoints.chat.messages"):
        client.post(URL, json=scope, headers=auth_headers)

    records = [r for r in caplog.records if r.name == "app.api.endpoints.chat.messages"]
    assert len(records) == 1
    assert "llm_config_unusable" in records[0].getMessage()
    assert "board-minutes-secret-tag" not in records[0].getMessage()


def test_the_estimate_measures_against_the_conversations_pinned_model(
    client, auth_headers, db_session, normal_user
):
    """#1364 residual: a pinned model's window is the one the turn uses, not the default's."""
    import uuid as uuid_mod

    from app.models.user_llm_settings import UserLLMSettings

    pinned = UserLLMSettings(
        user_id=normal_user.id,
        name="pinned",
        provider="openai",
        model_name="gpt-pinned",
        max_tokens=200_000,
        temperature="0.3",
        uuid=uuid_mod.uuid4(),
    )
    db_session.add(pinned)
    db_session.commit()

    def from_config(_user_id, config_id):
        assert config_id == pinned.id
        return SimpleNamespace(user_context_window=200_000)

    with (
        _llm_with_window(8_000),
        patch("app.services.llm_service.LLMService.create_from_config_id", side_effect=from_config),
    ):
        response = client.post(
            URL, params={"llm_config_uuid": str(pinned.uuid)}, json=SCOPE, headers=auth_headers
        )

    assert response.status_code == 200, response.text
    assert response.json()["context_window"] == 200_000


def test_a_pinned_model_the_caller_may_not_use_is_a_404(client, auth_headers):
    import uuid as uuid_mod

    with _llm_with_window(8_000):
        response = client.post(
            URL, params={"llm_config_uuid": str(uuid_mod.uuid4())}, json=SCOPE, headers=auth_headers
        )
    assert response.status_code == 404
