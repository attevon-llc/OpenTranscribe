"""Chat cloud-seam hooks (issue #52), mirroring the transcription seam tests.

The contract these lock in: a broken or hostile cloud hook can never break
community chat, but a quota rejection must still stop the message cleanly.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.services.chat import hooks
from tests.helpers import does_not_raise


@pytest.fixture(autouse=True)
def _clean_hooks():
    hooks.clear_hooks()
    yield
    hooks.clear_hooks()


def _dispatch_ctx() -> hooks.ChatDispatchContext:
    return hooks.ChatDispatchContext(
        conversation_uuid="conv-1",
        user_id=1,
        organization_id=None,
        provider="openai",
        model="gpt-4o-mini",
        request_id="msg-1",
    )


def _completion_ctx(success: bool = True) -> hooks.ChatCompletionContext:
    return hooks.ChatCompletionContext(
        conversation_uuid="conv-1",
        message_uuid="msg-1",
        user_id=1,
        organization_id=None,
        provider="openai",
        model="gpt-4o-mini",
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        tokens_estimated=False,
        retrieved_chunks=4,
        success=success,
    )


def test_no_hooks_registered_is_a_noop():
    """Community edition: zero hooks, zero overhead, no errors."""
    hooks.fire_before_message(_dispatch_ctx())
    with does_not_raise("dispatching with no registered hooks is a no-op"):
        hooks.fire_message_complete(_completion_ctx())


def test_before_message_hook_receives_context():
    seen: list = []
    hooks.register_before_message(seen.append)
    hooks.fire_before_message(_dispatch_ctx())
    assert seen[0].conversation_uuid == "conv-1"


def test_quota_error_propagates_to_block_the_message():
    def deny(_ctx):
        raise hooks.ChatQuotaExceededError()

    hooks.register_before_message(deny)
    with pytest.raises(hooks.ChatQuotaExceededError) as exc:
        hooks.fire_before_message(_dispatch_ctx())
    assert exc.value.status_code == 402


def test_other_before_hook_failures_are_contained():
    """A broken cloud layer must not stop community users chatting."""
    calls: list = []

    def broken(_ctx):
        raise RuntimeError("metering service down")

    hooks.register_before_message(broken)
    hooks.register_before_message(calls.append)

    hooks.fire_before_message(_dispatch_ctx())  # must not raise
    assert len(calls) == 1  # the healthy hook still ran


def test_completion_hook_failures_are_always_contained():
    """A metering problem must never turn a delivered answer into an error."""

    def broken(_ctx):
        raise RuntimeError("usage spine down")

    hooks.register_message_complete(broken)
    with does_not_raise("a failing completion hook must never break the chat response"):
        hooks.fire_message_complete(_completion_ctx())  # must not raise


def test_completion_context_carries_metering_fields():
    seen: list = []
    hooks.register_message_complete(seen.append)
    hooks.fire_message_complete(_completion_ctx())

    ctx = seen[0]
    assert ctx.message_uuid == "msg-1"  # idempotency scope
    assert ctx.prompt_tokens == 100
    assert ctx.completion_tokens == 50
    assert ctx.tokens_estimated is False


def test_contexts_are_frozen_so_hooks_cannot_mutate_them():
    ctx = _dispatch_ctx()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.user_id = 999  # type: ignore[misc]


def test_clear_hooks_removes_everything():
    hooks.register_before_message(lambda _ctx: None)
    hooks.register_message_complete(lambda _ctx: None)
    hooks.clear_hooks()
    assert hooks._before_message_hooks == []
    assert hooks._message_complete_hooks == []


def test_chat_rag_capability_is_enabled_and_classified():
    """Every capability key must be classified, and chat ships on in community."""
    from app.core.capabilities import CAPABILITY_AUDIENCE
    from app.core.capabilities import COMMUNITY_CAPABILITIES

    assert COMMUNITY_CAPABILITIES["chat.rag"] is True
    assert CAPABILITY_AUDIENCE["chat.rag"] == "user"
