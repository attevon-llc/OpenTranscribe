"""Chat usage accounting and cost estimation.

Usage tracking is a core (open-source) feature: a self-hoster paying an LLM bill
wants the same visibility a hosted tenant does. These tests pin the properties
that make the numbers trustworthy — correct separation of cache tokens, honest
handling of unpriced models, and idempotency — because a usage figure nobody can
trust is worse than no usage figure at all.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.services.chat.hooks import ChatCompletionContext
from app.services.chat.pricing import estimate_cost_usd
from app.services.chat.pricing import get_rate
from app.services.chat.usage import EVENT_TYPE_CHAT_TOKENS
from app.services.chat.usage import record_chat_usage
from tests.helpers import does_not_raise


def _ctx(**overrides: Any) -> ChatCompletionContext:
    base: dict[str, Any] = {
        "conversation_uuid": "conv-1",
        "message_uuid": "msg-1",
        "user_id": 7,
        "organization_id": None,
        "provider": "anthropic",
        "model": "claude-haiku-4-5",
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "total_tokens": 1200,
        "tokens_estimated": False,
        "retrieved_chunks": 12,
        "success": True,
    }
    base.update(overrides)
    return ChatCompletionContext(**base)


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------


def test_known_model_prices_input_and_output_separately():
    # Haiku 4.5: $1/MTok in, $5/MTok out.
    cost = estimate_cost_usd(
        provider="anthropic",
        model="claude-haiku-4-5",
        prompt_tokens=1_000_000,
        completion_tokens=1_000_000,
    )
    assert cost == Decimal(6)


def test_cache_reads_are_cheaper_than_uncached_input():
    """Cache reads bill ~0.1x input. Pricing them as input would overstate the bill
    on every cache-enabled deployment.
    """
    uncached = estimate_cost_usd(
        provider="anthropic",
        model="claude-haiku-4-5",
        prompt_tokens=1_000_000,
        completion_tokens=0,
    )
    cached = estimate_cost_usd(
        provider="anthropic",
        model="claude-haiku-4-5",
        prompt_tokens=0,
        completion_tokens=0,
        cache_read_tokens=1_000_000,
    )
    assert uncached is not None and cached is not None
    assert cached < uncached


def test_cache_writes_are_more_expensive_than_uncached_input():
    """Cache writes bill ~1.25x input — the direction people get backwards."""
    uncached = estimate_cost_usd(
        provider="anthropic",
        model="claude-haiku-4-5",
        prompt_tokens=1_000_000,
        completion_tokens=0,
    )
    written = estimate_cost_usd(
        provider="anthropic",
        model="claude-haiku-4-5",
        prompt_tokens=0,
        completion_tokens=0,
        cache_write_tokens=1_000_000,
    )
    assert uncached is not None and written is not None
    assert written > uncached


def test_unknown_model_is_unpriced_not_free():
    """A confident $0.00 is a worse answer than an honest blank."""
    assert (
        estimate_cost_usd(
            provider="anthropic",
            model="some-model-we-have-no-rate-for",
            prompt_tokens=1000,
            completion_tokens=100,
        )
        is None
    )


def test_bedrock_is_not_priced_off_the_first_party_rate_card():
    """Bedrock is AWS-operated with its own pricing. Reusing Anthropic's published
    rates would produce a confidently wrong number, which is worse than none.
    """
    assert get_rate("bedrock", "anthropic.claude-haiku-4-5-20251001-v1:0") is None
    assert (
        estimate_cost_usd(
            provider="bedrock",
            model="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            prompt_tokens=1000,
            completion_tokens=100,
        )
        is None
    )


@pytest.mark.parametrize("provider", ["ollama", "vllm"])
def test_local_runtimes_are_explicitly_free(provider):
    """Free and unpriced are different states, and the UI shows them differently."""
    cost = estimate_cost_usd(
        provider=provider, model="llama3", prompt_tokens=999_999, completion_tokens=999_999
    )
    assert cost == Decimal(0)


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------


def test_usage_is_recorded_with_the_message_uuid_as_idempotency_key():
    """A retried or replayed completion must not double-count."""
    with (
        patch("app.db.session_utils.session_scope") as scope,
        patch("app.services.usage_service.record_event") as record,
    ):
        scope.return_value.__enter__.return_value = MagicMock()
        record_chat_usage(_ctx(message_uuid="msg-abc"))

    assert record.call_args.kwargs["idempotency_key"] == "chat:msg-abc"
    assert record.call_args.kwargs["event_type"] == EVENT_TYPE_CHAT_TOKENS


def test_recorded_metadata_separates_cache_tokens_and_flags_estimates():
    with (
        patch("app.db.session_utils.session_scope") as scope,
        patch("app.services.usage_service.record_event") as record,
    ):
        scope.return_value.__enter__.return_value = MagicMock()
        record_chat_usage(
            _ctx(cache_read_tokens=900, cache_write_tokens=100, tokens_estimated=True)
        )

    meta = record.call_args.kwargs["metadata"]
    assert meta["cache_read_tokens"] == 900
    assert meta["cache_write_tokens"] == 100
    # Estimated counts must be distinguishable from measured ones — never price off a guess.
    assert meta["tokens_estimated"] is True


def test_use_context_is_recorded():
    """The single most useful signal for telling a heavy legitimate user apart from
    someone using the deployment as a general-purpose chatbot.
    """
    with (
        patch("app.db.session_utils.session_scope") as scope,
        patch("app.services.usage_service.record_event") as record,
    ):
        scope.return_value.__enter__.return_value = MagicMock()
        record_chat_usage(_ctx(use_context=False))

    assert record.call_args.kwargs["metadata"]["use_context"] is False


def test_quantity_is_tokens_not_currency():
    """Numeric(12,3) gives only $0.001 resolution, so a cheap message would round to
    zero. Tokens are also provider-neutral and need no repricing.
    """
    with (
        patch("app.db.session_utils.session_scope") as scope,
        patch("app.services.usage_service.record_event") as record,
    ):
        scope.return_value.__enter__.return_value = MagicMock()
        record_chat_usage(_ctx(total_tokens=1234))

    assert record.call_args.kwargs["quantity"] == Decimal(1234)
    assert record.call_args.kwargs["unit"] == "tokens"


def test_recording_failure_never_breaks_chat():
    """Accounting is strictly subordinate to the feature that emits it."""
    with (
        patch("app.db.session_utils.session_scope") as scope,
        patch(
            "app.services.usage_service.record_event", side_effect=RuntimeError("db down")
        ) as record_event,
    ):
        scope.return_value.__enter__.return_value = MagicMock()
        with does_not_raise("a usage-recording failure must never break the chat response"):
            record_chat_usage(_ctx())

    # Prove the failing dependency was reached; otherwise containment is unproven.
    record_event.assert_called_once()


def test_a_failed_exchange_is_still_recorded():
    """Provider tokens are consumed even when the answer errored, so the usage is
    real and must be visible rather than silently dropped.
    """
    with (
        patch("app.db.session_utils.session_scope") as scope,
        patch("app.services.usage_service.record_event") as record,
    ):
        scope.return_value.__enter__.return_value = MagicMock()
        record_chat_usage(_ctx(success=False))

    assert record.called
    assert record.call_args.kwargs["metadata"]["success"] is False
