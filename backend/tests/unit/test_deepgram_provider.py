"""Characterization tests for ``app/services/asr/deepgram_provider.py``.

Network-free: the ``deepgram`` SDK client is mocked entirely (``deepgram.DeepgramClient``,
patched at its import location since the provider imports it lazily inside each method).
Response objects are built as ``SimpleNamespace`` trees shaped like the deepgram-sdk v6
(Fern-generated) schema this module reads: ``response.results.channels[0].alternatives[0]``
for the flat transcript/words, ``response.results.utterances`` (a list of objects exposing
``speaker``/``transcript``/``start``/``end``/``words``/``confidence``) for the diarized path,
and ``response.results.channels[0].detected_language`` for language detection. Verified via
the SDK's own README examples (``client.listen.v1.media.transcribe_file(request=..., **kwargs)``
returning ``response.results.channels[0].alternatives[0].transcript``) rather than guessed.

Pins, in order:

1. ``_group_words_into_segments``'s exact ``>=`` split boundaries — a gap of 0.5s or a
   segment duration of 30.0s forces a split; 0.499s / 29.999s do not. The duration check
   compares the ALREADY-accumulated segment (``cur``) against the threshold before the next
   word is considered, not after — so the boundary word that pushes duration to exactly
   30.0s stays in the segment it completes, and the split happens on the word after it.
2. ``transcribe()`` reads the whole file in one ``f.read()`` call, never chunked — a
   memory-usage regression guard, not a fix.
3. ``validate_connection()`` calls ``client.manage.v1.projects.list()`` — the
   management-scoped endpoint, never the transcription endpoint — per the documented gotcha
   in ``app/services/asr/CLAUDE.md``.
4. Errors from both ``validate_connection()`` and ``transcribe()`` are scrubbed through
   ``sanitize_provider_error``/``self._sanitize_error`` before they reach a caller.
5. Deepgram's raw integer speaker labels (``0``, ``1``, ...) normalize 0-indexed through
   ``_normalize_speaker_label`` into ``SPEAKER_00``, ``SPEAKER_01``, ...

Following the characterization-test convention of ``tests/unit/test_transcription_storage.py``
and the network-free provider pattern of ``tests/unit/test_pyannote_provider.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import mock_open
from unittest.mock import patch

import pytest

from app.services.asr.deepgram_provider import DeepgramProvider
from app.services.asr.deepgram_provider import _group_words_into_segments
from app.services.asr.types import ASRConfig
from app.services.asr.types import ASRWord

# ── Helpers to build deepgram-sdk-shaped response objects ──────────────────────────────


def _dg_word(word: str, start: float, end: float, confidence: float = 0.99) -> SimpleNamespace:
    return SimpleNamespace(word=word, start=start, end=end, confidence=confidence)


def _dg_utterance(
    speaker: int | None,
    transcript: str,
    start: float,
    end: float,
    words: list,
    confidence: float = 0.95,
) -> SimpleNamespace:
    return SimpleNamespace(
        speaker=speaker,
        transcript=transcript,
        start=start,
        end=end,
        words=words,
        confidence=confidence,
    )


def _dg_response(
    utterances: list | None = None,
    alt_words: list | None = None,
    alt_transcript: str = "",
    detected_language: str | None = "en",
) -> SimpleNamespace:
    alt = SimpleNamespace(transcript=alt_transcript, words=alt_words or [])
    channel = SimpleNamespace(alternatives=[alt], detected_language=detected_language)
    results = SimpleNamespace(channels=[channel], utterances=utterances)
    return SimpleNamespace(results=results)


# ── 1. _group_words_into_segments boundary conditions ──────────────────────────────────


def test_gap_of_exactly_0_499_seconds_does_not_split():
    words = [ASRWord("a", 0.0, 1.0), ASRWord("b", 1.499, 2.0)]  # gap = 0.499
    segments = _group_words_into_segments(words, "fallback")
    assert len(segments) == 1
    assert [w.word for w in segments[0].words] == ["a", "b"]


def test_gap_of_exactly_0_5_seconds_splits():
    words = [ASRWord("a", 0.0, 1.0), ASRWord("b", 1.5, 2.0)]  # gap = 0.5
    segments = _group_words_into_segments(words, "fallback")
    assert len(segments) == 2
    assert [w.word for w in segments[0].words] == ["a"]
    assert [w.word for w in segments[1].words] == ["b"]
    assert segments[0].end == 1.0
    assert segments[1].start == 1.5


def test_duration_of_exactly_29_999_seconds_does_not_split():
    # duration of the accumulated segment [a, b] = 29.999 - 0.0 = 29.999, checked before c
    # is considered; gap b->c is well under 0.5s, so c merges too.
    words = [
        ASRWord("a", 0.0, 0.0),
        ASRWord("b", 0.001, 29.999),
        ASRWord("c", 30.099, 30.2),
    ]
    segments = _group_words_into_segments(words, "fallback")
    assert len(segments) == 1
    assert [w.word for w in segments[0].words] == ["a", "b", "c"]


def test_duration_of_exactly_30_0_seconds_splits():
    # duration of the accumulated segment [a, b] = 30.0 - 0.0 = 30.0 exactly, checked when
    # c is considered: the split happens on c, and b stays in the first segment.
    words = [
        ASRWord("a", 0.0, 0.0),
        ASRWord("b", 0.001, 30.0),
        ASRWord("c", 30.1, 30.2),
    ]
    segments = _group_words_into_segments(words, "fallback")
    assert len(segments) == 2
    assert [w.word for w in segments[0].words] == ["a", "b"]
    assert [w.word for w in segments[1].words] == ["c"]


def test_empty_word_list_falls_back_to_single_text_only_segment():
    segments = _group_words_into_segments([], "hello world")
    assert len(segments) == 1
    assert segments[0].text == "hello world"
    assert segments[0].start == 0.0
    assert segments[0].end == 0.0
    assert segments[0].words == []


# ── 2. transcribe() reads the whole file in one shot ────────────────────────────────────


def test_transcribe_reads_the_whole_file_in_one_call(tmp_path):
    """Memory-usage regression guard: pins the current one-shot ``f.read()``, not a fix."""
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"real bytes on disk so os.path.exists() passes")

    m_open = mock_open(read_data=b"fake-audio-bytes")
    response = _dg_response(alt_words=[_dg_word("hi", 0.0, 0.5)], alt_transcript="hi")

    with (
        patch("app.services.asr.deepgram_provider.open", m_open),
        patch("deepgram.DeepgramClient") as mock_client_cls,
    ):
        mock_client = mock_client_cls.return_value
        mock_client.listen.v1.media.transcribe_file.return_value = response

        provider = DeepgramProvider(api_key="dg_testkey")
        result = provider.transcribe(str(audio_path), ASRConfig(enable_diarization=False))

    # Called with no arguments — a single full read, not a chunked/sized read loop.
    m_open.return_value.read.assert_called_once_with()
    # And the read bytes actually reached the SDK call (real state, not just call bookkeeping).
    _, kwargs = mock_client.listen.v1.media.transcribe_file.call_args
    assert kwargs["request"] == b"fake-audio-bytes"
    assert result.segments[0].text == "hi"


# ── 3. validate_connection() uses the management-scoped endpoint ───────────────────────


def test_validate_connection_calls_manage_projects_list():
    provider = DeepgramProvider(api_key="dg_testkey123")

    with patch("deepgram.DeepgramClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.manage.v1.projects.list.return_value = SimpleNamespace(projects=[])

        ok, message, elapsed_ms = provider.validate_connection()

    assert ok is True
    assert message == "Deepgram connection successful"
    assert elapsed_ms >= 0.0
    mock_client.manage.v1.projects.list.assert_called_once_with()
    # Never the transcription endpoint — that would need a media-scoped, not a
    # management-scoped, key.
    assert not mock_client.listen.v1.media.transcribe_file.called


# ── 4. Errors are sanitized before they reach a caller ──────────────────────────────────


def test_validate_connection_scrubs_the_api_key_from_a_failure_message():
    secret_key = "dg_super_secret_abc123"
    provider = DeepgramProvider(api_key=secret_key)

    with patch("deepgram.DeepgramClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.manage.v1.projects.list.side_effect = Exception(
            f"401 Unauthorized: key {secret_key} is invalid"
        )

        ok, message, _elapsed_ms = provider.validate_connection()

    assert ok is False
    assert secret_key not in message
    assert "***" in message


def test_transcribe_scrubs_the_api_key_from_a_transcription_failure(tmp_path):
    secret_key = "dg_another_secret_key_999"
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"some audio bytes")

    with patch("deepgram.DeepgramClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.listen.v1.media.transcribe_file.side_effect = Exception(
            f"upstream rejected key {secret_key}"
        )

        provider = DeepgramProvider(api_key=secret_key)
        with pytest.raises(RuntimeError) as exc_info:
            provider.transcribe(str(audio_path), ASRConfig())

    assert secret_key not in str(exc_info.value)
    assert "***" in str(exc_info.value)


def test_transcribe_raises_file_not_found_before_touching_the_network(tmp_path):
    missing_path = str(tmp_path / "does-not-exist.wav")
    provider = DeepgramProvider(api_key="dg_key")

    with patch("deepgram.DeepgramClient") as mock_client_cls:
        with pytest.raises(FileNotFoundError):
            provider.transcribe(missing_path, ASRConfig())

    # No client was even constructed — the existence check happens first.
    mock_client_cls.assert_not_called()


# ── 5. Speaker labels normalize 0-indexed ────────────────────────────────────────────────


def test_transcribe_normalizes_integer_speaker_labels_0_indexed(tmp_path):
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"some audio bytes")

    utterances = [
        _dg_utterance(
            0, "hello there", 0.0, 1.0, [_dg_word("hello", 0.0, 0.5), _dg_word("there", 0.5, 1.0)]
        ),
        _dg_utterance(1, "hi", 1.2, 1.5, [_dg_word("hi", 1.2, 1.5)]),
    ]
    response = _dg_response(utterances=utterances, detected_language="en")

    with patch("deepgram.DeepgramClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.listen.v1.media.transcribe_file.return_value = response

        provider = DeepgramProvider(api_key="dg_key", model_name="nova-3")
        result = provider.transcribe(str(audio_path), ASRConfig(enable_diarization=True))

    assert result.has_speakers is True
    assert result.provider_name == "deepgram"
    assert result.model_name == "nova-3"
    assert result.language == "en"
    assert len(result.segments) == 2
    # Deepgram's raw "0"/"1" integers land on the 0-indexed base, matching the documented
    # convention in asr/CLAUDE.md (as opposed to Google's 1-indexed speaker_tag).
    assert result.segments[0].speaker == "SPEAKER_00"
    assert result.segments[1].speaker == "SPEAKER_01"
    assert result.segments[0].text == "hello there"
    assert [w.word for w in result.segments[0].words] == ["hello", "there"]


def test_transcribe_without_diarization_has_no_speakers_and_uses_word_grouping(tmp_path):
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"some audio bytes")

    # No utterances at all — exercises the alt.words fallback -> _group_words_into_segments.
    response = _dg_response(
        utterances=None,
        alt_words=[_dg_word("no", 0.0, 0.3), _dg_word("speakers", 0.3, 0.8)],
        alt_transcript="no speakers",
        detected_language=None,
    )

    with patch("deepgram.DeepgramClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.listen.v1.media.transcribe_file.return_value = response

        provider = DeepgramProvider(api_key="dg_key")
        result = provider.transcribe(
            str(audio_path), ASRConfig(enable_diarization=False, language="fr")
        )

    assert result.has_speakers is False
    assert len(result.segments) == 1
    assert result.segments[0].speaker is None
    assert [w.word for w in result.segments[0].words] == ["no", "speakers"]
    # detected_language was None on the channel -> falls back to config.language.
    assert result.language == "fr"
