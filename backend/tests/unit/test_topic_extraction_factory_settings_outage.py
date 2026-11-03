"""The topic-extraction factory passes a settings outage through instead of returning None.

``None`` means "no LLM is configured", and the API and the task both act on that: the API
tells the user to configure a provider, the task records a skip. A database error is not
that, so it must reach them.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy.exc import OperationalError

from app.services.llm_service import LLMService
from app.services.topic_extraction_service import TopicExtractionService


def test_topic_extraction_factory_raises_on_a_settings_outage():
    error = OperationalError("SELECT 1", {}, Exception("connection reset"))
    with (
        patch.object(LLMService, "create_from_settings", side_effect=error),
        pytest.raises(OperationalError),
    ):
        TopicExtractionService.create_from_settings(user_id=1)


def test_topic_extraction_factory_still_returns_none_without_any_llm():
    with patch.object(LLMService, "create_from_settings", return_value=None):
        assert TopicExtractionService.create_from_settings(user_id=1) is None


def test_topic_extraction_factory_still_returns_none_for_a_client_that_will_not_build():
    with patch.object(LLMService, "create_from_settings", side_effect=ValueError("bad config")):
        assert TopicExtractionService.create_from_settings(user_id=1) is None
