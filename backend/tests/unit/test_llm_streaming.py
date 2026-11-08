"""Unit tests for LLM streaming parsers (``app.services.llm_stream``).

The parsers are pure functions over decoded lines, so every provider dialect is
exercised against canned streams — no HTTP, no mocks. Regressions here would show
up as silently truncated chat answers or missing token counts.
"""

import json
import threading
from collections.abc import AsyncIterator
from collections.abc import Iterable

import httpx
import pytest

from app.services import llm_budget
from app.services.llm_stream import USAGE_OPTION_PROVIDERS
from app.services.llm_stream import AnthropicSSEParser
from app.services.llm_stream import LLMStreamEvent
from app.services.llm_stream import OllamaNDJSONParser
from app.services.llm_stream import OpenAISSEParser
from app.services.llm_stream import apply_stream_payload
from app.services.llm_stream import get_stream_parser
from tests.llm_stream_helpers import drain
from tests.llm_stream_helpers import parse_anthropic_sse
from tests.llm_stream_helpers import parse_ollama_ndjson
from tests.llm_stream_helpers import parse_openai_sse


class _Transport:
    """What the stream sent, and the canned answer it gets back."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.lines: Iterable[str] = []
        self.error: Exception | None = None
        #: Answers for the first attempts, in order: an exception to raise, or a
        #: ``(status, headers)`` pair. Once empty, ``status``/``lines`` answer.
        self.first_answers: list[Exception | tuple[int, dict[str, str]]] = []
        #: Raised after the first body line, i.e. once streaming has started.
        self.break_mid_stream: Exception | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.first_answers:
            answer = self.first_answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            status, headers = answer
            return httpx.Response(status, headers=headers, text="busy")
        if self.error is not None:
            raise self.error
        if self.status != 200:
            return httpx.Response(self.status, text="error body")
        lines = self.lines
        broken = self.break_mid_stream

        async def body() -> AsyncIterator[bytes]:
            # Lazy, one line per chunk, so a test can act between chunks.
            for index, line in enumerate(lines):
                yield f"{line}\n".encode()
                if broken is not None and index == 0:
                    raise broken

        return httpx.Response(200, content=body())

    @property
    def payload(self) -> dict:
        body: dict = json.loads(self.requests[-1].content)
        return body


@pytest.fixture(autouse=True)
def sleeps(monkeypatch) -> list[float]:
    """The retry waits, recorded instead of slept."""
    from app.services import llm_http_stream

    waited: list[float] = []

    async def _record(seconds: float) -> None:
        waited.append(seconds)

    monkeypatch.setattr(llm_http_stream, "_sleep", _record)
    return waited


def _transport(service) -> _Transport:
    """Inject the outbound transport at the seams that now OWN it.

    ``chat_completion_stream`` validates and PINS the endpoint first (issue #444) via
    ``_endpoint_session``, so ``llm.test`` would otherwise be refused as unresolvable and
    the test would pass or fail for a reason unrelated to streaming. The stream itself goes
    through ``_stream_client``, an ``httpx.AsyncClient``; ``httpx.MockTransport`` answers it.
    """
    from app.utils.url_validation import PinnedTarget

    transport = _Transport()
    target = PinnedTarget(
        original_url="http://llm.test/v1/chat/completions",
        url="http://203.0.113.9/v1/chat/completions",
        address="203.0.113.9",
        hostname="llm.test",
        host_header="llm.test",
        scheme="http",
        pinned=True,
    )
    service._endpoint_session = lambda url: (None, target)
    service._stream_client = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(transport.handle)
    )
    # The token limits are database work; these tests are about the HTTP layer.
    service._reserve_budget = lambda messages, kwargs: llm_budget.NO_RESERVATION
    return transport


def _texts(events: list[LLMStreamEvent]) -> str:
    return "".join(e.text for e in events if e.type == "delta")


def _first(events: list[LLMStreamEvent], type_: str) -> LLMStreamEvent | None:
    return next((e for e in events if e.type == type_), None)


# ---------------------------------------------------------------------------
# OpenAI-compatible SSE
# ---------------------------------------------------------------------------

OPENAI_STREAM = [
    'data: {"choices":[{"delta":{"role":"assistant"},"finish_reason":null}]}',
    "",
    'data: {"choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}',
    'data: {"choices":[{"delta":{"content":", world"},"finish_reason":null}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
    'data: {"choices":[],"usage":{"prompt_tokens":42,"completion_tokens":7,"total_tokens":49}}',
    "data: [DONE]",
]


def test_openai_stream_yields_deltas_usage_and_done():
    events = list(parse_openai_sse(OPENAI_STREAM))

    assert _texts(events) == "Hello, world"
    usage = _first(events, "usage")
    assert usage is not None
    assert usage.prompt_tokens == 42
    assert usage.completion_tokens == 7
    assert events[-1].type == "done"
    assert events[-1].finish_reason == "stop"


def test_openai_stream_stops_at_done_sentinel():
    """Anything after [DONE] is provider noise and must not become content."""
    events = list(
        parse_openai_sse(
            [
                'data: {"choices":[{"delta":{"content":"kept"}}]}',
                "data: [DONE]",
                'data: {"choices":[{"delta":{"content":"dropped"}}]}',
            ]
        )
    )
    assert _texts(events) == "kept"


def test_openai_stream_tolerates_malformed_json_and_comments():
    events = list(
        parse_openai_sse(
            [
                ": keepalive",
                "data: {not json at all",
                'data: {"choices":[{"delta":{"content":"ok"}}]}',
                "",
            ]
        )
    )
    assert _texts(events) == "ok"
    assert events[-1].type == "done"


def test_openai_stream_surfaces_provider_error_object():
    events = list(parse_openai_sse(['data: {"error":{"message":"context length exceeded"}}']))
    assert events[-1].type == "error"
    assert "context length" in events[-1].message


def test_openai_stream_always_terminates_with_done():
    """A truncated stream (no [DONE], no finish_reason) still terminates cleanly."""
    events = list(parse_openai_sse(['data: {"choices":[{"delta":{"content":"partial"}}]}']))
    assert _texts(events) == "partial"
    assert events[-1].type == "done"
    assert events[-1].finish_reason is None


# ---------------------------------------------------------------------------
# Anthropic SSE
# ---------------------------------------------------------------------------

ANTHROPIC_STREAM = [
    "event: message_start",
    'data: {"type":"message_start","message":{"usage":{"input_tokens":25,"output_tokens":1}}}',
    "",
    "event: content_block_start",
    'data: {"type":"content_block_start","index":0}',
    "event: ping",
    'data: {"type":"ping"}',
    "event: content_block_delta",
    'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Hi"}}',
    "event: content_block_delta",
    'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":" there"}}',
    "event: message_delta",
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":15}}',
    "event: message_stop",
    'data: {"type":"message_stop"}',
]


def test_anthropic_stream_captures_text_and_both_token_counts():
    events = list(parse_anthropic_sse(ANTHROPIC_STREAM))

    assert _texts(events) == "Hi there"
    usage = _first(events, "usage")
    assert usage is not None
    assert usage.prompt_tokens == 25
    assert usage.completion_tokens == 15  # message_delta overrides message_start
    assert events[-1].type == "done"
    assert events[-1].finish_reason == "end_turn"


def test_anthropic_stream_ignores_ping_and_unknown_events():
    events = list(
        parse_anthropic_sse(
            [
                'data: {"type":"ping"}',
                'data: {"type":"some_future_event","delta":{"text":"ignored"}}',
                'data: {"type":"content_block_delta","delta":{"text":"real"}}',
            ]
        )
    )
    assert _texts(events) == "real"


def test_anthropic_stream_surfaces_error_event():
    events = list(
        parse_anthropic_sse(
            [
                "event: error",
                'data: {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}',
            ]
        )
    )
    assert events[-1].type == "error"
    assert events[-1].message == "Overloaded"
    # Capacity, not a bad request: chat says "try again in a moment" (issue #1049).
    assert events[-1].transient is True


def test_anthropic_invalid_request_error_is_not_transient():
    events = list(
        parse_anthropic_sse(
            ['data: {"type":"error","error":{"type":"invalid_request_error","message":"bad"}}']
        )
    )
    assert events[-1].type == "error"
    assert events[-1].transient is False


# ---------------------------------------------------------------------------
# Ollama NDJSON
# ---------------------------------------------------------------------------

OLLAMA_STREAM = [
    '{"model":"llama3","message":{"role":"assistant","content":"Sum"},"done":false}',
    '{"model":"llama3","message":{"role":"assistant","content":"mary"},"done":false}',
    '{"model":"llama3","message":{"role":"assistant","content":""},"done":true,'
    '"done_reason":"stop","prompt_eval_count":26,"eval_count":298}',
]


def test_ollama_stream_reports_exact_token_counts():
    events = list(parse_ollama_ndjson(OLLAMA_STREAM))

    assert _texts(events) == "Summary"
    usage = _first(events, "usage")
    assert usage is not None
    assert usage.prompt_tokens == 26
    assert usage.completion_tokens == 298
    assert events[-1].type == "done"
    assert events[-1].finish_reason == "stop"


def test_ollama_stream_surfaces_error_line():
    events = list(parse_ollama_ndjson(['{"error":"model not found"}']))
    assert events[-1].type == "error"
    assert events[-1].message == "model not found"


def test_ollama_stream_skips_blank_lines():
    events = list(
        parse_ollama_ndjson(
            ["", '{"message":{"content":"x"},"done":false}', "   ", '{"done":true}']
        )
    )
    assert _texts(events) == "x"
    assert events[-1].type == "done"


# ---------------------------------------------------------------------------
# Parser selection + payload shaping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("openai", OpenAISSEParser),
        ("vllm", OpenAISSEParser),
        ("openrouter", OpenAISSEParser),
        ("custom", OpenAISSEParser),
        ("anthropic", AnthropicSSEParser),
        ("claude", AnthropicSSEParser),
        ("ollama", OllamaNDJSONParser),
        # Correct but INERT: Bedrock never reaches `get_stream_parser` at all.
        # `LLMService.chat_completion_stream` branches to
        # `llm_bedrock.stream_converse` ahead of the parser lookup (see the branch
        # order test below), so this fallback is never exercised for "bedrock" in
        # production — it is pinned here only so a future provider added without a
        # parser entry doesn't silently fall through to the OpenAI dialect.
        ("bedrock", OpenAISSEParser),
    ],
)
def test_get_stream_parser_maps_every_provider(provider, expected):
    assert get_stream_parser(provider) is expected


def test_get_stream_parser_parametrize_covers_every_llmprovider_member():
    """A future provider added to `LLMProvider` without a matching parametrize entry
    above must fail loudly here, not fall through untested to the OpenAI dialect.
    """
    from app.services.llm_types import LLMProvider

    covered = {
        "openai",
        "vllm",
        "openrouter",
        "custom",
        "anthropic",
        "claude",
        "ollama",
        "bedrock",
    }
    assert {p.value for p in LLMProvider} == covered


def test_bedrock_stream_never_reaches_get_stream_parser_or_the_http_client():
    """Bedrock branches to `llm_bedrock.stream_converse` BEFORE the URL lookup and
    parser selection (`llm_service.py`'s `_chat_completion_stream_untracked`), so a
    Bedrock-configured service must never call `get_stream_parser` or send through the
    HTTP client at all.
    """
    from unittest.mock import patch

    from app.services.llm_service import LLMService
    from app.services.llm_types import LLMConfig
    from app.services.llm_types import LLMProvider

    service = LLMService(LLMConfig(provider=LLMProvider.BEDROCK, model="test-model"))
    transport = _transport(service)

    with patch(
        "app.services.llm_bedrock.stream_converse",
        return_value=iter([LLMStreamEvent(type="done", finish_reason="stop")]),
    ) as mock_stream_converse:
        with patch("app.services.llm_http_stream.get_stream_parser") as mock_get_parser:
            events = drain(service.chat_completion_stream([{"role": "user", "content": "hi"}]))

    mock_stream_converse.assert_called_once()
    mock_get_parser.assert_not_called()
    assert transport.requests == []
    assert events[-1].type == "done"


def test_apply_stream_payload_requests_usage_only_where_supported():
    for provider in USAGE_OPTION_PROVIDERS:
        payload = apply_stream_payload({"model": "m"}, provider)
        assert payload["stream"] is True
        assert payload["stream_options"] == {"include_usage": True}

    # "custom" OpenAI-clones often reject unknown keys — never send stream_options.
    custom = apply_stream_payload({"model": "m"}, "custom")
    assert custom["stream"] is True
    assert "stream_options" not in custom


# ---------------------------------------------------------------------------
# LLMService.chat_completion_stream (HTTP layer around the parsers)
# ---------------------------------------------------------------------------


def _service(provider: str = "openai"):
    from app.services.llm_service import LLMService
    from app.services.llm_types import LLMConfig
    from app.services.llm_types import LLMProvider

    return LLMService(
        LLMConfig(
            provider=LLMProvider(provider),
            model="test-model",
            api_key="sk-test",
            base_url="http://llm.test/v1",
            max_tokens=8192,
        )
    )


def _stream(service, cancel_event=None) -> list[LLMStreamEvent]:
    return drain(
        service.chat_completion_stream(
            [{"role": "user", "content": "hi"}], cancel_event=cancel_event
        )
    )


def test_chat_completion_stream_relays_parsed_events():
    service = _service()
    transport = _transport(service)
    transport.lines = OPENAI_STREAM

    events = _stream(service)

    assert _texts(events) == "Hello, world"
    assert events[-1].type == "done"
    # Streaming must be requested on the wire, not just assumed.
    assert transport.payload["stream"] is True
    # Sent to the pinned address, under the name it was asked for.
    assert transport.requests[0].url.host == "203.0.113.9"
    assert transport.requests[0].headers["host"] == "llm.test"


def test_chat_completion_stream_reports_http_error_in_band():
    """A non-200 must arrive as an error EVENT — the SSE status line is already sent."""
    service = _service()
    transport = _transport(service)
    transport.status = 429

    events = _stream(service)

    assert len(events) == 1
    assert events[0].type == "error"
    assert "429" in events[0].message
    assert events[0].transient is True


@pytest.mark.parametrize(
    ("status", "transient"), [(503, True), (502, True), (400, False), (401, False)]
)
def test_chat_completion_stream_flags_capacity_statuses_as_transient(status, transient):
    service = _service()
    transport = _transport(service)
    transport.status = status

    events = _stream(service)

    assert [e.type for e in events] == ["error"]
    assert events[0].transient is transient


def test_chat_completion_stream_reports_connection_error_in_band():
    service = _service()
    transport = _transport(service)
    transport.error = httpx.ConnectError("refused")

    events = _stream(service)

    assert events[0].type == "error"
    assert "ConnectError" in events[0].message
    assert "refused" not in events[0].message  # exception text stays in the server log


def test_chat_completion_stream_honors_cancel_event():
    """Stop-generation truncates output and reports finish_reason='cancelled'."""
    cancel = threading.Event()
    service = _service()
    transport = _transport(service)

    def lines():
        yield 'data: {"choices":[{"delta":{"content":"before"}}]}'
        cancel.set()  # user hits Stop between chunks
        yield 'data: {"choices":[{"delta":{"content":"after"}}]}'

    transport.lines = lines()

    events = _stream(service, cancel_event=cancel)

    assert _texts(events) == "before"
    assert events[-1].type == "done"
    assert events[-1].finish_reason == "cancelled"


def test_chat_completion_stream_estimates_tokens_when_provider_omits_usage():
    """Providers without usage reporting still let the caller bill/display something."""
    service = _service("custom")
    transport = _transport(service)
    transport.lines = [
        'data: {"choices":[{"delta":{"content":"some answer text"}}]}',
        "data: [DONE]",
    ]

    events = _stream(service)

    assert _first(events, "usage") is None  # nothing reported by the provider
    assert service.estimate_tokens("some answer text") > 0


# ---------------------------------------------------------------------------
# Retries: before the first byte only (the policy urllib3 gave the old stream)
# ---------------------------------------------------------------------------


def test_a_capacity_status_is_retried_then_streams(sleeps):
    service = _service()
    transport = _transport(service)
    transport.first_answers = [(503, {})]
    transport.lines = OPENAI_STREAM

    events = _stream(service)

    assert _texts(events) == "Hello, world"
    assert len(transport.requests) == 2
    assert len(sleeps) == 1 and 0.0 <= sleeps[0] <= 0.5


def test_a_refused_connection_is_retried_then_streams(sleeps):
    service = _service()
    transport = _transport(service)
    transport.first_answers = [httpx.ConnectError("refused")]
    transport.lines = OPENAI_STREAM

    events = _stream(service)

    assert _texts(events) == "Hello, world"
    assert len(transport.requests) == 2


def test_it_gives_up_after_three_attempts(sleeps):
    service = _service()
    transport = _transport(service)
    transport.first_answers = [(503, {}), (503, {}), (503, {})]

    events = _stream(service)

    assert [e.type for e in events] == ["error"]
    assert "503" in events[0].message and events[0].transient is True
    assert len(transport.requests) == 3
    assert len(sleeps) == 2
    # Exponential backoff with full jitter: the second wait's ceiling is twice the first's.
    assert 0.0 <= sleeps[0] <= 0.5 and 0.0 <= sleeps[1] <= 1.0


def test_retry_after_is_honoured_and_capped(sleeps):
    service = _service()
    transport = _transport(service)
    transport.first_answers = [(429, {"Retry-After": "4"}), (429, {"Retry-After": "60"})]
    transport.lines = OPENAI_STREAM

    events = _stream(service)

    assert _texts(events) == "Hello, world"
    assert sleeps == [4.0, 10.0]


def test_a_client_error_is_not_retried(sleeps):
    service = _service()
    transport = _transport(service)
    transport.first_answers = [(400, {})]

    events = _stream(service)

    assert [e.type for e in events] == ["error"]
    assert len(transport.requests) == 1
    assert sleeps == []


def test_a_stream_that_breaks_after_it_started_is_never_retried(sleeps):
    service = _service()
    transport = _transport(service)
    transport.lines = OPENAI_STREAM[2:]
    transport.break_mid_stream = httpx.ReadError("connection reset")

    events = _stream(service)

    assert _texts(events) == "Hello"
    assert events[-1].type == "error" and "interrupted" in events[-1].message
    assert len(transport.requests) == 1
    assert sleeps == []
