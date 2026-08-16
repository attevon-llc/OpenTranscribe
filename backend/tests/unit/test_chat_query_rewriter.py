"""Conversational query rewriting, and the routing hint that rides along (#403 Stage 4).

This module had **no tests at all** before the intent line was added to it, which
is why the first one here is the property the whole design rests on: *there is
never a provider call made only to route*. Turn 1 carries no history, and turn 1
is where "summarize my meetings this week" lands — folding routing into the
rewrite would have added a round trip at exactly the wrong moment.

Every other test is a failure mode. The rewriter is an enhancement in the hot
path and must degrade to the original question on all of them.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.services.chat.query_rewriter import MAX_REWRITE_CHARS
from app.services.chat.query_rewriter import RewriteResult
from app.services.chat.query_rewriter import rewrite_query

pytestmark = pytest.mark.unit

QUESTION = "what did she say about the timeline?"
HISTORY = [
    {"role": "user", "content": "who owns the Atlas migration?"},
    {"role": "assistant", "content": "Dana Whitfield owns it."},
]


@dataclass
class _Response:
    content: str


class _RecordingLLM:
    """Returns a canned completion and records that it was asked."""

    def __init__(self, content: str = "") -> None:
        self.content = content
        self.calls: list[list[dict[str, str]]] = []

    def chat_completion(self, messages, **_kwargs):
        self.calls.append(messages)
        return _Response(self.content)


class _BrokenLLM:
    def __init__(self) -> None:
        self.calls = 0

    def chat_completion(self, messages, **_kwargs):  # noqa: ARG002
        self.calls += 1
        raise RuntimeError("provider exploded")


def test_turn_one_makes_no_provider_call_at_all():
    """The load-bearing property: routing never costs a round trip on turn 1."""
    llm = _RecordingLLM("something\nINTENT: aggregate")
    result = rewrite_query(llm, [], "summarize my meetings this week")

    assert llm.calls == []
    assert result == RewriteResult("summarize my meetings this week", None)


def test_no_llm_configured_returns_the_question_unchanged():
    assert rewrite_query(None, HISTORY, QUESTION) == RewriteResult(QUESTION, None)


def test_a_rewrite_and_its_intent_are_both_returned():
    llm = _RecordingLLM("What did Dana Whitfield say about the timeline?\nINTENT: lookup")
    result = rewrite_query(llm, HISTORY, QUESTION)

    assert result.query == "What did Dana Whitfield say about the timeline?"
    assert result.intent == "lookup"
    assert len(llm.calls) == 1


def test_the_intent_line_is_optional():
    """A model that ignores the second line must cost nothing."""
    llm = _RecordingLLM("What did Dana Whitfield say about the timeline?")
    result = rewrite_query(llm, HISTORY, QUESTION)

    assert result.query == "What did Dana Whitfield say about the timeline?"
    assert result.intent is None


def test_an_unrecognised_intent_is_discarded_not_propagated():
    llm = _RecordingLLM("What did Dana say about the timeline?\nINTENT: vibes")
    assert rewrite_query(llm, HISTORY, QUESTION).intent is None


def test_the_intent_line_never_leaks_into_the_search_query():
    """`_sanitize` takes line one; the second line must not reach retrieval."""
    llm = _RecordingLLM("What did Dana say about the timeline?\nINTENT: summarize")
    result = rewrite_query(llm, HISTORY, QUESTION)

    assert "INTENT" not in result.query
    assert result.intent == "summarize"


@pytest.mark.parametrize(
    "content",
    [
        "",
        "   ",
        "\nINTENT: lookup",
        "x" * (MAX_REWRITE_CHARS + 1),
    ],
)
def test_unusable_output_falls_back_to_the_original_question(content):
    llm = _RecordingLLM(content)
    assert rewrite_query(llm, HISTORY, QUESTION).query == QUESTION


def test_a_provider_failure_falls_back_and_does_not_raise():
    llm = _BrokenLLM()
    result = rewrite_query(llm, HISTORY, QUESTION)

    assert llm.calls == 1
    assert result == RewriteResult(QUESTION, None)


def test_history_and_question_are_concatenated_never_interpolated():
    """A transcript containing `{evil}` must not be formatted into the prompt."""
    llm = _RecordingLLM("rewritten")
    hostile = [{"role": "user", "content": "the value was {evil} and 100%s"}]
    rewrite_query(llm, hostile, QUESTION)

    sent = llm.calls[0][-1]["content"]
    assert "{evil}" in sent
    assert "100%s" in sent
