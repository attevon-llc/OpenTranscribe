"""The mock LLM's JSON-shaped scenarios: summary, topic suggestions and speaker identification.

``mock-gpt`` answers in markdown, so it cannot drive any feature whose parser wants JSON.
These scenarios exist so the summarization, topic-extraction and speaker-ID tasks can run
for real against a server that costs nothing. A scenario body that drifted from what its
parser reads would make every suite built on it fail for a reason that has nothing to do
with the code under test, so each body is checked here by feeding it to the APP's own parser,
not by comparing it with a copy of the expected shape.

Wiring only: the bodies are constants and say nothing about summary quality.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
import requests

from app.schemas.summary import SummaryData
from app.services.llm_service import LLMService
from app.services.llm_types import LLMConfig
from app.services.llm_types import LLMProvider
from app.services.llm_types import LLMResponse
from app.services.topic_extraction_service import TopicExtractionService

pytestmark = pytest.mark.unit

ECHO_FIELD = "mock_system_sha256"


def _root(mock_llm_url: str) -> str:
    """The server root; the request log lives beside ``/v1``, not under it."""
    return mock_llm_url.removesuffix("/v1")


def _chat(mock_llm_url: str, model: str, messages: list[dict[str, str]], **extra: Any) -> dict:
    response = requests.post(
        f"{mock_llm_url}/chat/completions",
        json={"model": model, "messages": messages, **extra},
        timeout=30,
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def _content(mock_llm_url: str, model: str, system: str = "system prompt") -> str:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": "go"}]
    return str(_chat(mock_llm_url, model, messages)["choices"][0]["message"]["content"])


def _service() -> LLMService:
    return LLMService(
        LLMConfig(provider=LLMProvider.CUSTOM, model="m", base_url="http://mock.invalid/v1")
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ------------------------------------------------- the bodies satisfy the app's own parsers


def test_the_summary_scenario_is_a_summary_the_app_accepts(mock_llm_url):
    content = _content(mock_llm_url, "mock-summary-json")

    summary = _service()._parse_summary_response(LLMResponse(content=content), len(content))

    assert "error" not in summary, summary
    parsed = SummaryData.model_validate(summary)
    assert parsed.bluf
    assert parsed.brief_summary
    # The default prompt's structured sections, each non-empty and of the right container type.
    assert isinstance(parsed.major_topics, list) and parsed.major_topics
    assert isinstance(parsed.action_items, list) and parsed.action_items
    assert isinstance(parsed.key_decisions, list) and parsed.key_decisions
    assert isinstance(parsed.follow_up_items, list) and parsed.follow_up_items


def test_the_topics_scenario_is_a_suggestion_the_extraction_parser_accepts(mock_llm_url):
    content = _content(mock_llm_url, "mock-topics-json")

    parsed = TopicExtractionService()._parse_llm_response(content, LLMProvider.CUSTOM)

    assert parsed is not None
    assert parsed.suggested_tags
    assert parsed.suggested_collections
    # Both sides of the auto-apply threshold (0.75) are present, so a test can tell an
    # applied suggestion from a merely stored one.
    confidences = sorted(tag.confidence for tag in parsed.suggested_tags)
    assert confidences[0] < 0.75 <= confidences[-1]


def test_the_speakers_scenario_yields_predictions_that_survive_validation(mock_llm_url):
    content = _content(mock_llm_url, "mock-speakers-json")

    result = _service()._parse_speaker_identification_response(LLMResponse(content=content))

    assert "error" not in result, result
    labels = [p["speaker_label"] for p in result["speaker_predictions"]]
    assert labels == ["SPEAKER_00", "SPEAKER_01"]
    # Validation drops non-names and low confidence; nothing the mock canned may be dropped.
    assert len(result["speaker_predictions"]) == 2


# ------------------------------------------------------- which prompt produced which reply


@pytest.mark.parametrize("model", ["mock-summary-json", "mock-topics-json", "mock-speakers-json"])
def test_the_reply_carries_the_digest_of_the_system_message_it_answered(mock_llm_url, model):
    system = "You are combining multiple section summaries."

    body = json.loads(_content(mock_llm_url, model, system=system))

    assert body[ECHO_FIELD] == _sha(system)


@pytest.mark.parametrize("model", ["mock-summary-json", "mock-topics-json", "mock-speakers-json"])
def test_two_different_system_messages_get_two_different_digests(mock_llm_url, model):
    first = json.loads(_content(mock_llm_url, model, system="slice prompt"))
    second = json.loads(_content(mock_llm_url, model, system="merge prompt"))

    assert first[ECHO_FIELD] != second[ECHO_FIELD]
    # Everything else is the same constant body, which is what makes the digest the only
    # thing that distinguishes the calls.
    assert {k: v for k, v in first.items() if k != ECHO_FIELD} == {
        k: v for k, v in second.items() if k != ECHO_FIELD
    }


def test_a_request_with_no_system_message_digests_the_empty_string(mock_llm_url):
    messages = [{"role": "user", "content": "go"}]

    body = json.loads(
        _chat(mock_llm_url, "mock-topics-json", messages)["choices"][0]["message"]["content"]
    )

    assert body[ECHO_FIELD] == _sha("")


def test_a_json_scenario_streams_the_same_body_it_returns_whole(mock_llm_url):
    system = "stream parity"
    whole = json.loads(_content(mock_llm_url, "mock-topics-json", system=system))

    response = requests.post(
        f"{mock_llm_url}/chat/completions",
        json={
            "model": "mock-topics-json",
            "stream": True,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": "go"}],
        },
        stream=True,
        timeout=30,
    )
    pieces: list[str] = []
    for raw in response.iter_lines():
        line = raw.decode("utf-8")
        if not line.startswith("data:") or line[5:].strip() == "[DONE]":
            continue
        delta = (json.loads(line[5:]).get("choices") or [{}])[0].get("delta", {})
        pieces.append(delta.get("content") or "")

    assert json.loads("".join(pieces)) == whole


# ------------------------------------------------------------------------ the request log


def test_the_request_log_records_each_call_and_can_be_cleared(mock_llm_url):
    root = _root(mock_llm_url)
    requests.post(f"{root}/_mock/requests/clear", timeout=10)
    system = "log probe system"
    messages = [{"role": "system", "content": system}, {"role": "user", "content": "log probe"}]

    _chat(mock_llm_url, "mock-summary-json", messages, max_tokens=123)
    _chat(mock_llm_url, "mock-gpt", messages)
    logged = requests.get(f"{root}/_mock/requests", timeout=10).json()["requests"]

    assert [entry["model"] for entry in logged] == ["mock-summary-json", "mock-gpt"]
    assert logged[0]["max_tokens"] == 123
    assert logged[0]["system_sha256"] == _sha(system)
    assert logged[0]["messages"] == messages

    requests.post(f"{root}/_mock/requests/clear", timeout=10)
    assert requests.get(f"{root}/_mock/requests", timeout=10).json()["requests"] == []


def test_a_failing_call_is_logged_too(mock_llm_url):
    """The log is how a test sees a call the provider rejected, so errors must appear in it."""
    root = _root(mock_llm_url)
    requests.post(f"{root}/_mock/requests/clear", timeout=10)

    response = requests.post(
        f"{mock_llm_url}/chat/completions",
        json={"model": "mock-error", "messages": [{"role": "user", "content": "x"}]},
        timeout=30,
    )

    assert response.status_code == 500
    logged = requests.get(f"{root}/_mock/requests", timeout=10).json()["requests"]
    assert [entry["model"] for entry in logged] == ["mock-error"]
