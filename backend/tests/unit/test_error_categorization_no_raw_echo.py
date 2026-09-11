"""Issue #786 (lane A) — the error categorization service must never echo the raw
exception text back to a caller.

Before this fix, every ``_handle_*`` f-stringed the raw ``error_message`` straight into
``user_message`` and every suggestion list, and ``get_error_info`` additionally carried the
raw text under an ``"original_error"`` key. Either path lets an internal path (`/tmp/...`),
a stack detail, or any other implementation detail reach a client. This module pins that
none of that happens any more, and that a missing audio track is its own reason with its own
(non-retryable) verdict rather than falling into the generic FILE_QUALITY/PROCESSING_ERROR
buckets.
"""

import json

import pytest

from app.services.error_categorization_service import ErrorCategorizationService
from app.services.error_categorization_service import UserErrorReason

SENTINEL = "SENTINEL-/srv/internal/secret-path"

# One raw message per reason, each carrying the sentinel so leakage is unambiguous.
RAW_MESSAGES_BY_REASON = {
    UserErrorReason.FILE_QUALITY: f"This file is corrupted: {SENTINEL}",
    UserErrorReason.NO_AUDIO_TRACK: f"does not contain any audio tracks: {SENTINEL}",
    UserErrorReason.NO_SPEECH: f"no speech detected in recording: {SENTINEL}",
    UserErrorReason.FORMAT_ISSUE: f"unsupported codec found: {SENTINEL}",
    UserErrorReason.NETWORK_ERROR: f"connection timeout while downloading: {SENTINEL}",
    UserErrorReason.PERMISSION_ERROR: f"access denied to protected content: {SENTINEL}",
    UserErrorReason.PROCESSING_ERROR: f"something went wrong internally: {SENTINEL}",
}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("reason", "raw_message"),
    list(RAW_MESSAGES_BY_REASON.items()),
    ids=[r.value for r in RAW_MESSAGES_BY_REASON],
)
def test_user_message_never_contains_the_raw_error(reason, raw_message):
    info = ErrorCategorizationService.get_error_info(raw_message)

    assert info["category"] == reason.value, (
        f"expected the raw message to classify as {reason.value}, got {info['category']}"
    )
    assert SENTINEL not in info["user_message"], (
        f"user_message leaked the raw error: {info['user_message']!r}"
    )
    for suggestion in info["suggestions"]:
        assert SENTINEL not in suggestion, f"suggestion leaked the raw error: {suggestion!r}"
    assert SENTINEL not in json.dumps(info), "get_error_info() leaked the raw error somewhere"


@pytest.mark.unit
def test_get_error_info_does_not_carry_the_original_error():
    info = ErrorCategorizationService.get_error_info(f"corrupted file: {SENTINEL}")

    assert "original_error" not in info


@pytest.mark.unit
def test_a_file_with_no_audio_track_is_its_own_reason_and_is_not_retryable():
    raw = (
        "Audio preprocessing failed: This video file does not contain any audio tracks. "
        "Please upload a video with audio or an audio file directly."
    )
    info = ErrorCategorizationService.get_error_info(raw)

    assert info["category"] == UserErrorReason.NO_AUDIO_TRACK.value
    assert info["is_retryable"] is False


@pytest.mark.unit
def test_a_corrupt_container_is_distinguished_from_a_missing_audio_track():
    corrupt = ErrorCategorizationService.get_error_info(
        "This file appears to be corrupted or is not a valid video file."
    )
    no_audio = ErrorCategorizationService.get_error_info(
        "This video file does not contain any audio tracks."
    )

    assert corrupt["category"] != no_audio["category"]
    assert no_audio["category"] == UserErrorReason.NO_AUDIO_TRACK.value


@pytest.mark.unit
def test_an_unsupported_format_is_a_format_issue():
    info = ErrorCategorizationService.get_error_info(
        "This video format is not supported. Please convert to a common format."
    )

    assert info["category"] == UserErrorReason.FORMAT_ISSUE.value


@pytest.mark.unit
def test_every_reason_has_a_non_empty_message_and_suggestions():
    for reason, raw_message in RAW_MESSAGES_BY_REASON.items():
        info = ErrorCategorizationService.get_error_info(raw_message)
        assert info["user_message"], f"{reason} produced an empty user_message"
        assert info["suggestions"], f"{reason} produced no suggestions"

    # UNCLASSIFIED is reached via None/empty input, not a pattern match.
    unclassified_info = ErrorCategorizationService.get_error_info(None)
    assert unclassified_info["category"] == UserErrorReason.UNCLASSIFIED.value
    assert unclassified_info["user_message"]
    assert unclassified_info["suggestions"]
