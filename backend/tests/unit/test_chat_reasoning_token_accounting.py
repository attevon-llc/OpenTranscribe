"""Separating reasoning out of the answer must not delete it from the meter (#439).

When a provider reports usage, its `completion_tokens` already covers reasoning —
the model generated those tokens and was billed for them. When it reports nothing
(the "custom" OpenAI-clones that cannot be sent `stream_options.include_usage`),
`_finalize_turn` estimates instead, and that estimate used to read `turn.answer`
alone. Routing reasoning to its own event type so it stops rendering would then
have quietly *under*-reported every reasoning model's usage.

Drives the real `_finalize_turn`. Everything it does after estimating —
persistence, hooks, audit — is individually try/except-contained by contract, so
it runs to completion against stubs with no database.
"""

from __future__ import annotations

import pytest

from app.services.chat.service import ChatTurn
from app.services.chat.service import _finalize_turn

ANSWER = "The team chose three buttons [1]."
REASONING = "Let me check excerpt [1] before I answer that."


class _StubConfig:
    class provider:  # noqa: N801 - mirrors LLMProvider's .value access
        value = "vllm"

    model = "gemma-4-e4b"


class _StubLLM:
    """Counts characters, so the arithmetic in the assertions is exact."""

    config = _StubConfig()

    def estimate_tokens(self, text: str) -> int:
        return len(text)


async def _finalize(turn: ChatTurn) -> None:
    await _finalize_turn(
        turn=turn,
        llm=_StubLLM(),
        messages=[{"role": "user", "content": "q"}],
        masked_count=0,
        conversation_id=1,
        conversation_uuid="conv-uuid",
        assistant_message_uuid="msg-uuid",
        user_id=1,
        organization_id=None,
        is_first_exchange=False,
        question="q",
        started=0.0,
        use_context=True,
    )


@pytest.mark.asyncio
async def test_estimated_completion_tokens_include_the_reasoning_the_model_produced():
    turn = ChatTurn()
    turn.answer_parts.append(ANSWER)
    turn.reasoning_parts.append(REASONING)

    await _finalize(turn)

    assert turn.tokens_estimated is True
    assert turn.completion_tokens == len(ANSWER) + len(REASONING)
    assert turn.completion_tokens > len(ANSWER), "reasoning was dropped from the meter"


@pytest.mark.asyncio
async def test_a_turn_without_reasoning_is_metered_exactly_as_before():
    """Control: the change must be invisible to non-reasoning models."""
    turn = ChatTurn()
    turn.answer_parts.append(ANSWER)

    await _finalize(turn)

    assert turn.completion_tokens == len(ANSWER)


@pytest.mark.asyncio
async def test_provider_reported_usage_is_never_overwritten_by_the_estimate():
    """Real counts win; they already include reasoning."""
    turn = ChatTurn()
    turn.answer_parts.append(ANSWER)
    turn.reasoning_parts.append(REASONING)
    turn.prompt_tokens = 26
    turn.completion_tokens = 86

    await _finalize(turn)

    assert turn.tokens_estimated is False
    assert turn.completion_tokens == 86
    assert turn.total_tokens == 26 + 86
