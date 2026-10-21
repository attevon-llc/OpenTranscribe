"""A valid file with nothing to transcribe is NO_SPEECH, never "corrupted" (#1189).

The zero-segment failure used to say "may be corrupted, contain only silence, or be in an
unsupported format", which the categoriser read as FILE_QUALITY because "corrupted" is checked
before the no-speech patterns. The user was told their good file was damaged.
"""

from __future__ import annotations

import pytest

from app.services.error_categorization_service import NO_SPEECH_DETECTED_MESSAGE
from app.services.error_categorization_service import ErrorCategorizationService
from app.services.error_categorization_service import UserErrorReason
from app.utils.error_classification import ErrorCategory

pytestmark = pytest.mark.unit


def test_the_zero_segment_message_classifies_as_no_speech_and_is_permanent():
    result = ErrorCategorizationService.classify_failure(NO_SPEECH_DETECTED_MESSAGE)

    assert result.reason is UserErrorReason.NO_SPEECH
    assert result.retry_category is ErrorCategory.INVALID_MEDIA
    assert "corrupt" not in result.user_message.lower()


def test_a_genuinely_corrupted_file_still_reads_as_file_quality():
    """The control: the fix did not just stop reporting corruption."""
    result = ErrorCategorizationService.classify_failure("The file is corrupted and cannot decode")

    assert result.reason is UserErrorReason.FILE_QUALITY


def test_both_zero_segment_sites_use_the_shared_message():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "app" / "tasks" / "transcription"
    for name in ("context.py", "diarize_task.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert "NO_SPEECH_DETECTED_MESSAGE" in text, name
        assert "may be corrupted, contain only silence" not in text, name
