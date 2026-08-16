"""Reasoning must be *activated* on the wire, or vLLM streams it as the answer (issue #439).

Measured against a real vLLM 0.19 serving Gemma 4 E4B with `--reasoning-parser
gemma4`, which is the configuration the bug was found in:

* The gemma4 chat template, when thinking is **off** (its default), appends
  ``<|channel>thought\\n<channel|>`` to the *prompt* — an already-closed, empty
  thought channel meant to suppress reasoning. The model reasons anyway, so its
  *generated* text contains no ``<|channel>`` opener, only a bare ``<channel|>``
  closer between the chain-of-thought and the answer.
* vLLM's **streaming** reasoning parser only enters reasoning mode on the opener,
  so it never fires: the whole chain-of-thought is streamed on ``delta.content``.
  And because ``Gemma4ReasoningParser.adjust_request`` sets
  ``skip_special_tokens=False`` to preserve boundary tokens, the bare closer is
  decoded into ``delta.content`` as the literal text ``<channel|>``.
* Sending ``chat_template_kwargs={"enable_thinking": true}`` makes the template
  emit ``<|think|>`` and stop pre-closing the channel. The model then emits a
  well-formed ``<|channel>thought ... <channel|>`` block, the parser engages, and
  reasoning arrives on ``delta.reasoning`` with clean content.

So the defect is in the *request*, not in our SSE parsing —
:func:`app.services.llm_stream.parse_openai_sse` already reads both
``reasoning_content`` and ``reasoning``. These tests pin the request instead.
"""

from __future__ import annotations

import pytest

from app.services.llm_service import LLMConfig
from app.services.llm_service import LLMProvider
from app.services.llm_service import LLMService

MESSAGES = [{"role": "user", "content": "what did the team decide?"}]


def _service(provider: LLMProvider, **overrides) -> LLMService:
    config = LLMConfig(
        provider=provider,
        model=overrides.pop("model", "gemma-4-e4b"),
        base_url=overrides.pop("base_url", "http://llm-test-vllm:8000/v1"),
        api_key="not-a-secret",
        **overrides,
    )
    return LLMService(config)


def test_vllm_payload_activates_thinking_so_the_server_separates_reasoning():
    """Without this key the gemma4 streaming parser never engages (issue #439)."""
    payload = _service(LLMProvider.VLLM)._prepare_payload(MESSAGES)

    assert payload["chat_template_kwargs"] == {"enable_thinking": True}


def test_vllm_caller_can_turn_thinking_off():
    """`False` SENDS the key; it does not drop it (issue #64).

    This assertion used to be ``"chat_template_kwargs" not in payload`` — i.e.
    "off" was implemented as the *control* arm. On gemma-4-e4b the two happen to
    be byte-identical (measured: 931 reasoning characters either way), but on a
    template where the switch actually works they are not, and shipping the
    control arm as the off switch would mean the request a user's toggle sends
    is not the request the capability probe measured.
    """
    payload = _service(LLMProvider.VLLM)._prepare_payload(MESSAGES, enable_thinking=False)

    assert payload["chat_template_kwargs"] == {"enable_thinking": False}


def test_none_is_the_probe_control_arm_and_omits_the_key():
    """The third arm, and the only way to send no instruction at all.

    ``services/llm_reasoning`` needs it to ask "what does this model do when
    nobody says anything?", which is the comparison that decides whether an
    off-switch exists. Nothing on the chat path passes it.
    """
    payload = _service(LLMProvider.VLLM)._prepare_payload(MESSAGES, enable_thinking=None)

    assert "chat_template_kwargs" not in payload


@pytest.mark.parametrize("provider", [LLMProvider.OPENAI, LLMProvider.CUSTOM])
def test_non_vllm_openai_compatible_providers_are_not_sent_the_key(provider):
    """`chat_template_kwargs` is a vLLM extension.

    Scoped exactly like ``stream_options.include_usage``
    (``llm_stream.USAGE_OPTION_PROVIDERS``): OpenAI itself and the "custom"
    OpenAI-clones self-hosters point at can reject an unknown payload key with a
    400, which would break chat outright rather than degrade it.
    """
    payload = _service(provider)._prepare_payload(MESSAGES)

    assert "chat_template_kwargs" not in payload


def test_streaming_and_non_streaming_send_the_same_activation():
    """Both paths share ``_prepare_payload``; a drift here is a silent one."""
    from app.services.llm_stream import apply_stream_payload

    service = _service(LLMProvider.VLLM)
    streaming = apply_stream_payload(service._prepare_payload(MESSAGES), "vllm")

    assert streaming["chat_template_kwargs"] == {"enable_thinking": True}
    assert streaming["stream"] is True


def test_a_response_truncated_inside_the_thought_channel_is_empty_not_none():
    """Measured: vLLM returns ``"content": null`` when the budget runs out mid-thought.

    ``finish_reason: "length"`` with all tokens spent in the reasoning channel
    leaves ``message.content`` explicitly null, which ``dict.get(k, "")`` returns
    as ``None`` — the key exists. Callers concatenate and strip this value.
    """
    service = _service(LLMProvider.VLLM)
    truncated = {
        "choices": [
            {
                "message": {"role": "assistant", "content": None, "reasoning": "Here's a think"},
                "finish_reason": "length",
            }
        ],
        "usage": {"total_tokens": 12},
    }

    content, usage_tokens, finish_reason = service._extract_openai_response(truncated)

    assert content == ""
    assert usage_tokens == 12
    assert finish_reason == "length"
