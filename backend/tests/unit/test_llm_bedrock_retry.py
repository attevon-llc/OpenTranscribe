"""Bedrock retry policy and transient-error classification (issue #1049).

A short Bedrock capacity blip surfaced as a failed chat turn because every client was
built with botocore's default retry policy ("reached max retries: 4"). These pin that
the client — including the one the STREAMING path builds — carries the configured
adaptive policy, and that capacity errors are flagged ``transient`` so chat can say
"try again in a moment" instead of relaying provider prose.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

from app.core.config import Settings
from app.core.config import settings
from app.services import llm_bedrock
from app.services.llm_bedrock import stream_converse
from app.services.llm_bedrock import translate_stream_event

MODEL = "anthropic.claude-haiku-4-5-20251001-v1:0"


def _client_error(code: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "Bedrock is unable to process your request."}},
        "ConverseStream",
    )


def _retries_passed_to_boto3(boto3_client: MagicMock) -> dict:
    config = boto3_client.call_args.kwargs["config"]
    return dict(config.retries)


def test_the_client_is_built_with_adaptive_retries_and_eight_attempts():
    with patch("boto3.client") as boto3_client:
        llm_bedrock._client("us-east-1")

    boto3_client.assert_called_once()
    assert boto3_client.call_args.args == ("bedrock-runtime",)
    assert _retries_passed_to_boto3(boto3_client) == {"mode": "adaptive", "max_attempts": 8}


def test_the_streaming_path_uses_the_retrying_client():
    """ConverseStream is the call that failed in the issue; it must not bypass the policy."""
    fake = MagicMock()
    fake.converse_stream.return_value = {"stream": iter([{"messageStop": {"stopReason": "x"}}])}
    with patch("boto3.client", return_value=fake) as boto3_client:
        list(
            stream_converse(
                model=MODEL,
                region="us-east-1",
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=10,
            )
        )

    fake.converse_stream.assert_called_once()
    assert _retries_passed_to_boto3(boto3_client) == {"mode": "adaptive", "max_attempts": 8}


def test_the_retry_policy_follows_settings(monkeypatch):
    monkeypatch.setattr(settings, "BEDROCK_RETRY_MODE", "standard")
    monkeypatch.setattr(settings, "BEDROCK_MAX_ATTEMPTS", 3)
    with patch("boto3.client") as boto3_client:
        llm_bedrock._client("us-east-1")

    assert _retries_passed_to_boto3(boto3_client) == {"mode": "standard", "max_attempts": 3}


@pytest.mark.parametrize(("mode", "attempts"), [("bogus", 8), ("adaptive", 0)])
def test_an_invalid_retry_setting_falls_back_to_the_default(monkeypatch, mode, attempts):
    monkeypatch.setattr(settings, "BEDROCK_RETRY_MODE", mode)
    monkeypatch.setattr(settings, "BEDROCK_MAX_ATTEMPTS", attempts)

    assert llm_bedrock.retry_config() == {"mode": "adaptive", "max_attempts": 8}


def test_the_settings_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("BEDROCK_RETRY_MODE", "standard")
    monkeypatch.setenv("BEDROCK_MAX_ATTEMPTS", "12")
    fresh = Settings()
    assert (fresh.BEDROCK_RETRY_MODE, fresh.BEDROCK_MAX_ATTEMPTS) == ("standard", 12)


def test_blank_env_values_keep_the_defaults(monkeypatch):
    """`.env.example` ships both blank."""
    monkeypatch.setenv("BEDROCK_RETRY_MODE", "")
    monkeypatch.setenv("BEDROCK_MAX_ATTEMPTS", "")
    fresh = Settings()
    assert (fresh.BEDROCK_RETRY_MODE, fresh.BEDROCK_MAX_ATTEMPTS) == ("adaptive", 8)


# --------------------------------------------------------------------------
# Transient classification
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "transient"),
    [
        ("ServiceUnavailableException", True),
        ("ThrottlingException", True),
        ("ModelNotReadyException", True),
        ("AccessDeniedException", False),
        ("ValidationException", False),
    ],
)
def test_a_failed_converse_stream_call_is_flagged_by_error_code(code, transient):
    fake = MagicMock()
    fake.converse_stream.side_effect = _client_error(code)
    with patch("app.services.llm_bedrock._client", return_value=fake):
        out = list(
            stream_converse(
                model=MODEL,
                region="us-east-1",
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=10,
            )
        )

    assert [e.type for e in out] == ["error"]
    assert out[0].transient is transient


def test_a_non_client_exception_is_not_transient():
    assert llm_bedrock.is_transient_error(RuntimeError("boom")) is False


@pytest.mark.parametrize(
    ("key", "transient"),
    [
        ("throttlingException", True),
        ("serviceUnavailableException", True),
        ("validationException", False),
        ("modelStreamErrorException", False),
    ],
)
def test_mid_stream_exception_members_are_flagged(key, transient):
    event, _ = translate_stream_event({key: {"message": "nope"}}, MODEL)
    assert event is not None and event.type == "error"
    assert event.transient is transient
