"""Unit tests for reasoning/"thinking" extraction in ``app.services.llm_stream``.

Covers the collapsible reasoning display feature: a dedicated wire field per
provider (vLLM's ``reasoning_content``, OpenRouter's ``reasoning``, Anthropic's
``thinking_delta``, Ollama's ``message.thinking``) plus the inline ``<think>``
tag fallback shared by all three HTTP-based parsers via
:class:`InlineThinkExtractor`. Same style as ``test_llm_streaming.py``: canned
byte streams, no HTTP, no mocks.
"""

from app.services.llm_stream import InlineThinkExtractor
from app.services.llm_stream import LLMStreamEvent
from app.services.llm_stream import parse_anthropic_sse
from app.services.llm_stream import parse_ollama_ndjson
from app.services.llm_stream import parse_openai_sse


def _texts(events: list[LLMStreamEvent], type_: str) -> str:
    return "".join(e.text for e in events if e.type == type_)


# ---------------------------------------------------------------------------
# InlineThinkExtractor — the <think> tag fallback, in isolation
# ---------------------------------------------------------------------------


def test_extractor_passes_through_content_with_no_tags():
    extractor = InlineThinkExtractor()
    out = extractor.feed("just ordinary text")
    assert out == [("content", "just ordinary text")]
    assert extractor.flush() == []


def test_extractor_splits_a_tag_contained_in_one_chunk():
    extractor = InlineThinkExtractor()
    out = extractor.feed("before <think>reasoning</think> after")
    assert out == [("content", "before "), ("reasoning", "reasoning"), ("content", " after")]


def test_extractor_handles_a_tag_split_across_chunks():
    """The opening tag itself straddles two feed() calls."""
    extractor = InlineThinkExtractor()
    first = extractor.feed("before <th")
    second = extractor.feed("ink>reasoning</think> after")

    assert first == [("content", "before ")]
    assert second == [("reasoning", "reasoning"), ("content", " after")]


def test_extractor_handles_the_closing_tag_split_across_chunks():
    extractor = InlineThinkExtractor()
    first = extractor.feed("<think>partial reason")
    second = extractor.feed("ing</th")
    third = extractor.feed("ink>done")

    assert first == [("reasoning", "partial reason")]
    assert second == [("reasoning", "ing")]
    assert third == [("content", "done")]


def test_extractor_holds_back_a_lone_angle_bracket_until_proven_not_a_tag():
    """Plain text ending in '<' must not be mistaken for a tag start and dropped."""
    extractor = InlineThinkExtractor()
    out = extractor.feed("score <")
    # '<' isn't followed by anything yet, so it's held back...
    assert out == [("content", "score ")]
    # ...and released, intact, once the next chunk proves it wasn't a tag.
    out2 = extractor.feed(" 5")
    assert out2 == [("content", "< 5")]


def test_extractor_flush_emits_a_held_back_unterminated_tag_prefix():
    extractor = InlineThinkExtractor()
    out = extractor.feed("start <thi")
    assert out == [("content", "start ")]
    assert extractor.flush() == [("content", "<thi")]
    # Nothing left to flush a second time.
    assert extractor.flush() == []


def test_extractor_never_loses_bytes_across_many_small_chunks():
    """Feeding one character at a time must reconstruct the exact original text."""
    source = "hi <think>secret plan</think> the answer is 42"
    extractor = InlineThinkExtractor()
    collected: list[tuple[str, str]] = []
    for ch in source:
        collected.extend(extractor.feed(ch))
    collected.extend(extractor.flush())

    content = "".join(text for kind, text in collected if kind == "content")
    reasoning = "".join(text for kind, text in collected if kind == "reasoning")
    assert content == "hi  the answer is 42"
    assert reasoning == "secret plan"


# ---------------------------------------------------------------------------
# OpenAI-compatible SSE — dedicated reasoning fields
# ---------------------------------------------------------------------------


def test_openai_stream_reads_reasoning_content_field():
    """vLLM's reasoning-parser convention: delta.reasoning_content."""
    events = list(
        parse_openai_sse(
            [
                'data: {"choices":[{"delta":{"reasoning_content":"Let me "}}]}',
                'data: {"choices":[{"delta":{"reasoning_content":"think..."}}]}',
                'data: {"choices":[{"delta":{"content":"The answer is 4."}}]}',
                "data: [DONE]",
            ]
        )
    )
    assert _texts(events, "reasoning") == "Let me think..."
    assert _texts(events, "delta") == "The answer is 4."
    assert events[-1].type == "done"


def test_openai_stream_reads_reasoning_field_openrouter_style():
    events = list(
        parse_openai_sse(
            [
                'data: {"choices":[{"delta":{"reasoning":"pondering"}}]}',
                'data: {"choices":[{"delta":{"content":"answer"}}]}',
                "data: [DONE]",
            ]
        )
    )
    assert _texts(events, "reasoning") == "pondering"
    assert _texts(events, "delta") == "answer"


def test_openai_stream_falls_back_to_inline_think_tags():
    """No dedicated field at all — reasoning is embedded in `content` as <think>."""
    events = list(
        parse_openai_sse(
            [
                'data: {"choices":[{"delta":{"content":"<think>"}}]}',
                'data: {"choices":[{"delta":{"content":"working it out"}}]}',
                'data: {"choices":[{"delta":{"content":"</think>final answer"}}]}',
                "data: [DONE]",
            ]
        )
    )
    assert _texts(events, "reasoning") == "working it out"
    assert _texts(events, "delta") == "final answer"


def test_openai_stream_flushes_an_unterminated_think_tag_before_done():
    """A stream that ends mid-tag must not silently drop the buffered text."""
    events = list(
        parse_openai_sse(
            [
                'data: {"choices":[{"delta":{"content":"<think>never closes"}}]}',
                "data: [DONE]",
            ]
        )
    )
    assert _texts(events, "reasoning") == "never closes"
    assert events[-1].type == "done"


def test_openai_stream_reasoning_does_not_affect_normal_responses():
    """Backward compatibility: a plain response with no reasoning is unaffected."""
    events = list(
        parse_openai_sse(
            [
                'data: {"choices":[{"delta":{"content":"Hello"}}]}',
                'data: {"choices":[{"delta":{"content":", world"}}]}',
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
                "data: [DONE]",
            ]
        )
    )
    assert _texts(events, "delta") == "Hello, world"
    assert _texts(events, "reasoning") == ""
    assert events[-1].type == "done"
    assert events[-1].finish_reason == "stop"


# ---------------------------------------------------------------------------
# Anthropic SSE — extended thinking
# ---------------------------------------------------------------------------

ANTHROPIC_THINKING_STREAM = [
    "event: message_start",
    'data: {"type":"message_start","message":{"usage":{"input_tokens":10,"output_tokens":1}}}',
    "event: content_block_start",
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking"}}',
    "event: content_block_delta",
    'data: {"type":"content_block_delta",'
    '"delta":{"type":"thinking_delta","thinking":"Working through this. "}}',
    "event: content_block_delta",
    'data: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"Got it."}}',
    "event: content_block_stop",
    'data: {"type":"content_block_stop","index":0}',
    "event: content_block_start",
    'data: {"type":"content_block_start","index":1,"content_block":{"type":"text"}}',
    "event: content_block_delta",
    'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"The answer."}}',
    "event: message_delta",
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":20}}',
    "event: message_stop",
    'data: {"type":"message_stop"}',
]


def test_anthropic_stream_separates_thinking_from_the_answer():
    events = list(parse_anthropic_sse(ANTHROPIC_THINKING_STREAM))

    assert _texts(events, "reasoning") == "Working through this. Got it."
    assert _texts(events, "delta") == "The answer."
    assert events[-1].type == "done"
    assert events[-1].finish_reason == "end_turn"


def test_anthropic_stream_ignores_redacted_thinking_block_without_crashing():
    """A redacted_thinking content_block_start carries no readable text."""
    events = list(
        parse_anthropic_sse(
            [
                'data: {"type":"content_block_start","index":0,'
                '"content_block":{"type":"redacted_thinking","data":"opaque"}}',
                'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"ok"}}',
            ]
        )
    )
    assert _texts(events, "delta") == "ok"
    assert _texts(events, "reasoning") == ""


def test_anthropic_stream_without_thinking_is_unaffected():
    """Backward compatibility against the original (pre-reasoning) fixture shape."""
    events = list(
        parse_anthropic_sse(
            [
                'data: {"type":"content_block_delta","delta":{"text":"Hi"}}',
                'data: {"type":"content_block_delta","delta":{"text":" there"}}',
            ]
        )
    )
    assert _texts(events, "delta") == "Hi there"
    assert _texts(events, "reasoning") == ""


# ---------------------------------------------------------------------------
# Ollama NDJSON — message.thinking + inline fallback
# ---------------------------------------------------------------------------


def test_ollama_stream_reads_message_thinking_field():
    events = list(
        parse_ollama_ndjson(
            [
                '{"message":{"content":"","thinking":"pondering "},"done":false}',
                '{"message":{"content":"","thinking":"the request"},"done":false}',
                '{"message":{"content":"42","thinking":""},"done":false}',
                '{"done":true,"done_reason":"stop","prompt_eval_count":5,"eval_count":9}',
            ]
        )
    )
    assert _texts(events, "reasoning") == "pondering the request"
    assert _texts(events, "delta") == "42"
    assert events[-1].type == "done"


def test_ollama_stream_falls_back_to_inline_think_tags_when_no_thinking_field():
    events = list(
        parse_ollama_ndjson(
            [
                '{"message":{"content":"<think>reasoning here</think>answer"},"done":false}',
                '{"done":true}',
            ]
        )
    )
    assert _texts(events, "reasoning") == "reasoning here"
    assert _texts(events, "delta") == "answer"


def test_ollama_stream_without_reasoning_is_unaffected():
    events = list(
        parse_ollama_ndjson(
            [
                '{"message":{"content":"Sum"},"done":false}',
                '{"message":{"content":"mary"},"done":true}',
            ]
        )
    )
    assert _texts(events, "delta") == "Summary"
    assert _texts(events, "reasoning") == ""
    assert events[-1].type == "done"
