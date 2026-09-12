"""Issue #786 (lane A) — audio_processor.py must never re-embed the raw exception
(and the internal temp-file path it carries) into a user-facing ValueError.

Before this fix, both `extract_audio_from_video` and `convert_audio_format` had a bare
`except Exception as e: raise ValueError(f"... {str(e)}")` as their outer fallback, which
put whatever the underlying exception said — including a temp file path — into the message
`ErrorCategorizationService` (and, downstream, the API) treats as user-facing. This module
pins that the fallback now raises a fixed sentence and that the raw exception is logged, not
returned. It also pins the new DRM/encrypted branch and that every raised sentence
classifies to something more specific than the generic `processing_error` bucket.

Paths below are built from `tempfile.gettempdir()` rather than hardcoded, so the strings
this test asserts on are not literal `/tmp/...` constants (that trips bandit's S108, which
exists to flag hardcoded temp-dir *usage in production code* — here it's just fixture data
naming a path that must NOT leak, not a real temp file this test creates).
"""

import logging
import os
import tempfile

import ffmpeg
import pytest

from app.services.error_categorization_service import ErrorCategorizationService
from app.services.error_categorization_service import UserErrorReason
from app.tasks.transcription import audio_processor

_TMP_DIR = tempfile.gettempdir()
LEAKY_PATH = os.path.join(_TMP_DIR, "tmpXXXXXX", "secret.mp4")
LEAKY_OS_ERROR = OSError(f"[Errno 13] Permission denied: {LEAKY_PATH}")


def _raise_os_error(*_args, **_kwargs):
    raise LEAKY_OS_ERROR


def _real_input(tmp_path, name: str) -> str:
    """A real, non-empty file — `_reject_empty_input` runs before ffmpeg is ever
    invoked, so a fake/nonexistent path would be rejected before the monkeypatched
    `ffmpeg.input` these tests drive is ever reached.
    """
    path = tmp_path / name
    path.write_bytes(b"not a real media file, just needs to be non-empty")
    return str(path)


class _FakeFfmpegOutput:
    """Stand-in for the ffmpeg-python output() chain that raises ffmpeg.Error on .run()."""

    def __init__(self, stderr: bytes):
        self._stderr = stderr

    def output(self, *_args, **_kwargs):
        return self

    def run(self, *_args, **_kwargs):
        raise ffmpeg.Error("ffmpeg", b"", self._stderr)


@pytest.mark.unit
def test_a_generic_extraction_failure_does_not_embed_the_os_error_path(
    monkeypatch, caplog, tmp_path
):
    input_path = _real_input(tmp_path, "input.mp4")
    output_path = str(tmp_path / "out.wav")
    monkeypatch.setattr(audio_processor.ffmpeg, "input", _raise_os_error)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(ValueError) as excinfo:
            audio_processor.extract_audio_from_video(input_path, output_path)

    message = str(excinfo.value)
    assert _TMP_DIR not in message
    assert "secret.mp4" not in message

    log_text = caplog.text
    assert _TMP_DIR in log_text
    assert "secret.mp4" in log_text


@pytest.mark.unit
def test_a_generic_conversion_failure_does_not_embed_the_os_error_path(
    monkeypatch, caplog, tmp_path
):
    input_path = _real_input(tmp_path, "input.mp3")
    output_path = str(tmp_path / "out.wav")
    monkeypatch.setattr(audio_processor.ffmpeg, "input", _raise_os_error)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(ValueError) as excinfo:
            audio_processor.convert_audio_format(input_path, output_path)

    message = str(excinfo.value)
    assert _TMP_DIR not in message
    assert "secret.mp4" not in message

    log_text = caplog.text
    assert _TMP_DIR in log_text
    assert "secret.mp4" in log_text


@pytest.mark.unit
def test_a_drm_protected_video_input_gets_its_own_message(monkeypatch, tmp_path):
    input_path = _real_input(tmp_path, "input.mp4")
    output_path = str(tmp_path / "out.wav")
    fake = _FakeFfmpegOutput(b"this stream appears to be encrypted and cannot be decoded")
    monkeypatch.setattr(audio_processor.ffmpeg, "input", lambda *_a, **_kw: fake)

    with pytest.raises(ValueError) as excinfo:
        audio_processor.extract_audio_from_video(input_path, output_path)

    message = str(excinfo.value)
    assert "DRM" in message or "encrypted" in message.lower()


@pytest.mark.unit
def test_a_drm_protected_audio_input_gets_its_own_message(monkeypatch, tmp_path):
    input_path = _real_input(tmp_path, "input.mp3")
    output_path = str(tmp_path / "out.wav")
    fake = _FakeFfmpegOutput(b"drm-protected content cannot be processed")
    monkeypatch.setattr(audio_processor.ffmpeg, "input", lambda *_a, **_kw: fake)

    with pytest.raises(ValueError) as excinfo:
        audio_processor.convert_audio_format(input_path, output_path)

    message = str(excinfo.value)
    assert "DRM" in message or "encrypted" in message.lower()


@pytest.mark.unit
def test_every_raised_message_classifies_to_a_specific_reason():
    raised_sentences = [
        "This file appears to be corrupted or is not a valid video file. Please check the file and try uploading again.",
        "This video file does not contain any audio tracks. Please upload a video with audio or an audio file directly.",
        "This video format is not supported. Please convert to a common format like MP4, AVI, or MOV and try again.",
        "This file appears to be DRM-protected or encrypted and cannot be processed.",
        "Unable to extract audio from this video file. The file may be corrupted, password-protected, or in an unsupported format.",
        "This file appears to be corrupted or is not a valid audio/video file. Please check the file and try uploading again.",
        "This file format is not supported. Please convert to a common format like MP3, WAV, or MP4 and try again.",
        "Unable to process this file as audio/video content. The file may be corrupted, password-protected, or in an unsupported format.",
    ]

    for sentence in raised_sentences:
        info = ErrorCategorizationService.get_error_info(sentence)
        assert info["category"] != UserErrorReason.PROCESSING_ERROR.value, (
            f"{sentence!r} classified as the generic processing_error bucket"
        )

    no_audio_info = ErrorCategorizationService.get_error_info(
        "This video file does not contain any audio tracks. Please upload a video with audio or an audio file directly."
    )
    assert no_audio_info["category"] == UserErrorReason.NO_AUDIO_TRACK.value

    format_info = ErrorCategorizationService.get_error_info(
        "This video format is not supported. Please convert to a common format like MP4, AVI, or MOV and try again."
    )
    assert format_info["category"] == UserErrorReason.FORMAT_ISSUE.value
