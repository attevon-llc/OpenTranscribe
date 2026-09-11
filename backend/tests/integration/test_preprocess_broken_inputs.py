"""Issue #786's explicit acceptance criterion — real broken inputs, driven through the real
``prepare_audio_for_transcription`` and real ffmpeg, must:

(a) never surface a raw ffmpeg/OS path or the word "ffmpeg" in the raised message;
(b) classify to three DIFFERENT `error_reason` values (a corrupt container, a video with no
    audio, and a zero-length file are not the same failure and must not collapse into one
    generic bucket); and
(c) the no-audio-track case must be `is_retryable is False`.

No checked-in binary fixtures — each broken input is built in a `tmp_path`.
"""

from __future__ import annotations

import os
import shutil

import pytest

from app.services.error_categorization_service import ErrorCategorizationService
from app.tasks.transcription.audio_processor import prepare_audio_for_transcription

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available on this host"),
]


def _no_path_or_ffmpeg_leaked(message: str, *paths: str) -> None:
    assert "/" not in message, f"raised message contains a path separator: {message!r}"
    assert "ffmpeg" not in message.lower(), f"raised message names ffmpeg: {message!r}"
    for path in paths:
        assert path not in message, f"raised message leaked a fixture path: {message!r}"


@pytest.fixture
def corrupt_container(tmp_path):
    path = tmp_path / "broken.mp4"
    path.write_bytes(os.urandom(512))
    return str(path)


@pytest.fixture
def silent_video(tmp_path):
    """A real, valid video with no audio stream, built by ffmpeg itself."""
    path = tmp_path / "silent.mp4"
    exit_code = os.system(  # noqa: S605 - fixed, non-shell-injectable args, test-only
        f"ffmpeg -f lavfi -i color=c=black:s=320x240:d=1 -y {path} > /dev/null 2>&1"
    )
    assert exit_code == 0, "failed to generate the silent-video fixture with ffmpeg"
    assert path.exists() and path.stat().st_size > 0
    return str(path)


@pytest.fixture
def zero_length_audio(tmp_path):
    path = tmp_path / "empty.mp3"
    path.write_bytes(b"")
    return str(path)


def test_a_corrupt_container_raises_a_clean_message(corrupt_container, tmp_path):
    with pytest.raises(ValueError) as excinfo:
        prepare_audio_for_transcription(corrupt_container, "video/mp4", str(tmp_path))

    _no_path_or_ffmpeg_leaked(str(excinfo.value), corrupt_container)


def test_a_video_with_no_audio_track_raises_a_clean_message(silent_video, tmp_path):
    with pytest.raises(ValueError) as excinfo:
        prepare_audio_for_transcription(silent_video, "video/mp4", str(tmp_path))

    _no_path_or_ffmpeg_leaked(str(excinfo.value), silent_video)


def test_a_zero_length_file_raises_a_clean_message(zero_length_audio, tmp_path):
    with pytest.raises(ValueError) as excinfo:
        prepare_audio_for_transcription(zero_length_audio, "audio/mpeg", str(tmp_path))

    _no_path_or_ffmpeg_leaked(str(excinfo.value), zero_length_audio)


def test_the_three_broken_inputs_classify_to_three_different_reasons(
    corrupt_container, silent_video, zero_length_audio, tmp_path
):
    messages = {}
    for name, (path, content_type) in {
        "corrupt": (corrupt_container, "video/mp4"),
        "no_audio": (silent_video, "video/mp4"),
        "zero_length": (zero_length_audio, "audio/mpeg"),
    }.items():
        with pytest.raises(ValueError) as excinfo:
            prepare_audio_for_transcription(path, content_type, str(tmp_path))
        messages[name] = str(excinfo.value)

    reasons = {
        name: ErrorCategorizationService.get_error_info(message)["category"]
        for name, message in messages.items()
    }

    assert len(set(reasons.values())) == 3, (
        f"expected three distinct error_reason values, got {reasons}"
    )

    no_audio_info = ErrorCategorizationService.get_error_info(messages["no_audio"])
    assert no_audio_info["is_retryable"] is False
