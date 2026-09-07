"""Characterization tests for ``app/services/asr/assemblyai_provider.py``.

Network-free: the real ``assemblyai`` SDK is importable in this venv (its objects are plain
pydantic models with no I/O of their own), so tests patch only ``assemblyai.Transcriber`` (the
network-calling class) and ``requests.get`` (``validate_connection``'s lightweight probe).
Response shapes below follow the documented AssemblyAI schema: ``TranscriptionConfig`` takes a
``speech_models`` list (not the deprecated singular ``speech_model``), utterances carry 0-indexed
single-letter ``speaker`` values ("A", "B", ...), and word/utterance timestamps are milliseconds.

What is pinned here, in order:

1. **A fix in ``validate_connection()``**: it used to only treat HTTP 401 as failure, so any
   other non-200 status (500, a proxy error, ...) fell through to an unconditional
   ``return True, "AssemblyAI connection successful"`` — a false positive. It now treats any
   non-2xx status as a failure. ``test_validate_connection_treats_any_non_2xx_status_as_failure``
   is parametrized over 401 (the original, still-correct special case), 500, and 403 (an
   arbitrary other non-2xx code) and asserts all three report failure.
2. **``_model_map`` still maps ``"slam-1"`` and ``"nano"``** even though the module's own comment
   says both were rejected live by the API. ``test_model_map_still_contains_entries_flagged_
   stale_in_the_docstring`` is a regression guard proving those entries have not been silently
   cleaned up yet — remove this test (and the entries) once that is confirmed genuinely dead.
3. Speaker normalization of AssemblyAI's single-letter utterance labels through the real
   diarization branch (``"A"`` -> ``SPEAKER_00``, ``"B"`` -> ``SPEAKER_01``).
4. The no-diarization word-grouping loop force-flushes a trailing partial chunk on the last word
   rather than dropping it — a positive regression guard, not a bug.
5. ``config.vocabulary[:1000]`` truncates ``word_boost`` by **list length only**. AssemblyAI's
   documented cap is likewise a count ("up to 1,000 unique keywords/phrases"), so the slice value
   itself is not wrong — but nothing in this code bounds the *character* size of an individual
   term or the aggregate payload, so 1,000 very long terms sail through unmodified. That gap is
   what ``test_vocabulary_truncation_is_by_count_not_by_character_size`` documents.

Following the characterization-test convention of ``tests/unit/test_transcription_storage.py``
and the network-free provider pattern of ``tests/unit/test_pyannote_provider.py``.
"""

from __future__ import annotations

import types
from unittest.mock import MagicMock
from unittest.mock import patch

import assemblyai as aai
import pytest

from app.services.asr.assemblyai_provider import AssemblyAIProvider
from app.services.asr.types import ASRConfig


def _provider(model_name: str = "universal") -> AssemblyAIProvider:
    return AssemblyAIProvider(api_key="test-key-12345", model_name=model_name)


def _fake_word(text: str, start_ms: float, end_ms: float, confidence: float = 0.9):
    return types.SimpleNamespace(text=text, start=start_ms, end=end_ms, confidence=confidence)


def _fake_utterance(text: str, start_ms: float, end_ms: float, speaker: str, words=None):
    return types.SimpleNamespace(
        text=text,
        start=start_ms,
        end=end_ms,
        speaker=speaker,
        confidence=0.95,
        words=words or [],
    )


def _fake_transcript(
    status=None, error=None, utterances=None, words=None, text="", language_code="en"
):
    return types.SimpleNamespace(
        status=status or aai.TranscriptStatus.completed,
        error=error,
        utterances=utterances,
        words=words,
        text=text,
        language_code=language_code,
    )


def _run_transcribe(tmp_path, config: ASRConfig, transcript, model_name: str = "universal"):
    """Run ``transcribe()`` against a mocked ``aai.Transcriber``.

    Returns ``(result, call_kwargs)`` where ``call_kwargs`` is the keyword arguments the
    provider passed to ``Transcriber().transcribe(...)`` — captured into a local variable so
    later assertions read real values (``cfg.speech_models``, ``cfg.word_boost``, ...) rather
    than mock plumbing directly.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"RIFF....WAVEfmt ")
    provider = AssemblyAIProvider(api_key="test-key-12345", model_name=model_name)
    with patch("assemblyai.Transcriber") as mock_cls:
        mock_cls.return_value.transcribe.return_value = transcript
        result = provider.transcribe(str(audio_path), config)
        call = mock_cls.return_value.transcribe.call_args
    call_kwargs = call.kwargs
    return result, call_kwargs


# ── 1. validate_connection() treats any non-2xx status as failure (fixed bug) ──────────


@pytest.mark.parametrize("status_code", [401, 500, 403])
def test_validate_connection_treats_any_non_2xx_status_as_failure(status_code):
    """FIXED BUG: any non-2xx status (not just 401) is now reported as failure.

    ``validate_connection`` used to branch only on ``status_code == 401``; every other bad
    status code — including a bare 500 with no auth problem at all — fell through to an
    unconditional success return. It now checks the full status range, so 401 (the original
    special-cased auth failure), 500 (a server error), and 403 (an arbitrary other non-2xx
    code) all correctly report failure.
    """
    resp = MagicMock(status_code=status_code)
    with patch("requests.get", return_value=resp):
        success, message, _ms = _provider().validate_connection()
    assert success is False
    assert str(status_code) in message


def test_validate_connection_200_is_reported_as_success():
    """Control for the fix above: a genuine 200 still reports success."""
    resp = MagicMock(status_code=200)
    with patch("requests.get", return_value=resp):
        success, message, _ms = _provider().validate_connection()
    assert success is True
    assert message == "AssemblyAI connection successful"


def test_validate_connection_network_error_is_still_caught():
    """Control for the fix above: a real network failure (no status code at all) IS handled."""
    with patch("requests.get", side_effect=ConnectionError("connection refused")):
        success, message, _ms = _provider().validate_connection()
    assert success is False
    assert "connection refused" in message


# ── 2. _model_map still maps entries the module's own comment calls rejected ───────────


def test_model_map_still_contains_entries_flagged_stale_in_the_docstring(tmp_path):
    """Regression guard, not a fix: ``slam-1`` and ``nano`` are still mapped to themselves.

    The module comment above ``_model_map`` in ``assemblyai_provider.py`` says both were
    rejected live by the API, yet the mapping still routes them straight through instead of
    e.g. falling back to ``universal-3-pro``. This proves that has not been silently cleaned
    up. Delete this test (and the stale entries) once that is genuinely confirmed dead.
    """
    config = ASRConfig(enable_diarization=False, vocabulary=None)
    transcript = _fake_transcript(words=None, utterances=None, text="")

    _result_slam, call_kwargs_slam = _run_transcribe(
        tmp_path / "slam", config, transcript, model_name="slam-1"
    )
    _result_nano, call_kwargs_nano = _run_transcribe(
        tmp_path / "nano", config, transcript, model_name="nano"
    )

    cfg_slam = call_kwargs_slam["config"]
    cfg_nano = call_kwargs_nano["config"]
    assert cfg_slam.speech_models == ["slam-1"]
    assert cfg_nano.speech_models == ["nano"]


# ── 3. Speaker label normalization through the real diarization branch ─────────────────


def test_diarization_utterance_speakers_normalize_from_single_letters(tmp_path):
    utterances = [
        _fake_utterance("Hello there.", 0, 2000, speaker="A"),
        _fake_utterance("Hi, how are you?", 2100, 4000, speaker="B"),
    ]
    transcript = _fake_transcript(utterances=utterances, words=None, text="Hello there.")
    config = ASRConfig(enable_diarization=True, vocabulary=None)

    result, _call_kwargs = _run_transcribe(tmp_path, config, transcript)

    assert result.has_speakers is True
    assert len(result.segments) == 2
    speaker_a, speaker_b = result.segments[0].speaker, result.segments[1].speaker
    assert speaker_a == "SPEAKER_00"
    assert speaker_b == "SPEAKER_01"


# ── 4. No-diarization grouping force-flushes a trailing partial chunk ──────────────────


def test_trailing_partial_chunk_is_flushed_not_dropped(tmp_path):
    # w0, w1 close together -> one segment. Then a >0.5s silence gap forces a split.
    # w2, w3 are close together but never accumulate enough gap/duration on their own to
    # trigger a split — only the `is_last` check on w3 forces them out. If that check were
    # missing, w2/w3 would be silently discarded (chunk_words built up, never appended).
    words = [
        _fake_word("hello", 0, 1000),
        _fake_word("world", 1200, 2000),
        _fake_word("third", 6000, 6500),
        _fake_word("fourth", 6600, 7000),
    ]
    transcript = _fake_transcript(utterances=None, words=words, text="hello world third fourth")
    config = ASRConfig(enable_diarization=False, vocabulary=None)

    result, _call_kwargs = _run_transcribe(tmp_path, config, transcript)

    assert len(result.segments) == 2
    first, second = result.segments
    assert first.text == "hello world"
    assert second.text == "third fourth"
    # The trailing chunk's own start/end come from the un-dropped words.
    assert second.start == pytest.approx(6.0)
    assert second.end == pytest.approx(7.0)
    # No word was lost across the flush boundary.
    total_words = len(first.words) + len(second.words)
    assert total_words == len(words)


# ── 5. Vocabulary truncation is by list length, not character size ─────────────────────


def test_vocabulary_truncation_is_by_count_not_by_character_size(tmp_path):
    long_term = "x" * 1000  # one absurdly long "word"
    vocabulary = [long_term] * 1000  # already at AssemblyAI's documented 1,000-item cap
    config = ASRConfig(enable_diarization=False, vocabulary=vocabulary)
    transcript = _fake_transcript(utterances=None, words=None, text="")

    _result, call_kwargs = _run_transcribe(tmp_path, config, transcript)

    word_boost = call_kwargs["config"].word_boost
    total_chars = sum(len(term) for term in word_boost)
    # Today's gap: the code slices by count (`vocabulary[:1000]`) and applies no per-term or
    # aggregate character bound, so all 1,000 megabyte-scale terms are sent unmodified.
    assert len(word_boost) == 1000
    assert total_chars == 1_000_000


def test_vocabulary_truncation_does_cap_a_longer_list_by_count(tmp_path):
    """Control for the test above: the existing count-based slice DOES fire past 1,000 items."""
    vocabulary = [f"term{i}" for i in range(1500)]
    config = ASRConfig(enable_diarization=False, vocabulary=vocabulary)
    transcript = _fake_transcript(utterances=None, words=None, text="")

    _result, call_kwargs = _run_transcribe(tmp_path, config, transcript)

    word_boost = call_kwargs["config"].word_boost
    assert len(word_boost) == 1000
    assert word_boost == vocabulary[:1000]


# ── Rate-limit taxonomy (issue Lane 5) ────────────────────────────────────────


class _FakeSDKError(Exception):
    """Stand-in for a real vendor SDK exception carrying a `.status_code`.

    Real SDK exception classes declare this attribute themselves; plain ``Exception``
    does not, so it is declared here purely so the assignment below type-checks.
    """

    status_code: int


def test_429_status_code_is_classified_as_asr_rate_limited(tmp_path):
    from app.services.asr.errors import ASRRateLimitedError

    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"RIFF....WAVEfmt ")
    provider = _provider()

    with patch("assemblyai.Transcriber") as mock_cls:
        rate_limit_exc = _FakeSDKError("too many requests")
        rate_limit_exc.status_code = 429  # matches the SDK's exception shape
        mock_cls.return_value.transcribe.side_effect = rate_limit_exc

        with pytest.raises(ASRRateLimitedError) as excinfo:
            provider.transcribe(str(audio_path), ASRConfig())

    assert excinfo.value.provider == "assemblyai"


def test_non_429_error_stays_a_plain_runtime_error(tmp_path):
    """Negative control: an error with no rate-limit status must NOT be classified as
    retryable.
    """
    from app.services.asr.errors import ASRRateLimitedError

    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"RIFF....WAVEfmt ")
    provider = _provider()

    with patch("assemblyai.Transcriber") as mock_cls:
        server_error = _FakeSDKError("internal error")
        server_error.status_code = 500
        mock_cls.return_value.transcribe.side_effect = server_error

        with pytest.raises(RuntimeError, match="AssemblyAI transcription failed") as excinfo:
            provider.transcribe(str(audio_path), ASRConfig())

    assert not isinstance(excinfo.value, ASRRateLimitedError)
