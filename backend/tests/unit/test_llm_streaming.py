"""Unit tests for LLM streaming parsers (``app.services.llm_stream``).

The parsers are pure functions over decoded lines, so every provider dialect is
exercised against canned streams — no HTTP, no mocks. Regressions here would show
up as silently truncated chat answers or missing token counts.
"""

import threading
from unittest.mock import MagicMock

import pytest

from app.services.llm_stream import USAGE_OPTION_PROVIDERS
from app.services.llm_stream import LLMStreamEvent
from app.services.llm_stream import apply_stream_payload
from app.services.llm_stream import get_stream_parser
from app.services.llm_stream import parse_anthropic_sse
from app.services.llm_stream import parse_ollama_ndjson
from app.services.llm_stream import parse_openai_sse


def _transport(service):
    """Inject the outbound transport at the seam that now OWNS it.

    ``chat_completion_stream`` no longer posts through a bare ``service.session``:
    ``_endpoint_session`` validates and PINS the endpoint first (issue #444), so a mock
    hung on ``service.session`` is never reached and ``llm.test`` is refused as
    unresolvable — the test would then pass or fail for a reason unrelated to streaming.
    """
    from app.utils.url_validation import PinnedTarget

    session = MagicMock()
    target = PinnedTarget(
        original_url="http://llm.test/v1/chat/completions",
        url="http://203.0.113.9/v1/chat/completions",
        address="203.0.113.9",
        hostname="llm.test",
        host_header="llm.test",
        scheme="http",
        pinned=True,
    )
    service._endpoint_session = lambda url: (session, target)
    return session


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
        ("openai", parse_openai_sse),
        ("vllm", parse_openai_sse),
        ("openrouter", parse_openai_sse),
        ("custom", parse_openai_sse),
        ("anthropic", parse_anthropic_sse),
        ("claude", parse_anthropic_sse),
        ("ollama", parse_ollama_ndjson),
        # Correct but INERT: Bedrock never reaches `get_stream_parser` at all.
        # `LLMService.chat_completion_stream` branches to
        # `llm_bedrock.stream_converse` ahead of the parser lookup (see the branch
        # order test below), so this fallback is never exercised for "bedrock" in
        # production — it is pinned here only so a future provider added without a
        # parser entry doesn't silently fall through to the OpenAI dialect.
        ("bedrock", parse_openai_sse),
    ],
)
def test_get_stream_parser_maps_every_provider(provider, expected):
    assert get_stream_parser(provider) is expected


def test_get_stream_parser_parametrize_covers_every_llmprovider_member():
    """A future provider added to `LLMProvider` without a matching parametrize entry
    above must fail loudly here, not fall through untested to the OpenAI dialect.
    """
    from app.services.llm_service import LLMProvider

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


def test_bedrock_stream_never_reaches_get_stream_parser_or_session_post():
    """Bedrock branches to `llm_bedrock.stream_converse` BEFORE the URL lookup and
    parser selection (`llm_service.py`'s `chat_completion_stream`), so a
    Bedrock-configured service must never call `get_stream_parser` or POST through the
    HTTP session at all.
    """
    from unittest.mock import patch

    from app.services.llm_service import LLMConfig
    from app.services.llm_service import LLMProvider
    from app.services.llm_service import LLMService

    service = LLMService(LLMConfig(provider=LLMProvider.BEDROCK, model="test-model"))
    session = _transport(service)

    with patch(
        "app.services.llm_bedrock.stream_converse",
        return_value=iter([LLMStreamEvent(type="done", finish_reason="stop")]),
    ) as mock_stream_converse:
        with patch("app.services.llm_service.get_stream_parser") as mock_get_parser:
            events = list(service.chat_completion_stream([{"role": "user", "content": "hi"}]))

    mock_stream_converse.assert_called_once()
    mock_get_parser.assert_not_called()
    session.post.assert_not_called()
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
    from app.services.llm_service import LLMConfig
    from app.services.llm_service import LLMProvider
    from app.services.llm_service import LLMService

    return LLMService(
        LLMConfig(
            provider=LLMProvider(provider),
            model="test-model",
            api_key="sk-test",
            base_url="http://llm.test/v1",
            max_tokens=8192,
        )
    )


def _mock_response(lines: list[str], status: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.text = "error body"
    response.iter_lines.return_value = iter(lines)
    return response


def test_chat_completion_stream_relays_parsed_events():
    service = _service()
    session = _transport(service)
    session.post.return_value = _mock_response(OPENAI_STREAM)

    events = list(service.chat_completion_stream([{"role": "user", "content": "hi"}]))

    assert _texts(events) == "Hello, world"
    assert events[-1].type == "done"
    # Streaming must be requested on the wire, not just assumed.
    payload = session.post.call_args.kwargs["json"]
    assert payload["stream"] is True
    assert session.post.call_args.kwargs["stream"] is True


def test_chat_completion_stream_reports_http_error_in_band():
    """A non-200 must arrive as an error EVENT — the SSE status line is already sent."""
    service = _service()
    session = _transport(service)
    session.post.return_value = _mock_response([], status=429)

    events = list(service.chat_completion_stream([{"role": "user", "content": "hi"}]))

    assert len(events) == 1
    assert events[0].type == "error"
    assert "429" in events[0].message


def test_chat_completion_stream_reports_connection_error_in_band():
    import requests

    service = _service()
    session = _transport(service)
    session.post.side_effect = requests.exceptions.ConnectionError("refused")

    events = list(service.chat_completion_stream([{"role": "user", "content": "hi"}]))

    assert events[0].type == "error"
    assert "refused" in events[0].message


def test_chat_completion_stream_honors_cancel_event():
    """Stop-generation truncates output and reports finish_reason='cancelled'."""
    cancel = threading.Event()
    service = _service()
    session = _transport(service)

    def lines():
        yield 'data: {"choices":[{"delta":{"content":"before"}}]}'
        cancel.set()  # user hits Stop between chunks
        yield 'data: {"choices":[{"delta":{"content":"after"}}]}'

    response = MagicMock()
    response.status_code = 200
    response.iter_lines.return_value = lines()
    session.post.return_value = response

    events = list(
        service.chat_completion_stream([{"role": "user", "content": "hi"}], cancel_event=cancel)
    )

    assert _texts(events) == "before"
    assert events[-1].type == "done"
    assert events[-1].finish_reason == "cancelled"


def test_chat_completion_stream_estimates_tokens_when_provider_omits_usage():
    """Providers without usage reporting still let the caller bill/display something."""
    service = _service("custom")
    session = _transport(service)
    session.post.return_value = _mock_response(
        ['data: {"choices":[{"delta":{"content":"some answer text"}}]}', "data: [DONE]"]
    )

    events = list(service.chat_completion_stream([{"role": "user", "content": "hi"}]))

    assert _first(events, "usage") is None  # nothing reported by the provider
    assert service.estimate_tokens("some answer text") > 0
