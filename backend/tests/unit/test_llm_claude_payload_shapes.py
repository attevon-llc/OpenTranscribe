"""Recorded request shapes for ``_prepare_claude_payload`` per model family.

Anthropic returns 400 for an assistant prefill on Claude 4.6+ and for sampling
parameters on Opus 4.7+ / Sonnet 5+ / Haiku 5+ (Claude docs, migration guides). A model that
accepts both keeps the historical request; the others get a JSON instruction (or structured
outputs when a schema is supplied) and no ``temperature``.
"""

import pytest

from app.services.llm_service import LLMService
from app.services.llm_types import LLMConfig
from app.services.llm_types import LLMProvider

MESSAGES = [
    {"role": "system", "content": "You summarize."},
    {"role": "user", "content": "Summarize this."},
]
SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
    "additionalProperties": False,
}

OLD = "claude-haiku-4-5-20251001"
PREFILL_GONE = ["claude-opus-4-6", "claude-sonnet-4-6"]
SAMPLING_GONE = [
    "claude-opus-4-7",
    "claude-opus-5",
    "claude-opus-5-5",
    "claude-sonnet-5",
    "claude-sonnet-5-5",
    "claude-haiku-5-5",
]
NEW = PREFILL_GONE + SAMPLING_GONE


def _service(model: str) -> LLMService:
    return LLMService(
        LLMConfig(
            provider=LLMProvider.ANTHROPIC,
            model=model,
            base_url="http://llm.test",
            api_key="not-a-secret",
        )
    )


def _payload(model: str, **kwargs):
    return _service(model)._prepare_payload(MESSAGES, max_tokens=500, temperature=0.1, **kwargs)


def test_old_model_keeps_prefill_and_temperature():
    payload = _payload(OLD, prefill_json=True)
    assert payload["messages"][-1] == {"role": "assistant", "content": "{"}
    assert payload["temperature"] == 0.1
    assert payload["system"] == "You summarize."
    assert "output_config" not in payload


@pytest.mark.parametrize("model", NEW)
def test_new_models_never_end_with_an_assistant_turn(model):
    payload = _payload(model, prefill_json=True)
    assert [m["role"] for m in payload["messages"]] == ["user"]


@pytest.mark.parametrize("model", NEW)
def test_new_models_get_a_json_instruction_in_the_system_prompt(model):
    payload = _payload(model, prefill_json=True)
    assert payload["system"].startswith("You summarize.")
    assert "single valid JSON object" in payload["system"]
    assert "output_config" not in payload


def test_json_instruction_becomes_the_system_prompt_when_there_was_none():
    payload = _service("claude-sonnet-5-5")._prepare_payload(
        [{"role": "user", "content": "Hi"}], prefill_json=True
    )
    assert payload["system"].startswith("Respond with a single valid JSON object")


@pytest.mark.parametrize("model", SAMPLING_GONE)
def test_sampling_models_get_no_sampling_parameters(model):
    payload = _payload(model, prefill_json=True, top_p=0.9)
    assert not {"temperature", "top_p", "top_k"} & payload.keys()


@pytest.mark.parametrize("model", PREFILL_GONE)
def test_4_6_models_drop_prefill_but_keep_temperature(model):
    assert _payload(model, prefill_json=True)["temperature"] == 0.1


@pytest.mark.parametrize("model", NEW)
def test_no_json_request_adds_nothing_to_the_system_prompt(model):
    assert _payload(model)["system"] == "You summarize."


def test_old_model_with_a_schema_uses_structured_outputs_not_prefill():
    payload = _payload(OLD, json_schema=SCHEMA)
    assert payload["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}
    assert payload["messages"][-1]["role"] == "user"
    assert "single valid JSON object" not in payload["system"]


@pytest.mark.parametrize("model", NEW)
def test_new_models_with_a_schema_use_structured_outputs(model):
    payload = _payload(model, json_schema=SCHEMA)
    assert payload["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}
    assert payload["messages"][-1]["role"] == "user"
    assert "single valid JSON object" not in payload["system"]


def test_schema_on_a_model_without_structured_outputs_falls_back_to_prefill():
    payload = _payload("claude-3-5-sonnet-20241022", json_schema=SCHEMA)
    assert "output_config" not in payload
    assert payload["messages"][-1] == {"role": "assistant", "content": "{"}


def test_unknown_claude_id_gets_the_safe_shape():
    payload = _payload("claude-opus-future", prefill_json=True)
    assert payload["messages"][-1]["role"] == "user"
    assert "temperature" not in payload
    assert "single valid JSON object" in payload["system"]


@pytest.mark.parametrize("model", NEW)
def test_a_caller_supplied_final_assistant_turn_is_dropped(model):
    """Topic extraction and speaker ID end with a steering assistant turn."""
    service = _service(model)
    payload = service._prepare_payload(
        [*MESSAGES, {"role": "assistant", "content": "<thinking>"}], max_tokens=500
    )
    assert [m["role"] for m in payload["messages"]] == ["user"]


def test_a_caller_supplied_final_assistant_turn_is_kept_on_old_models():
    payload = _service(OLD)._prepare_payload(
        [*MESSAGES, {"role": "assistant", "content": "<thinking>"}], max_tokens=500
    )
    assert payload["messages"][-1] == {"role": "assistant", "content": "<thinking>"}


def test_non_claude_id_behind_the_anthropic_provider_is_unchanged():
    payload = _payload("my-gateway-model", prefill_json=True)
    assert payload["messages"][-1] == {"role": "assistant", "content": "{"}
    assert payload["temperature"] == 0.1


# --- parsing both reply shapes --------------------------------------------------------


def _parse(content: str) -> dict:
    from app.services.llm_types import LLMResponse

    response = LLMResponse(content=content, model="m", provider="anthropic", usage_tokens=1)
    return _service("claude-sonnet-5-5")._parse_summary_response(response, 10)


@pytest.mark.parametrize(
    "reply",
    [
        '"title": "T", "tldr": "x"}',  # continuation of a "{" prefill: no opening brace
        '{"title": "T", "tldr": "x"}',  # whole object, as without a prefill
        '```json\n{"title": "T", "tldr": "x"}\n```',  # fenced
    ],
)
def test_summary_parser_accepts_a_reply_with_or_without_the_opening_brace(reply):
    parsed = _parse(reply)
    assert parsed["title"] == "T"
    assert parsed["tldr"] == "x"


# --- reading the reply ----------------------------------------------------------------


def test_extract_reads_the_text_block_after_a_thinking_block():
    data = {
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": '{"title": "T"}'},
        ],
        "usage": {"input_tokens": 3, "output_tokens": 4},
        "stop_reason": "end_turn",
    }
    content, usage, finish = _service("claude-opus-5-5")._extract_response_content(data)
    assert content == '{"title": "T"}'
    assert (usage, finish) == (7, "end_turn")
