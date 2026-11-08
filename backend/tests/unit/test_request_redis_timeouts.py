"""Request-path Redis calls are time-bounded, and a reply checks for Stop sparingly (audit M1/M2).

The shared ``get_redis()`` client keeps redis-py's unbounded socket defaults on purpose
(blocking readers use it). Calls made while serving a request must not: a Redis that
accepts the connection and never answers would hold the calling thread, or the event
loop for slowapi's synchronous check on an async route, forever. And a streaming chat
reply used to read its Stop flag from Redis once per TOKEN, on the event loop.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import cast

import pytest
from limits.storage import RedisStorage

from app.core import chat_defaults
from app.core import redis as redis_core
from app.core.config import settings
from app.core.redaction_config import EffectiveRedactionConfig
from app.services.chat import service as chat_service
from app.services.chat.output_redactor import OutputRedactor
from app.services.chat.turn import ChatTurn
from app.services.llm_stream import LLMStreamEvent
from app.services.llm_types import ChatLLM


@pytest.fixture
def fresh_request_client(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_REQUEST_SOCKET_TIMEOUT_SECONDS", 1.5)
    monkeypatch.setattr(settings, "REDIS_REQUEST_CONNECT_TIMEOUT_SECONDS", 0.75)
    redis_core.get_request_redis.cache_clear()
    yield redis_core.get_request_redis()
    redis_core.get_request_redis.cache_clear()


def test_the_request_client_carries_the_configured_timeouts(fresh_request_client):
    kwargs = fresh_request_client.connection_pool.connection_kwargs
    assert kwargs["socket_timeout"] == 1.5
    assert kwargs["socket_connect_timeout"] == 0.75


def test_control_the_shared_client_stays_unbounded():
    """The shared client is deliberately unbounded; the request client is the bounded one."""
    kwargs = redis_core.get_redis().connection_pool.connection_kwargs
    assert kwargs.get("socket_timeout") is None


def test_the_rate_limiter_storage_carries_the_configured_timeouts(monkeypatch):
    from app.auth import rate_limit

    monkeypatch.setattr(settings, "REDIS_REQUEST_SOCKET_TIMEOUT_SECONDS", 1.5)
    monkeypatch.setattr(settings, "REDIS_REQUEST_CONNECT_TIMEOUT_SECONDS", 0.75)
    monkeypatch.setattr(rate_limit, "_redis_reachable", lambda: True)

    limiter = rate_limit._create_limiter()

    storage = limiter._storage
    assert isinstance(storage, RedisStorage)
    kwargs = storage.storage.connection_pool.connection_kwargs
    assert kwargs["socket_timeout"] == 1.5
    assert kwargs["socket_connect_timeout"] == 0.75


def test_chat_limits_never_use_the_unbounded_client(monkeypatch):
    from app.services.chat import limits

    def _unbounded():
        raise AssertionError("chat limits reached the unbounded shared client")

    class _Client:
        def get(self, _key):
            return b"1"

    monkeypatch.setattr(redis_core, "get_redis", _unbounded)
    monkeypatch.setattr(redis_core, "get_request_redis", lambda: _Client())

    assert limits.is_cancelled("message-uuid") is True


# --------------------------------------------------------------------------------------
# The Stop flag is polled, not read per token.
# --------------------------------------------------------------------------------------

TOKENS = 200


class _FastLLM:
    """Emits every token at once, like a fast local model."""

    def chat_completion_stream(self, messages, cancel_event=None, **_kwargs):
        for _ in range(TOKENS):
            yield LLMStreamEvent(type="delta", text="x")
        yield LLMStreamEvent(type="done", finish_reason="stop")


async def _stream_with_counted_cancel_reads(monkeypatch) -> tuple[int, int]:
    reads: list[float] = []

    def _is_cancelled(_uuid: str) -> bool:
        reads.append(time.monotonic())
        return False

    monkeypatch.setattr(chat_service.limits, "is_cancelled", _is_cancelled)
    inactive = cast(
        "EffectiveRedactionConfig", SimpleNamespace(enabled=False, enabled_categories=set())
    )
    frames = [
        frame
        async for frame in chat_service._answer_frames(
            cast("ChatLLM", _FastLLM()),
            [{"role": "user", "content": "hi"}],
            {},
            turn=ChatTurn(),
            cancel_event=threading.Event(),
            answer_redactor=OutputRedactor(inactive),
            reasoning_redactor=OutputRedactor(inactive),
            started=time.monotonic(),
            assistant_message_uuid="message-uuid",
            conversation_uuid="conversation-uuid",
        )
    ]
    deltas = sum(1 for frame in frames if frame.startswith("event: delta"))
    return len(reads), deltas


@pytest.mark.asyncio
async def test_a_fast_stream_reads_the_stop_flag_once_not_per_token(monkeypatch):
    reads, deltas = await _stream_with_counted_cancel_reads(monkeypatch)

    assert deltas == TOKENS
    assert reads == 1, f"the Stop flag was read {reads} times for {TOKENS} tokens"


@pytest.mark.asyncio
async def test_control_with_no_interval_every_token_reads_the_flag(monkeypatch):
    """Proves the counter sees every read, so the test above is not vacuous."""
    monkeypatch.setattr(chat_defaults, "CHAT_CANCEL_POLL_INTERVAL_S", 0.0)

    reads, _deltas = await _stream_with_counted_cancel_reads(monkeypatch)

    assert reads >= TOKENS
