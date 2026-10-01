"""Multi-section summary merge must not let a near-empty slice erase the meeting (#941).

A long recording is split into slices by ``_chunk_transcript_intelligently``, each
slice summarized by ``_summarize_section``, and the results merged by
``_combine_sections``. Before #941 the slice prompt and the merge prompt carried no
notion of "this is one slice of a longer recording": a tiny trailing slice holding
only crosstalk reported "no decision, no action", and the merge adopted that
framing, returning an empty summary for a meeting whose first slice had topics,
decisions and action items.

The fix is instruction-level, so these tests pin the instructions and payload
shape the model actually receives:

* every slice's system prompt carries the scope rules (``SECTION_SCOPE_RULES``);
* the merge's system prompt carries the union rules (``SECTION_MERGE_RULES``);
* the merge payload numbers each slice (``section``/``of``) in chronological order;
* every failure path (unparseable JSON, exception while processing) marks the slice
  ``"_error": true`` so a technical failure is never read as "nothing happened".
"""

from __future__ import annotations

import json
from typing import Any
from unittest import mock

from app.services.llm_service import LLMConfig
from app.services.llm_service import LLMProvider
from app.services.llm_service import LLMResponse
from app.services.llm_service import LLMService

VALID_SUMMARY = json.dumps(
    {
        "bluf": "Budget approved.",
        "brief_summary": "Team approved the budget.",
        "major_topics": [],
        "action_items": [],
        "key_decisions": [],
        "follow_up_items": [],
    }
)


def _service() -> LLMService:
    return LLMService(LLMConfig(provider=LLMProvider.CUSTOM, model="m", base_url="http://x/v1"))


def _response(content: str) -> LLMResponse:
    return LLMResponse(content=content, finish_reason="stop")


def _system_prompt(call: Any) -> str:
    messages = call.args[0]
    assert messages[0]["role"] == "system"
    return str(messages[0]["content"])


def _merge_payload(call: Any) -> list[dict[str, Any]]:
    """Extract the JSON array of labelled slices from the merge's user message."""
    user_content = call.args[0][1]["content"]
    payload: list[dict[str, Any]] = json.loads(user_content[user_content.index("[") :])
    return payload


def test_slice_prompt_scopes_the_model_to_its_own_span():
    service = _service()
    with mock.patch.object(
        service, "chat_completion", return_value=_response('{"key_points": []}')
    ) as chat:
        service._summarize_section("I can't hear you... bye", 2, 2, None, "{transcript}")

    system = _system_prompt(chat.call_args)
    assert "section 2 of 2" in system
    assert "one slice of a longer recording" in system
    assert "never draw a conclusion about the recording as a whole" in system


def test_merge_prompt_requires_a_union_and_numbers_the_slices():
    service = _service()
    substantive = {"key_points": ["Budget approved"], "decisions": ["Approve budget"]}
    silent = {"key_points": ["No decision recorded"], "decisions": []}

    with mock.patch.object(
        service, "chat_completion", return_value=_response(VALID_SUMMARY)
    ) as chat:
        service._combine_sections([substantive, silent], None, "{transcript}", 2)

    call = chat.call_args
    system = _system_prompt(call)
    assert "Take the union of their facts" in system
    assert "can never cancel, weaken or reframe" in system

    assert _merge_payload(call) == [
        {"section": 1, "of": 2, "summary": substantive},
        {"section": 2, "of": 2, "summary": silent},
    ]


def test_unparseable_slice_is_flagged_as_an_error_not_as_empty():
    service = _service()
    with (
        mock.patch.object(service, "chat_completion", return_value=_response("not json at all")),
        mock.patch("json_repair.loads", return_value=""),
    ):
        result = service._summarize_section("chunk", 1, 3, None, "{transcript}")

    assert result["_error"] is True
    assert result["decisions"] == []


def test_slice_that_raises_reaches_the_merge_flagged_as_an_error():
    """The exception path must carry the same ``_error`` flag as the parse-failure path."""
    service = _service()

    def _summarize(chunk: str, section_num: int, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if section_num == 2:
            raise TimeoutError("provider timed out")
        return {"key_points": ["Budget approved"], "decisions": ["Approve budget"]}

    captured: dict[str, list[dict[str, Any]]] = {}

    def _combine(sections: list[dict[str, Any]], *args: Any, **kwargs: Any) -> dict[str, Any]:
        captured["sections"] = sections
        return {}

    with (
        mock.patch.object(service, "_summarize_section", side_effect=_summarize),
        mock.patch.object(service, "_combine_sections", side_effect=_combine),
    ):
        service._process_multiple_chunks(["first", "second"], None, "{transcript}")

    first, second = captured["sections"]
    assert "_error" not in first
    assert second["_error"] is True
    assert second["key_points"] == ["Section 2: Processing failed (TimeoutError)"]
