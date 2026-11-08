"""Streaming response parsers for the LLM providers OpenTranscribe supports.

``LLMService.chat_completion`` returns a whole answer at once, which is right for
Celery tasks (summaries, speaker identification) but wrong for interactive chat —
the RAG chat surface streams tokens to the browser as they arrive.

Everything here is **pure parsing over decoded lines** (an incremental parser per
format, fed one line at a time), deliberately kept out of ``llm_service.py``: the wire formats differ per provider and are the part most worth
unit-testing against canned byte streams (no HTTP, no mocking gymnastics).

Three wire formats:
  - OpenAI-style SSE (openai / vllm / openrouter / custom OpenAI-compatible)
  - Anthropic event-typed SSE
  - Ollama newline-delimited JSON

Every parser normalizes to :class:`LLMStreamEvent` so callers stay provider-agnostic.

**Reasoning / "thinking" content** (issue: collapsible reasoning display). A growing
set of models stream their intermediate reasoning separately from the final answer:

  - OpenAI-compatible servers fronting a reasoning model (DeepSeek-R1, QwQ, ...) via
    vLLM or a similar gateway put it on ``delta.reasoning_content`` (the vLLM reasoning
    parser convention); some gateways (OpenRouter) instead use ``delta.reasoning``. Both
    are read.
  - Anthropic's extended thinking sends a ``content_block_start`` with
    ``content_block.type == "thinking"``, then ``content_block_delta`` events whose
    ``delta.type == "thinking_delta"`` carries the text on ``delta.thinking`` — a
    sibling of the ordinary ``text_delta``, not a replacement for it.
  - Ollama's native ``/api/chat`` reports reasoning on ``message.thinking`` for models
    that support it (server-side ``think`` support), separate from ``message.content``.
  - Any provider without a dedicated field may still emit reasoning INLINE as
    ``<think>...</think>`` in the ordinary content stream (the raw chat-template
    convention several open-weight reasoning models use). :class:`InlineThinkExtractor`
    is the fallback that splits that back out, incrementally, since the tag can land on
    either side of a chunk boundary.

Reasoning text is normalized to the same shape as an answer delta — a
``type="reasoning"`` :class:`LLMStreamEvent` carrying the fragment on ``text`` — so
callers that don't care about it can ignore the type and everything else about the
event contract stays unchanged.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterable
from collections.abc import AsyncIterator
from collections.abc import Iterator
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Providers whose payloads accept ``stream_options: {"include_usage": true}``.
# Deliberately excludes "custom": many OpenAI-clone servers reject unknown
# payload keys outright, and a 400 there would break chat for self-hosters.
# Those providers fall back to estimated token counts.
USAGE_OPTION_PROVIDERS = frozenset({"openai", "vllm", "openrouter"})

#: Anthropic's mid-stream ``error`` types that mean "at capacity, retry shortly".
_ANTHROPIC_TRANSIENT_ERROR_TYPES = frozenset({"overloaded_error", "rate_limit_error"})

#: HTTP statuses a provider returns for a capacity condition rather than a bad request.
TRANSIENT_HTTP_STATUSES = frozenset({429, 502, 503, 504})


@dataclass(frozen=True)
class LLMStreamEvent:
    """One normalized event from a provider's token stream.

    Attributes:
        type: ``"delta"`` (answer text chunk), ``"reasoning"`` (a chunk of the
            model's separately-streamed reasoning/thinking, shaped identically to
            ``"delta"``), ``"usage"`` (token counts), ``"done"`` (stream finished)
            or ``"error"``.
        text: Text fragment, for ``delta`` and ``reasoning`` events.
        prompt_tokens: Input tokens reported by the provider, for ``usage``.
        completion_tokens: Output tokens reported by the provider, for ``usage``.
        cache_read_tokens: Input tokens served from a prompt cache, for ``usage``.
            Billed far below the uncached rate, so metering that ignores this
            over-reports cost on any cache-enabled deployment.
        cache_write_tokens: Input tokens written to a prompt cache, for ``usage``.
            Billed *above* the uncached rate, so it must not be folded into
            ``prompt_tokens`` either.
        finish_reason: Provider stop reason, for ``done``.
        message: Human-readable detail, for ``error``. Provider prose — it is for
            server logs only and must never be relayed to the user (issue #1049).
        transient: ``error`` only — the provider reported a capacity condition
            (throttling, overload, 503) that is expected to clear on its own. The
            chat layer uses it to tell the user "try again in a moment" instead of
            a generic failure.
        code: ``error`` only — a stable code when the failure is ours rather than the
            provider's (``llm_budget_exceeded`` / ``llm_budget_unavailable``: the token
            limits refused the call and nothing was sent).
    """

    type: str
    text: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    finish_reason: str | None = None
    message: str = ""
    transient: bool = False
    code: str | None = None


# JSON-from-LLM variants differ: this one parses ONE whole payload to a dict, no fence/prose handling.
def _loads_stream_payload(raw: str) -> dict | None:
    """Parse one JSON payload, tolerating provider noise (never raises)."""
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.debug("Discarding unparseable stream payload (%d chars)", len(raw))
        return None
    return parsed if isinstance(parsed, dict) else None


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


def _partial_tag_suffix_len(buffer: str, marker: str) -> int:
    """Longest suffix of ``buffer`` that could be the start of ``marker``.

    Used to hold back a possibly-split tag at the end of a chunk rather than
    emitting it as ordinary text — e.g. a chunk ending in ``"...say <th"`` must
    not flush the ``<th`` until the next chunk proves it either completes
    ``<think>`` or is just stray text.
    """
    for length in range(min(len(marker) - 1, len(buffer)), 0, -1):
        if marker.startswith(buffer[-length:]):
            return length
    return 0


class InlineThinkExtractor:
    """Incrementally splits ``<think>...</think>`` spans out of a content stream.

    Fallback for providers/models with no dedicated reasoning-content wire field
    that instead embed reasoning directly in the ordinary content stream — the raw
    chat-template convention several open-weight reasoning models (DeepSeek-R1,
    QwQ) follow when served behind a plain chat-completions endpoint. Splitting is
    incremental because the opening/closing tag can straddle two chunks; a naive
    per-chunk ``str.find`` would leak half a tag into the rendered answer.

    A provider that never emits the tag pays only the cost of holding back a
    handful of characters whenever a chunk happens to end in a valid tag prefix
    (e.g. plain text ending in ``<``) — those are always flushed on the next
    ``feed()`` or by the final ``flush()``, so no text is ever lost, only briefly
    delayed by at most one chunk.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._in_think = False

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        """Consume one content fragment.

        Returns:
            ``[(kind, text), ...]`` in order, ``kind`` one of ``"content"`` /
            ``"reasoning"``. Usually zero or one pair; more when a chunk itself
            contains a full ``<think>``/``</think>`` transition.
        """
        self._buffer += chunk
        out: list[tuple[str, str]] = []
        while True:
            marker = _THINK_CLOSE if self._in_think else _THINK_OPEN
            idx = self._buffer.find(marker)
            if idx != -1:
                head, self._buffer = self._buffer[:idx], self._buffer[idx + len(marker) :]
                if head:
                    out.append(("reasoning" if self._in_think else "content", head))
                self._in_think = not self._in_think
                continue
            hold = _partial_tag_suffix_len(self._buffer, marker)
            if hold < len(self._buffer):
                cut = len(self._buffer) - hold
                emit, self._buffer = self._buffer[:cut], self._buffer[cut:]
                if emit:
                    out.append(("reasoning" if self._in_think else "content", emit))
            break
        return out

    def flush(self) -> list[tuple[str, str]]:
        """Emit whatever remains buffered at stream end (e.g. an unterminated tag)."""
        if not self._buffer:
            return []
        out = [("reasoning" if self._in_think else "content", self._buffer)]
        self._buffer = ""
        return out


def _think_events(pairs: list[tuple[str, str]]) -> Iterator[LLMStreamEvent]:
    """Turn :class:`InlineThinkExtractor` output into stream events."""
    for kind, text in pairs:
        yield LLMStreamEvent(type="reasoning" if kind == "reasoning" else "delta", text=text)


class StreamParser:
    """Incremental parser for one provider's streaming wire format.

    Lines are pushed in one at a time with :meth:`feed`, so the same parser serves an
    ``async for`` over ``httpx.Response.aiter_lines()`` without a thread to hold a
    blocking iterator. :func:`parse_stream` is the driver.

    Attributes:
        ended: The provider signalled the end of its stream; stop feeding and call
            :meth:`finish` for the closing events.
        failed: The provider reported an error; the ``error`` event has been returned
            and nothing follows it (no ``done``).
    """

    def __init__(self) -> None:
        self.ended = False
        self.failed = False

    def feed(self, line: str) -> list[LLMStreamEvent]:
        """Parse one decoded line; return the events it produced."""
        raise NotImplementedError

    def finish(self) -> list[LLMStreamEvent]:
        """The closing events, ending with exactly one ``done``."""
        raise NotImplementedError


async def parse_stream(
    parser: StreamParser, lines: AsyncIterable[str]
) -> AsyncIterator[LLMStreamEvent]:
    """Drive ``parser`` over ``lines``.

    Yields:
        Normalized stream events: exactly one ``done`` at the end, or a final
        ``error`` with nothing after it.
    """
    async for line in lines:
        for event in parser.feed(line):
            yield event
        if parser.failed:
            return
        if parser.ended:
            break
    for event in parser.finish():
        yield event


class OpenAISSEParser(StreamParser):
    """An OpenAI-compatible ``chat/completions`` SSE stream.

    Frames are ``data: {json}`` with a ``data: [DONE]`` sentinel. Content arrives on
    ``choices[0].delta.content``; usage arrives either on a final usage-only chunk
    (when ``stream_options.include_usage`` was accepted) or not at all.

    Reasoning arrives one of two ways, both handled: a dedicated
    ``delta.reasoning_content`` (vLLM's reasoning-parser convention) or
    ``delta.reasoning`` (OpenRouter), OR inline ``<think>...</think>`` markers inside
    ``delta.content`` itself for providers with no dedicated field — see
    :class:`InlineThinkExtractor`.
    """

    def __init__(self) -> None:
        super().__init__()
        self.finish_reason: str | None = None
        self.extractor = InlineThinkExtractor()

    def feed(self, line: str) -> list[LLMStreamEvent]:
        if not line or not line.startswith("data:"):
            return []
        payload = line[len("data:") :].strip()
        if not payload:
            return []
        if payload == "[DONE]":
            self.ended = True
            return []

        chunk = _loads_stream_payload(payload)
        if chunk is None:
            return []

        # Some gateways surface mid-stream failures as an error object.
        if isinstance(chunk.get("error"), dict):
            self.failed = True
            return [
                LLMStreamEvent(
                    type="error", message=str(chunk["error"].get("message", "provider error"))
                )
            ]

        events: list[LLMStreamEvent] = []
        choices = chunk.get("choices") or []
        if choices and isinstance(choices[0], dict):
            choice = choices[0]
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning:
                events.append(LLMStreamEvent(type="reasoning", text=str(reasoning)))
            content = delta.get("content")
            if content:
                events.extend(_think_events(self.extractor.feed(str(content))))
            if choice.get("finish_reason"):
                self.finish_reason = str(choice["finish_reason"])

        usage = chunk.get("usage")
        if isinstance(usage, dict):
            events.append(
                LLMStreamEvent(
                    type="usage",
                    prompt_tokens=_int_or_none(usage.get("prompt_tokens")),
                    completion_tokens=_int_or_none(usage.get("completion_tokens")),
                )
            )
        return events

    def finish(self) -> list[LLMStreamEvent]:
        return [
            *_think_events(self.extractor.flush()),
            LLMStreamEvent(type="done", finish_reason=self.finish_reason),
        ]


class AnthropicSSEParser(StreamParser):
    """An Anthropic Messages API SSE stream.

    Anthropic sends ``event:``/``data:`` pairs. The payload's own ``type`` field is
    authoritative (the ``event:`` line merely repeats it), so this parser reads the
    JSON and ignores the event name. Prompt tokens land on ``message_start``,
    completion tokens on ``message_delta``.

    Extended thinking streams as its own content block: a ``content_block_delta``
    whose ``delta.type == "thinking_delta"`` carries the reasoning text on
    ``delta.thinking``, a sibling of the ordinary ``text_delta``/``delta.text`` used
    for the final answer — never a replacement for it. A ``redacted_thinking`` block
    (encrypted reasoning Anthropic chose not to show) carries no readable text and is
    silently skipped, same as any other content-block type this parser doesn't render.
    """

    def __init__(self) -> None:
        super().__init__()
        self.prompt_tokens: int | None = None
        self.completion_tokens: int | None = None
        self.finish_reason: str | None = None

    def feed(self, line: str) -> list[LLMStreamEvent]:
        if not line or not line.startswith("data:"):
            return []  # "event:" lines and blank separators carry no extra information
        payload = line[len("data:") :].strip()
        if not payload:
            return []

        chunk = _loads_stream_payload(payload)
        if chunk is None:
            return []

        chunk_type = chunk.get("type")
        if chunk_type == "error":
            detail = chunk.get("error") or {}
            self.failed = True
            return [
                LLMStreamEvent(
                    type="error",
                    message=str(detail.get("message", "provider error")),
                    transient=detail.get("type") in _ANTHROPIC_TRANSIENT_ERROR_TYPES,
                )
            ]
        if chunk_type == "message_start":
            usage = (chunk.get("message") or {}).get("usage") or {}
            self.prompt_tokens = _int_or_none(usage.get("input_tokens"))
            self.completion_tokens = _int_or_none(usage.get("output_tokens"))
        elif chunk_type == "content_block_delta":
            delta = chunk.get("delta") or {}
            if delta.get("type") == "thinking_delta":
                thinking = delta.get("thinking")
                if thinking:
                    return [LLMStreamEvent(type="reasoning", text=str(thinking))]
            else:
                text = delta.get("text")
                if text:
                    return [LLMStreamEvent(type="delta", text=str(text))]
        elif chunk_type == "message_delta":
            stop_reason = (chunk.get("delta") or {}).get("stop_reason")
            if stop_reason:
                self.finish_reason = str(stop_reason)
            usage = chunk.get("usage") or {}
            if usage.get("output_tokens") is not None:
                self.completion_tokens = _int_or_none(usage.get("output_tokens"))
        elif chunk_type == "message_stop":
            self.ended = True
        # "ping" and content_block_start/stop carry nothing we need.
        return []

    def finish(self) -> list[LLMStreamEvent]:
        events: list[LLMStreamEvent] = []
        if self.prompt_tokens is not None or self.completion_tokens is not None:
            events.append(
                LLMStreamEvent(
                    type="usage",
                    prompt_tokens=self.prompt_tokens,
                    completion_tokens=self.completion_tokens,
                )
            )
        events.append(LLMStreamEvent(type="done", finish_reason=self.finish_reason))
        return events


class OllamaNDJSONParser(StreamParser):
    """An Ollama ``/api/chat`` newline-delimited JSON stream.

    One JSON object per line; content on ``message.content``. The terminal line
    (``done: true``) carries ``prompt_eval_count`` / ``eval_count``, which Ollama
    reports exactly — no estimation needed.

    Reasoning-capable models report it on ``message.thinking`` when the server
    supports Ollama's ``think`` option — a field that, per the module docstring, may
    or may not be present depending on model/server version. Whether or not it is,
    ``message.content`` also runs through :class:`InlineThinkExtractor` as a fallback
    for models/servers that embed ``<think>`` tags in plain content instead.
    """

    def __init__(self) -> None:
        super().__init__()
        self.prompt_tokens: int | None = None
        self.completion_tokens: int | None = None
        self.finish_reason: str | None = None
        self.extractor = InlineThinkExtractor()

    def feed(self, line: str) -> list[LLMStreamEvent]:
        if not line or not line.strip():
            return []
        chunk = _loads_stream_payload(line.strip())
        if chunk is None:
            return []

        if chunk.get("error"):
            self.failed = True
            return [LLMStreamEvent(type="error", message=str(chunk["error"]))]

        events: list[LLMStreamEvent] = []
        message = chunk.get("message") or {}
        thinking = message.get("thinking")
        if thinking:
            events.append(LLMStreamEvent(type="reasoning", text=str(thinking)))
        content = message.get("content")
        if content:
            events.extend(_think_events(self.extractor.feed(str(content))))

        if chunk.get("done"):
            self.prompt_tokens = _int_or_none(chunk.get("prompt_eval_count"))
            self.completion_tokens = _int_or_none(chunk.get("eval_count"))
            self.finish_reason = str(chunk.get("done_reason") or "stop")
            self.ended = True
        return events

    def finish(self) -> list[LLMStreamEvent]:
        events = list(_think_events(self.extractor.flush()))
        if self.prompt_tokens is not None or self.completion_tokens is not None:
            events.append(
                LLMStreamEvent(
                    type="usage",
                    prompt_tokens=self.prompt_tokens,
                    completion_tokens=self.completion_tokens,
                )
            )
        events.append(LLMStreamEvent(type="done", finish_reason=self.finish_reason))
        return events


def get_stream_parser(provider: str) -> type[StreamParser]:
    """Return the parser class for ``provider``.

    Args:
        provider: Provider value from :class:`app.services.llm_types.LLMProvider`.

    Returns:
        A :class:`StreamParser` subclass; build one per stream.
    """
    if provider in ("anthropic", "claude"):
        return AnthropicSSEParser
    if provider == "ollama":
        return OllamaNDJSONParser
    return OpenAISSEParser


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
