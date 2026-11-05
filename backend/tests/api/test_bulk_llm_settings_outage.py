"""A bulk summarize or identify-speakers action reports a settings outage as an outage.

The availability probe used to fold a database error into "LLM provider is not configured",
which sends the person off to set up a provider they already have.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy.exc import OperationalError

from app.api.endpoints.files.bulk_action_handlers import _handle_identify_speakers_action
from app.api.endpoints.files.bulk_action_handlers import _handle_summarize_action

RESOLVE = "app.services.llm_service.LLMService._resolve_user_llm_settings"
UNREADABLE = "AI provider settings could not be read. Try again in a moment."
LEAK_MARKER = "SELECT secret_column FROM user_llm_settings"


@pytest.mark.parametrize("handler", [_handle_summarize_action, _handle_identify_speakers_action])
def test_bulk_llm_actions_report_a_settings_outage(handler, db_session):
    error = OperationalError(LEAK_MARKER, {}, Exception("connection reset"))
    with patch(RESOLVE, side_effect=error):
        result = handler(db_session, "00000000-0000-0000-0000-000000000000", 1, user_id=1)

    assert result.success is False
    assert result.error == "LLM_SETTINGS_UNAVAILABLE"
    assert result.message == UNREADABLE
    assert LEAK_MARKER not in result.model_dump_json()
