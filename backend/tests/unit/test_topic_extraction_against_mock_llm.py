"""Topic extraction driven end to end against the mock LLM, the way the nlp worker drives it.

``mock-gpt`` answers in markdown, so a topic request sent to it can never carry the JSON the
parser needs. These tests send the REAL extraction prompt (not a hand-built reply) to each
scenario model and feed what comes back through the real parser.
"""

from __future__ import annotations

from typing import Any

import pytest
import requests

from app.services.llm_service import LLMService
from app.services.llm_types import LLMConfig
from app.services.llm_types import LLMProvider
from app.services.llm_types import LLMResponse
from app.services.topic_extraction_service import TopicExtractionService

pytestmark = pytest.mark.unit

TRANSCRIPT = "SPEAKER_00: We reviewed the schedule and agreed on the planning milestones."


class _WireLLM(LLMService):
    """A real ``LLMService`` whose completion call skips the Redis token-budget reservation.

    The budget check is not what is under test; the request the service builds and the reply
    the mock returns still cross the wire unchanged.
    """

    def __init__(self, url: str, model: str) -> None:
        super().__init__(LLMConfig(provider=LLMProvider.CUSTOM, model=model, base_url=url))
        self._url = url

    def chat_completion(self, messages: list[dict[str, str]], **kwargs: Any) -> LLMResponse:
        response = requests.post(
            f"{self._url}/chat/completions",
            json={"model": self.config.model, "messages": messages, **kwargs},
            timeout=30,
        )
        response.raise_for_status()
        return LLMResponse(content=response.json()["choices"][0]["message"]["content"])


def _extract(mock_llm_url: str, model: str):
    llm = _WireLLM(mock_llm_url, model)
    return TopicExtractionService()._call_llm_for_extraction(llm, TRANSCRIPT, 1, 60.0)


def test_the_topics_scenario_answers_the_real_extraction_prompt_with_suggestions(mock_llm_url):
    parsed = _extract(mock_llm_url, "mock-topics-json")

    assert parsed is not None
    assert [t.name for t in parsed.suggested_tags] == ["mock-planning", "mock-schedule"]
    assert [c.name for c in parsed.suggested_collections] == ["Mock Planning Sessions"]


def test_mock_gpt_answers_the_real_topic_prompt_with_suggestions(mock_llm_url):
    # The app's topic prompt spells out its JSON schema, so mock-gpt answers it the way a
    # real model would (issue #1308) instead of with the markdown chat reply.
    parsed = _extract(mock_llm_url, "mock-gpt")

    assert parsed is not None
    assert [c.name for c in parsed.suggested_collections] == ["Mock Planning Sessions"]


def test_a_markdown_chat_reply_is_not_a_topic_answer():
    # The parser must still reject prose rather than invent suggestions.
    reply = "Based on the transcript excerpts provided, here is what I found [1]."
    assert TopicExtractionService()._parse_llm_response(reply, LLMProvider.CUSTOM) is None
