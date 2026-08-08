"""Streaming response parsers for the LLM providers OpenTranscribe supports.

``LLMService.chat_completion`` returns a whole answer at once, which is right for
Celery tasks (summaries, speaker identification) but wrong for interactive chat —
the RAG chat surface streams tokens to the browser as they arrive.

Everything here is a **pure function over decoded lines**, deliberately kept out of
``llm_service.py``: the wire formats differ per provider and are the part most worth
unit-testing against canned byte streams (no HTTP, no mocking gymnastics).

Three wire formats:
  - OpenAI-style SSE (openai / vllm / openrouter / custom OpenAI-compatible)
  - Anthropic event-typed SSE
  - Ollama newline-delimited JSON

Every parser normalizes to :class:`LLMStreamEvent` so callers stay provider-agnostic.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from collections.abc import Iterator
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Providers whose payloads accept ``stream_options: {"include_usage": true}``.
# Deliberately excludes "custom": many OpenAI-clone servers reject unknown
# payload keys outright, and a 400 there would break chat for self-hosters.
# Those providers fall back to estimated token counts.
USAGE_OPTION_PROVIDERS = frozenset({"openai", "vllm", "openrouter"})


@dataclass(frozen=True)
class LLMStreamEvent:
    """One normalized event from a provider's token stream.

    Attributes:
        type: ``"delta"`` (text chunk), ``"usage"`` (token counts),
            ``"done"`` (stream finished) or ``"error"``.
        text: Text fragment, for ``delta`` events.
        prompt_tokens: Input tokens reported by the provider, for ``usage``.
        completion_tokens: Output tokens reported by the provider, for ``usage``.
        cache_read_tokens: Input tokens served from a prompt cache, for ``usage``.
            Billed far below the uncached rate, so metering that ignores this
            over-reports cost on any cache-enabled deployment.
        cache_write_tokens: Input tokens written to a prompt cache, for ``usage``.
            Billed *above* the uncached rate, so it must not be folded into
            ``prompt_tokens`` either.
        finish_reason: Provider stop reason, for ``done``.
        message: Human-readable detail, for ``error``.
    """

    type: str
    text: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    finish_reason: str | None = None
    message: str = ""


def _loads(raw: str) -> dict | None:
    """Parse one JSON payload, tolerating provider noise (never raises)."""
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.debug("Discarding unparseable stream payload (%d chars)", len(raw))
        return None
    return parsed if isinstance(parsed, dict) else None


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


def parse_openai_sse(lines: Iterable[str]) -> Iterator[LLMStreamEvent]:
    """Parse an OpenAI-compatible ``chat/completions`` SSE stream.

    Frames are ``data: {json}`` with a ``data: [DONE]`` sentinel. Content arrives on
    ``choices[0].delta.content``; usage arrives either on a final usage-only chunk
    (when ``stream_options.include_usage`` was accepted) or not at all.

    Args:
        lines: Decoded lines from the HTTP response.

    Yields:
        Normalized stream events, ending with exactly one ``done``.
    """
    finish_reason: str | None = None
    for line in lines:
        if not line or not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload:
            continue
        if payload == "[DONE]":
            break

        chunk = _loads(payload)
        if chunk is None:
            continue

        # Some gateways surface mid-stream failures as an error object.
        if isinstance(chunk.get("error"), dict):
            yield LLMStreamEvent(
                type="error", message=str(chunk["error"].get("message", "provider error"))
            )
            return

        choices = chunk.get("choices") or []
        if choices and isinstance(choices[0], dict):
            choice = choices[0]
            content = (choice.get("delta") or {}).get("content")
            if content:
                yield LLMStreamEvent(type="delta", text=str(content))
            if choice.get("finish_reason"):
                finish_reason = str(choice["finish_reason"])

        usage = chunk.get("usage")
        if isinstance(usage, dict):
            yield LLMStreamEvent(
                type="usage",
                prompt_tokens=_int_or_none(usage.get("prompt_tokens")),
                completion_tokens=_int_or_none(usage.get("completion_tokens")),
            )

    yield LLMStreamEvent(type="done", finish_reason=finish_reason)


def parse_anthropic_sse(lines: Iterable[str]) -> Iterator[LLMStreamEvent]:
    """Parse an Anthropic Messages API SSE stream.

    Anthropic sends ``event:``/``data:`` pairs. The payload's own ``type`` field is
    authoritative (the ``event:`` line merely repeats it), so this parser reads the
    JSON and ignores the event name. Prompt tokens land on ``message_start``,
    completion tokens on ``message_delta``.

    Args:
        lines: Decoded lines from the HTTP response.

    Yields:
        Normalized stream events, ending with exactly one ``done``.
    """
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None

    for line in lines:
        if not line or not line.startswith("data:"):
            continue  # "event:" lines and blank separators carry no extra information
        payload = line[len("data:") :].strip()
        if not payload:
            continue

        chunk = _loads(payload)
        if chunk is None:
            continue

        chunk_type = chunk.get("type")
        if chunk_type == "error":
            detail = chunk.get("error") or {}
            yield LLMStreamEvent(type="error", message=str(detail.get("message", "provider error")))
            return
        if chunk_type == "message_start":
            usage = (chunk.get("message") or {}).get("usage") or {}
            prompt_tokens = _int_or_none(usage.get("input_tokens"))
            completion_tokens = _int_or_none(usage.get("output_tokens"))
        elif chunk_type == "content_block_delta":
            text = (chunk.get("delta") or {}).get("text")
            if text:
                yield LLMStreamEvent(type="delta", text=str(text))
        elif chunk_type == "message_delta":
            stop_reason = (chunk.get("delta") or {}).get("stop_reason")
            if stop_reason:
                finish_reason = str(stop_reason)
            usage = chunk.get("usage") or {}
            if usage.get("output_tokens") is not None:
                completion_tokens = _int_or_none(usage.get("output_tokens"))
        elif chunk_type == "message_stop":
            break
        # "ping" and content_block_start/stop carry nothing we need.

    if prompt_tokens is not None or completion_tokens is not None:
        yield LLMStreamEvent(
            type="usage", prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        )
    yield LLMStreamEvent(type="done", finish_reason=finish_reason)


def parse_ollama_ndjson(lines: Iterable[str]) -> Iterator[LLMStreamEvent]:
    """Parse an Ollama ``/api/chat`` newline-delimited JSON stream.

    One JSON object per line; content on ``message.content``. The terminal line
    (``done: true``) carries ``prompt_eval_count`` / ``eval_count``, which Ollama
    reports exactly — no estimation needed.

    Args:
        lines: Decoded lines from the HTTP response.

    Yields:
        Normalized stream events, ending with exactly one ``done``.
    """
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None

    for line in lines:
        if not line or not line.strip():
            continue
        chunk = _loads(line.strip())
        if chunk is None:
            continue

        if chunk.get("error"):
            yield LLMStreamEvent(type="error", message=str(chunk["error"]))
            return

        content = (chunk.get("message") or {}).get("content")
        if content:
            yield LLMStreamEvent(type="delta", text=str(content))

        if chunk.get("done"):
            prompt_tokens = _int_or_none(chunk.get("prompt_eval_count"))
            completion_tokens = _int_or_none(chunk.get("eval_count"))
            finish_reason = str(chunk.get("done_reason") or "stop")
            break

    if prompt_tokens is not None or completion_tokens is not None:
        yield LLMStreamEvent(
            type="usage", prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        )
    yield LLMStreamEvent(type="done", finish_reason=finish_reason)


def get_stream_parser(provider: str):
    """Return the line parser for ``provider``.

    Args:
        provider: Provider value from :class:`app.services.llm_service.LLMProvider`.

    Returns:
        A callable taking decoded lines and yielding :class:`LLMStreamEvent`.
    """
    if provider in ("anthropic", "claude"):
        return parse_anthropic_sse
    if provider == "ollama":
        return parse_ollama_ndjson
    return parse_openai_sse


def apply_stream_payload(payload: dict, provider: str) -> dict:
    """Flip a prepared chat payload into streaming mode for ``provider``.

    Mutates and returns ``payload`` (the caller owns a freshly built dict from
    ``LLMService._prepare_payload``), so the non-streaming payload builders stay
    the single source of truth for model/temperature/token settings.

    Args:
        payload: Payload built by ``LLMService._prepare_payload``.
        provider: Provider value driving the streaming dialect.

    Returns:
        The same dict, with streaming keys applied.
    """
    payload["stream"] = True
    if provider in USAGE_OPTION_PROVIDERS:
        payload["stream_options"] = {"include_usage": True}
    return payload
