"""An LLM configuration that EXISTS but cannot be used is reported as such, not as "none".

``create_from_settings`` used to turn ``LLMConfigurationError`` into ``None``, so a deleted or
broken configuration looked exactly like "no LLM configured" in chat and in the bulk actions.
The person was then told to add a provider they already had. The fixed sentence per reason is
safe to show; a stable code lets the client choose its own copy.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.api.endpoints.files.management import _handle_identify_speakers_action
from app.api.endpoints.files.management import _handle_summarize_action
from app.services.llm_service import LLMConfigFailure
from app.services.llm_service import LLMConfigurationError
from app.services.llm_service import LLMService

RESOLVE = "app.services.llm_service.LLMService._resolve_user_llm_settings"


@pytest.fixture
def unusable():
    """The user's active configuration points at a row that no longer exists."""
    with patch(RESOLVE, side_effect=LLMConfigurationError(LLMConfigFailure.NOT_FOUND)):
        yield


def _conversation(client, headers) -> dict:
    response = client.post("/api/chat/conversations", json={}, headers=headers)
    assert response.status_code == 201, response.text
    created: dict = response.json()
    return created


def test_a_chat_turn_names_an_unusable_configuration(client, auth_headers, unusable):
    conversation = _conversation(client, auth_headers)
    with (
        patch("app.services.chat.limits.acquire_stream_slot", return_value="slot-test"),
        patch("app.services.chat.limits.release_stream_slot"),
    ):
        response = client.post(
            f"/api/chat/conversations/{conversation['uuid']}/messages",
            json={"content": "hi"},
            headers=auth_headers,
        )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "llm_config_unusable"
    assert detail["message"] == str(LLMConfigurationError(LLMConfigFailure.NOT_FOUND))


def test_the_default_factory_still_hides_it_unless_asked(unusable):
    # Everything else (status probe, topic extraction, the estimate) keeps the old contract.
    assert LLMService.create_from_settings(user_id=1) is None
    with pytest.raises(LLMConfigurationError):
        LLMService.create_from_settings(user_id=1, raise_on_unusable=True)


@pytest.mark.parametrize("handler", [_handle_summarize_action, _handle_identify_speakers_action])
def test_bulk_actions_report_an_unusable_configuration(handler, unusable, db_session):
    result = handler(db_session, "00000000-0000-0000-0000-000000000000", 1, user_id=1)

    assert result.success is False
    assert result.error == "LLM_CONFIG_UNUSABLE"
    assert result.message == str(LLMConfigurationError(LLMConfigFailure.NOT_FOUND))


@pytest.mark.parametrize("handler", [_handle_summarize_action, _handle_identify_speakers_action])
def test_bulk_actions_still_say_not_available_when_nothing_is_configured(handler, db_session):
    with (
        patch(RESOLVE, return_value=None),
        patch("app.services.llm_service.LLMService.create_from_system_settings", return_value=None),
    ):
        result = handler(db_session, "00000000-0000-0000-0000-000000000000", 1, user_id=1)

    assert result.success is False
    assert result.error == "LLM_NOT_AVAILABLE"
