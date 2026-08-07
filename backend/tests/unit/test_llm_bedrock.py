"""Bedrock Converse provider.

The event translation, message mapping, and model-ID resolution are pure functions
precisely so they can be tested against recorded ConverseStream payloads without an
AWS account, credentials, or network. The boto3 call itself is the only part that
needs a live account, and it is the thinnest part.
"""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.services.llm_bedrock import BedrockNotConfiguredError
from app.services.llm_bedrock import build_request_metadata
from app.services.llm_bedrock import resolve_model_id
from app.services.llm_bedrock import split_system_messages
from app.services.llm_bedrock import stream_converse
from app.services.llm_bedrock import translate_stream_event

MODEL = "anthropic.claude-haiku-4-5-20251001-v1:0"


# --------------------------------------------------------------------------
# Model ID resolution — cross-region inference profiles
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("region", "expected_prefix"),
    [
        ("us-east-1", "us."),
        ("us-west-2", "us."),
        ("eu-west-1", "eu."),
        ("ap-southeast-2", "apac."),
        ("ca-central-1", "us."),
    ],
)
def test_bare_model_id_gets_the_region_geo_prefix(region, expected_prefix):
    """A bare foundation-model ID only works where the model is provisioned in that
    exact region; the prefixed inference-profile ID load-balances across the geography.
    """
    assert resolve_model_id(MODEL, region) == f"{expected_prefix}{MODEL}"


def test_already_prefixed_id_is_left_alone():
    """Double-prefixing produces an ID that does not exist."""
    assert resolve_model_id(f"us.{MODEL}", "us-east-1") == f"us.{MODEL}"


def test_profile_arn_is_left_alone():
    """An explicit application-inference-profile ARN is how cost allocation tags are
    attached, so it must survive verbatim.
    """
    arn = "arn:aws:bedrock:us-east-1:123456789012:inference-profile/my-profile"
    assert resolve_model_id(arn, "us-east-1") == arn


def test_unknown_region_falls_back_to_the_bare_id():
    """Better to send the bare ID and let AWS answer than to invent a prefix."""
    assert resolve_model_id(MODEL, "xx-nowhere-1") == MODEL


def test_empty_model_is_a_configuration_error():
    with pytest.raises(BedrockNotConfiguredError):
        resolve_model_id("", "us-east-1")


# --------------------------------------------------------------------------
# Message mapping — Converse is stricter than the OpenAI shape
# --------------------------------------------------------------------------


def test_system_messages_are_lifted_out_of_the_turn_list():
    """Converse rejects a `system` role inside `messages`; it takes a separate field."""
    system, turns = split_system_messages(
        [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
        ]
    )
    assert system == [{"text": "You are helpful."}]
    assert turns == [{"role": "user", "content": [{"text": "Hi"}]}]


def test_consecutive_same_role_messages_are_merged():
    """Converse requires strictly alternating turns. The chat history builder can emit
    two user turns in a row after an edit-and-resubmit, which would 400 the call.
    """
    _system, turns = split_system_messages(
        [
            {"role": "user", "content": "First"},
            {"role": "user", "content": "Second"},
            {"role": "assistant", "content": "Reply"},
        ]
    )
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[0]["content"][0]["text"] == "First\n\nSecond"


def test_leading_assistant_turn_is_prefixed_with_a_user_turn():
    """Converse requires the first turn to be `user`."""
    _system, turns = split_system_messages([{"role": "assistant", "content": "Hello"}])
    assert turns[0]["role"] == "user"
    assert turns[1]["role"] == "assistant"


def test_empty_content_is_dropped():
    _system, turns = split_system_messages(
        [{"role": "user", "content": ""}, {"role": "user", "content": "real"}]
    )
    assert len(turns) == 1


# --------------------------------------------------------------------------
# requestMetadata — AWS-side cost attribution
# --------------------------------------------------------------------------


def test_metadata_values_are_stringified():
    """AWS rejects non-string values outright."""
    out = build_request_metadata({"org_id": 42, "user_id": 7})
    assert out == {"org_id": "42", "user_id": "7"}


def test_metadata_is_capped_at_16_entries_and_256_chars():
    """Exceeding either limit rejects the whole request, so it is clamped here rather
    than at each call site.
    """
    out = build_request_metadata({f"k{i}": "v" for i in range(30)})
    assert len(out) == 16

    out = build_request_metadata({"k": "x" * 500})
    assert len(out["k"]) == 256


def test_none_values_are_omitted():
    assert build_request_metadata({"org_id": None, "user_id": 1}) == {"user_id": "1"}


# --------------------------------------------------------------------------
# Event translation
# --------------------------------------------------------------------------


def test_content_delta_becomes_a_text_event():
    event, stop = translate_stream_event({"contentBlockDelta": {"delta": {"text": "hello"}}}, MODEL)
    assert event is not None
    assert event.type == "delta"
    assert event.text == "hello"
    assert stop is None


def test_message_stop_carries_the_finish_reason_not_an_event():
    event, stop = translate_stream_event({"messageStop": {"stopReason": "end_turn"}}, MODEL)
    assert event is None
    assert stop == "end_turn"


def test_metadata_event_carries_usage_including_cache_tokens():
    """Cache reads bill far below and cache writes above the uncached rate, so folding
    them into prompt_tokens would misprice every cache-enabled request.
    """
    event, _stop = translate_stream_event(
        {
            "metadata": {
                "usage": {
                    "inputTokens": 1200,
                    "outputTokens": 300,
                    "cacheReadInputTokens": 900,
                    "cacheWriteInputTokens": 100,
                }
            }
        },
        MODEL,
    )
    assert event is not None
    assert event.type == "usage"
    assert event.prompt_tokens == 1200
    assert event.completion_tokens == 300
    assert event.cache_read_tokens == 900
    assert event.cache_write_tokens == 100


def test_throttling_becomes_an_error_event():
    """Bedrock reports throttling as a stream member, not a raised exception — an
    unhandled one would look like a silently truncated answer.
    """
    event, _stop = translate_stream_event(
        {"throttlingException": {"message": "Too many requests"}}, MODEL
    )
    assert event is not None
    assert event.type == "error"
    assert "throttled" in event.message.lower()


@pytest.mark.parametrize(
    "key",
    [
        "internalServerException",
        "modelStreamErrorException",
        "validationException",
        "serviceUnavailableException",
    ],
)
def test_every_stream_exception_shape_becomes_an_error_event(key):
    event, _stop = translate_stream_event({key: {"message": "boom"}}, MODEL)
    assert event is not None
    assert event.type == "error"


def test_unknown_event_shapes_are_ignored_rather_than_fatal():
    """A newer API version adding an event type must not break the stream."""
    assert translate_stream_event({"someFutureEvent": {}}, MODEL) == (None, None)
    assert translate_stream_event({"messageStart": {"role": "assistant"}}, MODEL) == (None, None)


# --------------------------------------------------------------------------
# stream_converse — end-to-end over a faked boto3 client
# --------------------------------------------------------------------------


def _fake_client(events):
    client = MagicMock()
    client.converse_stream.return_value = {"stream": iter(events)}
    return client


def _drain(**overrides: Any) -> list:
    kwargs: dict[str, Any] = {
        "model": MODEL,
        "region": "us-east-1",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
    }
    kwargs.update(overrides)
    return list(stream_converse(**kwargs))


def test_happy_path_yields_deltas_then_usage_then_done():
    events = [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"delta": {"text": "Hello"}}},
        {"contentBlockDelta": {"delta": {"text": " world"}}},
        {"messageStop": {"stopReason": "end_turn"}},
        {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 2}}},
    ]
    with patch("app.services.llm_bedrock._client", return_value=_fake_client(events)):
        out = _drain()

    assert [e.type for e in out] == ["delta", "delta", "usage", "done"]
    assert "".join(e.text for e in out if e.type == "delta") == "Hello world"
    assert out[-1].finish_reason == "end_turn"


def test_stream_always_terminates_with_exactly_one_done_or_error():
    """The contract every other provider parser honours."""
    with patch("app.services.llm_bedrock._client", return_value=_fake_client([])):
        out = _drain()
    terminals = [e for e in out if e.type in ("done", "error")]
    assert len(terminals) == 1


# --------------------------------------------------------------------------
# inferenceConfig — sampling parameters are opt-in (issue #359)
# --------------------------------------------------------------------------


def _sent_inference_config(**overrides: Any) -> dict:
    """Run a stream and return the inferenceConfig Bedrock was actually given."""
    client = _fake_client([{"messageStop": {"stopReason": "end_turn"}}])
    with patch("app.services.llm_bedrock._client", return_value=client):
        _drain(**overrides)
    config: dict = client.converse_stream.call_args.kwargs["inferenceConfig"]
    return config


def test_sampling_params_are_omitted_when_unset():
    """Newer models reject non-default sampling params, so we must not invent them."""
    cfg = _sent_inference_config()
    assert cfg == {"maxTokens": 100}
    assert "topP" not in cfg
    assert "temperature" not in cfg


def test_top_p_is_sent_as_camel_case_top_p():
    """Converse uses topP, not the OpenAI-style top_p."""
    cfg = _sent_inference_config(top_p=0.4)
    assert cfg["topP"] == 0.4
    assert "top_p" not in cfg


def test_temperature_and_top_p_can_be_sent_together():
    cfg = _sent_inference_config(temperature=0.15, top_p=0.9)
    assert cfg == {"maxTokens": 100, "temperature": 0.15, "topP": 0.9}


def test_top_p_of_zero_is_sent_not_treated_as_absent():
    """0.0 is a legitimate value; a falsy check here would silently drop it."""
    assert _sent_inference_config(top_p=0.0)["topP"] == 0.0


def test_cancel_event_stops_the_stream_and_reports_cancelled():
    cancel = threading.Event()
    cancel.set()
    events = [{"contentBlockDelta": {"delta": {"text": "should not appear"}}}]
    with patch("app.services.llm_bedrock._client", return_value=_fake_client(events)):
        out = _drain(cancel_event=cancel)

    assert [e.type for e in out] == ["done"]
    assert out[0].finish_reason == "cancelled"


def test_client_construction_failure_is_reported_in_band():
    """A streaming response has already committed its HTTP status by the time most
    provider problems surface, so errors are relayed as events, never raised.
    """
    with patch(
        "app.services.llm_bedrock._client",
        side_effect=BedrockNotConfiguredError("No AWS region configured"),
    ):
        out = _drain()

    assert [e.type for e in out] == ["error"]
    assert "region" in out[0].message.lower()


def test_api_call_failure_is_reported_in_band():
    client = MagicMock()
    client.converse_stream.side_effect = RuntimeError("AccessDeniedException")
    with patch("app.services.llm_bedrock._client", return_value=client):
        out = _drain()

    assert [e.type for e in out] == ["error"]
    assert "AccessDenied" in out[0].message


def test_system_prompt_and_metadata_reach_the_request():
    client = _fake_client([{"messageStop": {"stopReason": "end_turn"}}])
    with patch("app.services.llm_bedrock._client", return_value=client):
        _drain(
            messages=[
                {"role": "system", "content": "Be terse."},
                {"role": "user", "content": "hi"},
            ],
            attribution={"org_id": 3, "conversation_uuid": "abc"},
        )

    request = client.converse_stream.call_args.kwargs
    assert request["system"] == [{"text": "Be terse."}]
    assert request["requestMetadata"] == {"org_id": "3", "conversation_uuid": "abc"}
    assert request["modelId"].startswith("us.")


def test_temperature_is_omitted_when_unset():
    """Newer models reject non-default sampling parameters, so an unset temperature
    must not become an explicit default.
    """
    client = _fake_client([{"messageStop": {"stopReason": "end_turn"}}])
    with patch("app.services.llm_bedrock._client", return_value=client):
        _drain()

    assert "temperature" not in client.converse_stream.call_args.kwargs["inferenceConfig"]
