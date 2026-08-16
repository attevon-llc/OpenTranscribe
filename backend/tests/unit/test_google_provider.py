"""Unit tests for ``app/services/asr/google_provider.py`` (issue #445).

Network-free: `google.cloud.speech.SpeechClient` is patched in place on the real,
installed SDK module (`from google.cloud import speech` inside the provider's methods
binds to that module object, so patching its `SpeechClient` attribute intercepts every
call site without a mock-the-whole-package fixture). Response objects are built from the
**real** `google-cloud-speech` proto-plus classes (`WordInfo`, `SpeechRecognitionAlternative`,
`SpeechRecognitionResult`) rather than ad hoc mocks, so the schema asserted here
(`words[].speaker_tag` as a 1-indexed int, `words[].start_time`/`end_time` as
`datetime.timedelta`, `alternatives[].transcript`/`confidence`) is the one the live SDK
actually produces — confirmed interactively against the installed
`google-cloud-speech>=2.40.0` package before writing these tests.

What is pinned, in order:

1. **Now-fixed regression guard**: `transcribe()`'s exception handler passes the caught
   exception through ``self._sanitize_error(...)`` before raising ``RuntimeError``. The
   module's own docstring/inline comment (google_provider.py L72) warns that "Google ADC
   errors can expose service-account tokens in some SDK versions" — before this fix, a
   raw ADC failure would have propagated a bearer token straight into logs/UI.
2. **Now-fixed consistency bug: hand-built ``SPEAKER_XX`` strings instead of the shared
   normalizer.** This provider used to hand-build labels
   (``f"SPEAKER_{(cur_tag - 1):02d}"``) instead of calling the shared
   ``normalize_speaker_label``/``_normalize_speaker_label`` helper every other provider uses
   (``deepgram_provider.py``, ``aws_provider.py``). The two were numerically equivalent, but
   the single-source-of-truth guarantee ``asr/CLAUDE.md`` documents for the helper did not
   actually hold for this file — a future change to the canonical format (padding width, a
   different fallback rule) would have silently failed to apply here. The provider now calls
   ``self._normalize_speaker_label(cur_tag - 1)`` directly. The test below asserts BOTH that
   the shared helper is genuinely invoked (via a spy) and that the output is unchanged (a
   behavior regression guard) — Google's tag is still 1-indexed and still converted to
   0-indexed *before* being handed to the helper, per that same doc.
3. ``validate_connection()`` only constructs ``speech.SpeechClient()`` — pinning the
   documented false-positive (a bad/fake credential still "validates") from
   ``app/services/asr/CLAUDE.md``: "azure and google `validate_connection()` make no network
   call — a bad credential still 'validates'".
4. Empty-word-list handling: a non-diarized alternative with no ``words`` but a non-empty
   ``transcript`` loses real segment timing — ``start``/``end`` default to ``0.0``.
5. The whole audio file is read into memory (``f.read()``) before being sent as
   ``RecognitionAudio(content=...)`` — a memory-usage regression guard, not a bug: pinning it
   means a future streaming rewrite is a deliberate change, not an accidental one.
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from google.cloud import speech

from app.services.asr.base import normalize_speaker_label
from app.services.asr.google_provider import GoogleASRProvider
from app.services.asr.types import ASRConfig


def _provider() -> GoogleASRProvider:
    return GoogleASRProvider(api_key=None, model_name="chirp-3", credentials_file=None)


def _config(**overrides: Any) -> ASRConfig:
    base: dict[str, Any] = {
        "language": "en-US",
        "min_speakers": 1,
        "max_speakers": 5,
        "enable_diarization": True,
    }
    base.update(overrides)
    return ASRConfig(**base)


def _word(text: str, start: float, end: float, speaker_tag: int) -> speech.WordInfo:
    return speech.WordInfo(
        word=text,
        start_time=datetime.timedelta(seconds=start),
        end_time=datetime.timedelta(seconds=end),
        speaker_tag=speaker_tag,
    )


def _mock_client(response: Any) -> MagicMock:
    """A SpeechClient double whose long_running_recognize().result() returns *response*."""
    client = MagicMock()
    op = MagicMock()
    op.result.return_value = response
    client.long_running_recognize.return_value = op
    return client


def _write_audio(tmp_path, content: bytes = b"\x00\x01RIFFfakewavdata\xff\xfe") -> str:
    path = tmp_path / "audio.wav"
    path.write_bytes(content)
    return str(path)


# ── 1. Sanitization regression guard ─────────────────────────────────────────────────


def test_transcribe_sanitizes_credential_leak_in_exception_message(tmp_path):
    """A real ADC failure can embed a bearer/service-account token in its message.

    google_provider.py's own inline comment (L72) says so explicitly: "Google ADC errors
    can expose service-account tokens in some SDK versions." Before the orchestrating
    session's fix, `transcribe()`'s except-block re-raised `str(exc)` verbatim; it now routes
    through `self._sanitize_error(...)`. This test plants a fake bearer token in the mocked
    SDK exception and confirms it never reaches the raised RuntimeError.
    """
    fake_token = "ya29.a0AfH6SMBFAKEtokenTHATmustNEVERleak1234567890"
    leaking_message = f"401 Unauthorized: invalid credentials (Authorization: Bearer {fake_token})"
    client = MagicMock()
    client.long_running_recognize.side_effect = RuntimeError(leaking_message)

    provider = _provider()
    audio_path = _write_audio(tmp_path)

    with patch.object(speech, "SpeechClient", return_value=client):
        with pytest.raises(RuntimeError) as excinfo:
            provider.transcribe(audio_path, _config())

    raised_message = str(excinfo.value)
    assert fake_token not in raised_message
    assert "***" in raised_message
    # Confirm this is really the sanitized path, not an accidentally-empty message.
    assert "Google Cloud Speech transcription failed" in raised_message


# ── 2. Speaker labels are built by the shared normalizer, not hand-rolled ────────────


@pytest.mark.parametrize("speaker_tag", [1, 2, 5])
def test_speaker_label_is_produced_by_the_shared_normalizer(tmp_path, speaker_tag):
    """The provider must genuinely CALL normalize_speaker_label(), not reimplement it.

    Regression test for the fix: google_provider.py used to hand-build
    ``f"SPEAKER_{(cur_tag - 1):02d}"`` instead of routing through
    ``self._normalize_speaker_label()`` like every other provider in this package (see
    app/services/asr/CLAUDE.md). That made the single-source-of-truth guarantee the shared
    helper is supposed to provide false for Google alone. This spies on
    ``app.services.asr.base.normalize_speaker_label`` — the module-level function
    ``_normalize_speaker_label`` delegates to — to prove the call really reaches it, with
    the correct already-0-indexed argument, in addition to checking the output value as a
    behavior regression guard.
    """
    response = speech.LongRunningRecognizeResponse(
        results=[
            speech.SpeechRecognitionResult(
                alternatives=[
                    speech.SpeechRecognitionAlternative(
                        transcript="hi there",
                        confidence=0.95,
                        words=[
                            _word("hi", 0.0, 0.3, speaker_tag),
                            _word("there", 0.3, 0.8, speaker_tag),
                        ],
                    )
                ]
            )
        ]
    )
    provider = _provider()
    audio_path = _write_audio(tmp_path)

    with (
        patch.object(speech, "SpeechClient", return_value=_mock_client(response)),
        patch(
            "app.services.asr.base.normalize_speaker_label",
            wraps=normalize_speaker_label,
        ) as spy,
    ):
        result = provider.transcribe(audio_path, _config(enable_diarization=True))

    # The shared helper was actually reached, with the already-0-indexed value.
    spy.assert_called_once_with(speaker_tag - 1)

    assert len(result.segments) == 1
    expected_label = normalize_speaker_label(str(speaker_tag - 1))
    assert result.segments[0].speaker == expected_label


def test_speaker_tag_zero_maps_to_no_speaker_label(tmp_path):
    """speaker_tag == 0 is Google's "unknown" sentinel and must NOT normalize to SPEAKER_00.

    normalize_speaker_label("−1") would be nonsensical, and the provider's own comment
    (L158) documents 0 as "unknown / no tag assigned" — it must produce speaker=None, not
    fall through to the shared normalizer at all.
    """
    response = speech.LongRunningRecognizeResponse(
        results=[
            speech.SpeechRecognitionResult(
                alternatives=[
                    speech.SpeechRecognitionAlternative(
                        transcript="untagged",
                        confidence=0.9,
                        words=[_word("untagged", 0.0, 0.4, 0)],
                    )
                ]
            )
        ]
    )
    provider = _provider()
    audio_path = _write_audio(tmp_path)

    with patch.object(speech, "SpeechClient", return_value=_mock_client(response)):
        result = provider.transcribe(audio_path, _config(enable_diarization=True))

    assert len(result.segments) == 1
    assert result.segments[0].speaker is None
    # has_speakers must correctly reflect that no real label was assigned.
    assert result.has_speakers is False


# ── 3. validate_connection() makes no network call ────────────────────────────────────


def test_validate_connection_is_construction_only_and_a_bad_credential_still_validates():
    """Pin the documented false-positive: azure/google validate_connection() never calls
    the network, so a bad or fake credential still reports success.

    app/services/asr/CLAUDE.md: "azure and google validate_connection() make no network
    call — a bad credential still 'validates'." SpeechClient() is instantiated and nothing
    else is called on it — no .recognize(), no .list_*(), nothing that would touch the wire.
    """
    fake_client = MagicMock()
    with patch.object(speech, "SpeechClient", return_value=fake_client) as ctor:
        provider = GoogleASRProvider(credentials_file=None)
        ok, message, _ms = provider.validate_connection()

    assert ok is True
    assert message == "Google Cloud Speech validated"
    ctor.assert_called_once_with()
    # No method beyond construction was ever invoked on the "credential".
    assert fake_client.method_calls == []


# ── 4. Empty word list loses real segment timing ──────────────────────────────────────


def test_empty_word_list_with_nonempty_transcript_defaults_timing_to_zero(tmp_path):
    """A non-diarized alternative can have transcript text but an empty words[] (e.g. a
    provider quirk or a very short utterance). The provider currently falls back to
    start=end=0.0 rather than any real timing — pin this as a known limitation.
    """
    response = speech.LongRunningRecognizeResponse(
        results=[
            speech.SpeechRecognitionResult(
                alternatives=[
                    speech.SpeechRecognitionAlternative(
                        transcript="just text, no word timings",
                        confidence=0.8,
                        words=[],
                    )
                ]
            )
        ]
    )
    provider = _provider()
    audio_path = _write_audio(tmp_path)

    with patch.object(speech, "SpeechClient", return_value=_mock_client(response)):
        result = provider.transcribe(audio_path, _config(enable_diarization=False))

    assert len(result.segments) == 1
    seg = result.segments[0]
    assert seg.text == "just text, no word timings"
    assert seg.words == []
    assert seg.start == 0.0
    assert seg.end == 0.0


# ── 5. Whole file read into memory before sending ─────────────────────────────────────


def test_entire_audio_file_is_read_into_memory_before_being_sent(tmp_path):
    """transcribe() does `with open(audio_path, "rb") as f: audio_content = f.read()`
    and hands the full bytes to RecognitionAudio(content=...) rather than streaming.
    This is a memory-usage regression guard: a large file is fully materialized in RAM.
    """
    payload = b"RIFF" + bytes(range(256)) * 40  # 10,244 bytes, deterministic content
    audio_path = _write_audio(tmp_path, content=payload)

    response = speech.LongRunningRecognizeResponse(results=[])
    client = _mock_client(response)
    provider = _provider()

    with patch.object(speech, "SpeechClient", return_value=client):
        provider.transcribe(audio_path, _config(enable_diarization=False))

    assert client.long_running_recognize.call_count == 1
    _, call_kwargs = client.long_running_recognize.call_args
    sent_audio = call_kwargs["audio"]
    assert sent_audio.content == payload
    assert len(sent_audio.content) == len(payload)
