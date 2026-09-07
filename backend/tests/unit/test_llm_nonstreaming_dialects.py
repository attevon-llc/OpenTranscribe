"""Non-streaming payload preparation and response extraction, per provider dialect.

``LLMService.chat_completion`` and ``chat_completion_raw`` funnel every provider through
``_prepare_payload`` (build the request) and ``_extract_response_content`` (parse the
response). Each dialect has its own quirks — Claude lifts system messages to a top-level
key and reads only ``content[0]``, Ollama uses its native ``/api/chat`` shape and defaults
``done_reason`` to ``"stop"``, OpenAI-compatible servers can return ``content: null`` on a
reasoning model that exhausts its budget. None of this was under test before this file:
a change to any of these private methods could silently break Claude/Ollama non-streaming
calls with nothing to catch it.

Pure functions over dicts — no HTTP, no ``_transport`` mock. Two behaviors are pinned
AS-IS, deliberately not fixed here (flagged in the PR body):

* Claude's ``_extract_claude_response`` reads only ``content[0]`` — a multi-block response
  (e.g. an extended-thinking block first, then text) returns the first block's text, which
  could be empty for a thinking block.
* Claude's ``_prepare_claude_payload`` keeps only the LAST system message when several are
  present (the loop overwrites), unlike Bedrock's ``split_system_messages`` which
  accumulates every system block.
"""

from __future__ import annotations

import pytest

from app.services.llm_service import LLMConfig
from app.services.llm_service import LLMProvider
from app.services.llm_service import LLMService


def _service(provider: LLMProvider, **overrides) -> LLMService:
    config = LLMConfig(
        provider=provider,
        model=overrides.pop("model", "test-model"),
        base_url=overrides.pop("base_url", "http://llm.test/v1"),
        api_key=overrides.pop("api_key", "not-a-secret"),
        **overrides,
    )
    return LLMService(config)


# ---------------------------------------------------------------------------
# _prepare_claude_payload
# ---------------------------------------------------------------------------


def test_claude_payload_lifts_system_message_to_a_top_level_key():
    service = _service(LLMProvider.ANTHROPIC)
    payload = service._prepare_claude_payload(
        [
            {"role": "system", "content": "Be terse."},
            {"role": "user", "content": "Hi"},
        ]
    )
    assert payload["system"] == "Be terse."
    assert payload["messages"] == [{"role": "user", "content": "Hi"}]


def test_claude_payload_keeps_only_the_last_system_message():
    """Pinned as-is: the loop overwrites rather than accumulates (contrast Bedrock's
    `split_system_messages`, which concatenates every system block instead).
    """
    service = _service(LLMProvider.ANTHROPIC)
    payload = service._prepare_claude_payload(
        [
            {"role": "system", "content": "First."},
            {"role": "system", "content": "Second."},
            {"role": "user", "content": "Hi"},
        ]
    )
    assert payload["system"] == "Second."


def test_claude_payload_drops_roles_other_than_system_user_assistant():
    service = _service(LLMProvider.ANTHROPIC)
    payload = service._prepare_claude_payload(
        [
            {"role": "tool", "content": "some tool output"},
            {"role": "user", "content": "Hi"},
        ]
    )
    assert payload["messages"] == [{"role": "user", "content": "Hi"}]


def test_claude_payload_uses_kwargs_over_config_defaults():
    service = _service(LLMProvider.ANTHROPIC)
    payload = service._prepare_claude_payload(
        [{"role": "user", "content": "Hi"}], max_tokens=123, temperature=0.9
    )
    assert payload["max_tokens"] == 123
    assert payload["temperature"] == 0.9


def test_claude_payload_falls_back_to_config_defaults():
    service = _service(LLMProvider.ANTHROPIC)
    payload = service._prepare_claude_payload([{"role": "user", "content": "Hi"}])
    assert payload["max_tokens"] == service.config.response_tokens
    assert payload["temperature"] == service.config.temperature


def test_claude_payload_omits_system_key_entirely_when_no_system_message():
    """Not an empty string — the key must be absent."""
    service = _service(LLMProvider.ANTHROPIC)
    payload = service._prepare_claude_payload([{"role": "user", "content": "Hi"}])
    assert "system" not in payload


def test_claude_payload_prefill_json_appends_an_assistant_open_brace():
    service = _service(LLMProvider.ANTHROPIC)
    payload = service._prepare_claude_payload(
        [{"role": "user", "content": "Hi"}], prefill_json=True
    )
    assert payload["messages"][-1] == {"role": "assistant", "content": "{"}


def test_claude_payload_prefill_json_does_not_fire_when_last_message_is_assistant():
    service = _service(LLMProvider.ANTHROPIC)
    payload = service._prepare_claude_payload(
        [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello there"},
        ],
        prefill_json=True,
    )
    assert payload["messages"][-1] == {"role": "assistant", "content": "Hello there"}


def test_claude_payload_prefill_json_does_not_fire_with_no_user_messages():
    service = _service(LLMProvider.ANTHROPIC)
    payload = service._prepare_claude_payload(
        [{"role": "system", "content": "Be terse."}], prefill_json=True
    )
    assert payload["messages"] == []


# ---------------------------------------------------------------------------
# _prepare_ollama_payload
# ---------------------------------------------------------------------------


def test_ollama_payload_uses_the_native_chat_shape():
    service = _service(LLMProvider.OLLAMA, max_tokens=32000)
    messages = [
        {"role": "system", "content": "Be terse."},
        {"role": "user", "content": "Hi"},
    ]
    payload = service._prepare_ollama_payload(messages, max_tokens=500, num_ctx=16000)

    assert payload["model"] == service.config.model
    # Messages, including the system role, pass through VERBATIM — unlike Claude.
    assert payload["messages"] == messages
    assert payload["stream"] is False
    assert payload["options"]["num_predict"] == 500
    assert payload["options"]["num_ctx"] == 16000


def test_ollama_payload_num_predict_defaults_from_response_tokens():
    service = _service(LLMProvider.OLLAMA)
    payload = service._prepare_ollama_payload([{"role": "user", "content": "Hi"}])
    assert payload["options"]["num_predict"] == service.config.response_tokens


def test_ollama_payload_num_ctx_defaults_from_user_context_window():
    service = _service(LLMProvider.OLLAMA, max_tokens=9001)
    payload = service._prepare_ollama_payload([{"role": "user", "content": "Hi"}])
    assert payload["options"]["num_ctx"] == 9001


def test_ollama_payload_format_only_present_when_passed_in_kwargs():
    service = _service(LLMProvider.OLLAMA)
    without = service._prepare_ollama_payload([{"role": "user", "content": "Hi"}])
    assert "format" not in without

    with_format = service._prepare_ollama_payload(
        [{"role": "user", "content": "Hi"}], format="json"
    )
    assert with_format["format"] == "json"


def test_ollama_payload_never_gets_vllm_only_chat_template_kwargs():
    service = _service(LLMProvider.OLLAMA)
    payload = service._prepare_ollama_payload([{"role": "user", "content": "Hi"}])
    assert "chat_template_kwargs" not in payload


# ---------------------------------------------------------------------------
# _extract_claude_response
# ---------------------------------------------------------------------------


def test_extract_claude_response_reads_the_messages_api_shape():
    service = _service(LLMProvider.ANTHROPIC)
    data = {
        "content": [{"type": "text", "text": "Hello!"}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "stop_reason": "end_turn",
    }
    content, usage, finish_reason = service._extract_claude_response(data)
    assert content == "Hello!"
    assert usage == 15
    assert finish_reason == "end_turn"


def test_extract_claude_response_only_reads_the_first_content_block():
    """Pinned as-is: a `thinking` block ahead of `text` returns the (possibly empty)
    thinking block's text, not the text block. Flagged as a follow-up in the PR body.
    """
    service = _service(LLMProvider.ANTHROPIC)
    data = {
        "content": [
            {"type": "thinking", "thinking": "reasoning..."},
            {"type": "text", "text": "The actual answer"},
        ],
    }
    content, _usage, _finish_reason = service._extract_claude_response(data)
    # The first block has no "text" key at all, so `.get("text", "")` yields "".
    assert content == ""


def test_extract_claude_response_missing_usage_is_none_not_zero():
    service = _service(LLMProvider.ANTHROPIC)
    data = {"content": [{"type": "text", "text": "Hi"}]}
    _content, usage, _finish_reason = service._extract_claude_response(data)
    assert usage is None


def test_extract_claude_response_missing_content_raises():
    service = _service(LLMProvider.ANTHROPIC)
    with pytest.raises(Exception, match="No content"):
        service._extract_claude_response({})


def test_extract_claude_response_empty_content_list_raises():
    service = _service(LLMProvider.ANTHROPIC)
    with pytest.raises(Exception, match="No content"):
        service._extract_claude_response({"content": []})


def test_extract_claude_response_non_list_content_falls_back_to_str():
    service = _service(LLMProvider.ANTHROPIC)
    content, _usage, _finish_reason = service._extract_claude_response({"content": "raw string"})
    assert content == "raw string"


def test_extract_claude_response_missing_stop_reason_is_none():
    service = _service(LLMProvider.ANTHROPIC)
    _content, _usage, finish_reason = service._extract_claude_response(
        {"content": [{"type": "text", "text": "Hi"}]}
    )
    assert finish_reason is None


# ---------------------------------------------------------------------------
# _extract_ollama_response
# ---------------------------------------------------------------------------


def test_extract_ollama_response_reads_the_native_chat_shape():
    service = _service(LLMProvider.OLLAMA)
    data = {
        "message": {"role": "assistant", "content": "Hello!"},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 12,
        "eval_count": 8,
    }
    content, usage, finish_reason = service._extract_ollama_response(data)
    assert content == "Hello!"
    assert usage == 20
    assert finish_reason == "stop"


def test_extract_ollama_response_done_reason_defaults_to_stop():
    """Unlike Claude's `stop_reason`, which defaults to None."""
    service = _service(LLMProvider.OLLAMA)
    data = {"message": {"role": "assistant", "content": "Hi"}}
    _content, _usage, finish_reason = service._extract_ollama_response(data)
    assert finish_reason == "stop"


def test_extract_ollama_response_missing_message_raises():
    service = _service(LLMProvider.OLLAMA)
    with pytest.raises(Exception, match="No message"):
        service._extract_ollama_response({"done": True})


def test_extract_ollama_response_empty_content_returns_empty_string_not_raise():
    """The raise for empty content happens later, in `chat_completion` — the extractor
    itself only logs.
    """
    service = _service(LLMProvider.OLLAMA)
    content, _usage, _finish_reason = service._extract_ollama_response(
        {"message": {"role": "assistant", "content": ""}}
    )
    assert content == ""


def test_extract_ollama_response_usage_requires_both_counts_prompt_only():
    service = _service(LLMProvider.OLLAMA)
    data = {"message": {"content": "Hi"}, "prompt_eval_count": 12}
    _content, usage, _finish_reason = service._extract_ollama_response(data)
    assert usage is None


def test_extract_ollama_response_usage_requires_both_counts_eval_only():
    service = _service(LLMProvider.OLLAMA)
    data = {"message": {"content": "Hi"}, "eval_count": 8}
    _content, usage, _finish_reason = service._extract_ollama_response(data)
    assert usage is None


def test_extract_ollama_response_usage_present_when_both_counts_present():
    service = _service(LLMProvider.OLLAMA)
    data = {"message": {"content": "Hi"}, "prompt_eval_count": 12, "eval_count": 8}
    _content, usage, _finish_reason = service._extract_ollama_response(data)
    assert usage == 20


# ---------------------------------------------------------------------------
# _extract_openai_response — the fallback dialect
# ---------------------------------------------------------------------------


def test_extract_openai_response_reads_choices_and_usage():
    service = _service(LLMProvider.OPENAI)
    data = {
        "choices": [{"message": {"content": "Hi there"}, "finish_reason": "stop"}],
        "usage": {"total_tokens": 42},
    }
    content, usage, finish_reason = service._extract_openai_response(data)
    assert content == "Hi there"
    assert usage == 42
    assert finish_reason == "stop"


def test_extract_openai_response_null_content_becomes_empty_string():
    """A reasoning model that spends its budget in the thought channel returns
    `content: null` with `finish_reason: length` — the key IS present.
    """
    service = _service(LLMProvider.OPENAI)
    data = {"choices": [{"message": {"content": None}, "finish_reason": "length"}]}
    content, _usage, finish_reason = service._extract_openai_response(data)
    assert content == ""
    assert finish_reason == "length"


def test_extract_openai_response_missing_choices_raises():
    service = _service(LLMProvider.OPENAI)
    with pytest.raises(Exception, match="No choices"):
        service._extract_openai_response({})


# ---------------------------------------------------------------------------
# _extract_response_content / _prepare_payload dispatch — every provider member
# ---------------------------------------------------------------------------


CLAUDE_SHAPE = {"content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn"}
OLLAMA_SHAPE = {"message": {"content": "hi"}, "done": True}
OPENAI_SHAPE = {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}


@pytest.mark.parametrize("provider", [LLMProvider.CLAUDE, LLMProvider.ANTHROPIC])
def test_extract_response_content_routes_claude_and_anthropic_to_the_claude_arm(provider):
    service = _service(provider)
    content, _usage, finish_reason = service._extract_response_content(CLAUDE_SHAPE)
    assert content == "hi"
    assert finish_reason == "end_turn"

    # Feeding the OpenAI shape to the Claude arm raises, proving the route matters:
    # a wrong route here would either succeed by coincidence or fail differently.
    with pytest.raises(Exception, match="No content"):
        service._extract_response_content(OPENAI_SHAPE)


def test_extract_response_content_routes_ollama_to_the_ollama_arm():
    service = _service(LLMProvider.OLLAMA)
    content, _usage, _finish_reason = service._extract_response_content(OLLAMA_SHAPE)
    assert content == "hi"

    with pytest.raises(Exception, match="No message"):
        service._extract_response_content(OPENAI_SHAPE)


@pytest.mark.parametrize(
    "provider",
    [
        p
        for p in LLMProvider
        if p
        not in (LLMProvider.CLAUDE, LLMProvider.ANTHROPIC, LLMProvider.OLLAMA, LLMProvider.BEDROCK)
    ],
)
def test_extract_response_content_routes_everything_else_to_the_openai_arm(provider):
    service = _service(provider)
    content, _usage, finish_reason = service._extract_response_content(OPENAI_SHAPE)
    assert content == "hi"
    assert finish_reason == "stop"

    # Feeding the Claude shape to the OpenAI arm raises "No choices" (there is no
    # `choices` key in the Claude shape), proving the route matters.
    with pytest.raises(Exception, match="No choices in LLM response"):
        service._extract_response_content(CLAUDE_SHAPE)


@pytest.mark.parametrize("provider", [LLMProvider.CLAUDE, LLMProvider.ANTHROPIC])
def test_prepare_payload_routes_claude_and_anthropic_to_the_claude_builder(provider):
    service = _service(provider)
    payload = service._prepare_payload(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    )
    assert payload["system"] == "sys"
    assert "options" not in payload


def test_prepare_payload_routes_ollama_to_the_ollama_builder():
    service = _service(LLMProvider.OLLAMA)
    payload = service._prepare_payload([{"role": "user", "content": "hi"}])
    assert "options" in payload
    assert payload["stream"] is False


@pytest.mark.parametrize(
    "provider",
    [
        p
        for p in LLMProvider
        if p
        not in (LLMProvider.CLAUDE, LLMProvider.ANTHROPIC, LLMProvider.OLLAMA, LLMProvider.BEDROCK)
    ],
)
def test_prepare_payload_routes_everything_else_to_the_openai_builder(provider):
    service = _service(provider)
    payload = service._prepare_payload([{"role": "user", "content": "hi"}])
    assert payload["stream"] is False
    assert "options" not in payload
    assert "system" not in payload
