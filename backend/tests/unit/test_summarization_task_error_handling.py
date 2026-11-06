"""How the summarization task words and records a provider failure.

``_create_user_friendly_error`` turns an exception message into one of a few fixed sentences, and
``_handle_llm_error`` marks the file failed, tells the user once, and re-raises. Neither had a test.
The property that matters most is that no raw exception text ever reaches the user: a provider
error quotes its own URL, which for a self-hosted model is an internal host and port.

No database, no network.
"""

from __future__ import annotations

from unittest import mock

import pytest

from app.tasks import summarization as summ

pytestmark = pytest.mark.unit

SECRET = "http://internal-llm.invalid:8000/v1 key=sk-SECRET"


@pytest.mark.parametrize(
    "raw, expected",
    [
        (
            # Verbatim from LLMService.chat_completion, the message a real timeout produces.
            "Request timed out after 300 seconds. Content may be too long for processing.",
            "Request timed out. Try reducing video length or contact support.",
        ),
        (
            "ReadTimeout: HTTPSConnectionPool",
            "Request timed out. Try reducing video length or contact support.",
        ),
        (
            "maximum context length is 8192 tokens",
            "Content too long for model. Try shorter videos or contact support.",
        ),
        (
            "Too many tokens in request",
            "Content too long for model. Try shorter videos or contact support.",
        ),
        ("Connection refused", "Network connection failed. Please try again."),
        ("Network is unreachable", "Network connection failed. Please try again."),
        ("something nobody anticipated", "Unknown error occurred during summary generation"),
        ("", "Unknown error occurred during summary generation"),
    ],
)
def test_each_failure_class_maps_to_its_fixed_sentence(raw, expected):
    assert summ._create_user_friendly_error(raw) == expected


def test_matching_ignores_case():
    assert summ._create_user_friendly_error("TIMEOUT") == summ._create_user_friendly_error(
        "timeout"
    )
    assert "too long" in summ._create_user_friendly_error("CONTEXT window exceeded")


def test_the_most_actionable_class_wins_when_a_message_matches_several():
    """A timeout is checked before context length, which is checked before the network."""
    both = summ._create_user_friendly_error("connection timeout while sending the context")
    assert both.startswith("Request timed out")
    assert summ._create_user_friendly_error("context sent over a bad network").startswith(
        "Content too long"
    )


@pytest.mark.parametrize(
    "raw", [SECRET, f"timeout talking to {SECRET}", f"context {SECRET}", f"network {SECRET}"]
)
def test_no_part_of_the_raw_message_is_ever_returned(raw):
    friendly = summ._create_user_friendly_error(raw)

    assert "internal-llm" not in friendly and "sk-SECRET" not in friendly and "8000" not in friendly


@pytest.fixture
def seams():
    with (
        mock.patch.object(summ, "_mark_summary_failed") as mark_failed,
        mock.patch.object(summ, "send_summary_notification") as notify,
    ):
        yield mark_failed, notify


def test_a_provider_failure_marks_the_file_failed_and_tells_the_user_once(seams):
    mark_failed, notify = seams

    with pytest.raises(Exception, match="LLM summarization failed"):
        summ._handle_llm_error(TimeoutError("timeout"), 17, 5, "transcript", "vllm", "gemma")

    mark_failed.assert_called_once_with(17)
    notify.assert_called_once_with(
        5,
        17,
        "failed",
        "AI summary generation failed: Request timed out. Try reducing video length or contact support.",
        0,
    )


def test_the_raised_error_names_the_class_of_failure_chains_the_cause_and_leaks_nothing(seams):
    original = RuntimeError(f"upstream said: {SECRET}")

    with pytest.raises(Exception, match=r"^LLM summarization failed: ") as raised:
        summ._handle_llm_error(original, 17, 5, "transcript", None, None)

    message = str(raised.value)
    assert message == (
        "LLM summarization failed: Unknown error occurred during summary generation. "
        "No fallback summary will be generated."
    )
    assert raised.value.__cause__ is original
    assert "internal-llm" not in message and "sk-SECRET" not in message
    sent = seams[1].call_args.args[3]
    assert "internal-llm" not in sent and "sk-SECRET" not in sent


def test_the_file_is_marked_failed_even_if_the_notification_cannot_be_sent(seams):
    mark_failed, notify = seams
    notify.side_effect = ConnectionError("redis down")

    with pytest.raises(ConnectionError):
        summ._handle_llm_error(RuntimeError("boom"), 17, 5, "transcript", "vllm", "gemma")

    mark_failed.assert_called_once_with(17)
