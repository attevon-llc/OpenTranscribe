"""Characterization tests for ``app/services/asr/openai_provider.py``.

Network-free: the ``openai`` SDK's ``OpenAI`` class is patched at
``openai.OpenAI`` (the module the provider does its lazy ``from openai import
OpenAI`` against), so nothing here reaches api.openai.com. Response objects are
built with ``types.SimpleNamespace`` to mimic the SDK's attribute-style access
(``getattr(sd, "avg_logprob", None)`` etc.) without depending on the SDK's
actual pydantic model classes.

What is pinned/verified here, in order:

1. **Fixed: ``gpt-4o-transcribe-diarize`` genuinely diarizes now.** This
   module's docstring used to (wrongly) claim the model id "was fictional and
   has been removed"; it is real, and ``factory.py``'s catalog entry
   advertising ``supports_diarization: True`` for it was correct all along —
   the provider code just never implemented it. Fixed: ``transcribe()`` now
   requests ``response_format="diarized_json"`` for this model specifically
   (the only format that returns speaker labels for it — ``verbose_json`` is
   explicitly rejected), parses each segment's ``speaker`` field
   (``"Speaker 1"``, ``"Speaker 2"``, ...) through the shared
   ``normalize_speaker_label``, and ``supports_diarization()`` now returns
   ``True`` only for this exact model id. Schema verified against OpenAI's
   public API reference and community bug reports, not a live call (no API
   key configured here) — worth a real-response smoke test once one is
   available.

2. **Fixed: the ``response_format="verbose_json"`` incompatibility.** Per
   OpenAI's API reference, ``gpt-4o-transcribe``/``gpt-4o-mini-transcribe``
   reject ``verbose_json`` outright (400 ``invalid_request_error``) — only
   ``json`` is supported, which has no ``segments``/``avg_logprob``. Fixed:
   the non-whisper-1, non-diarize branch now requests ``response_format="json"``.
   This was the practical bug: any real call to ``gpt-4o-transcribe`` would
   have failed at the SDK/HTTP layer before this module's segment-parsing
   code ever ran.

3. **The ``avg_logprob`` -> confidence conversion** (whisper-1 only):
   ``confidence = clamp(exp(avg_logprob), 0.0, 1.0)``. Verified against the
   documented ``whisper-1`` ``verbose_json`` segment shape, which exposes
   ``avg_logprob`` per segment (a <= 0 log-probability). ``gpt-4o-transcribe``
   (now correctly requesting the segment-less ``json`` format) never reaches
   this branch at all in production; it is exercised here in isolation to
   pin what happens if a segment-shaped response is ever received without an
   ``avg_logprob`` attribute (``confidence=None``, not a crash or a
   misleading default).

4. **Language fallback**: ``getattr(resp, "language", None) or
   config.language``. Pinned as today's (possibly misleading) behavior: when
   the SDK response omits ``language`` — which the ``translations`` endpoint's
   response does, since translation output is always English and OpenAI does
   not echo the detected source language back — the result silently reports
   the CONFIG's input language, not any language actually detected from the
   audio.

5. **The audio file handle is opened via ``with`` and closed even when the API
   call raises** — a positive regression guard, not a bug.

Docstring convention follows ``tests/unit/test_transcription_storage.py``.
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from unittest.mock import mock_open
from unittest.mock import patch

import httpx
import openai
import pytest

from app.services.asr.errors import ASRRateLimitedError
from app.services.asr.openai_provider import OpenAIASRProvider
from app.services.asr.types import ASRConfig


def _provider(model_name: str = "gpt-4o-transcribe") -> OpenAIASRProvider:
    return OpenAIASRProvider(api_key="test-key", model_name=model_name)  # gitleaks:allow


# --- 1/2. supports_diarization() + the request response_format per model ---


def test_supports_diarization_is_true_only_for_the_diarize_model():
    assert _provider("whisper-1").supports_diarization() is False
    assert _provider("gpt-4o-transcribe").supports_diarization() is False
    assert _provider("gpt-4o-transcribe-diarize").supports_diarization() is True


def test_gpt4o_transcribe_requests_json_not_verbose_json(tmp_path):
    # verbose_json is rejected outright by gpt-4o-transcribe; json is the only
    # supported format and carries no `segments`.
    resp = SimpleNamespace(text="hello", language="en")
    fake_client = _fake_client(transcriptions_return=resp)
    provider = _provider("gpt-4o-transcribe")

    with _patched_openai(fake_client), _patched_open():
        provider.transcribe(str(tmp_path / "fake-audio.wav"), ASRConfig(language="en"))

    fake_client.audio.transcriptions.create.assert_called_once()
    _, kwargs = fake_client.audio.transcriptions.create.call_args
    assert kwargs["response_format"] == "json"
    assert kwargs["model"] == "gpt-4o-transcribe"


def test_diarize_model_requests_diarized_json_and_parses_speaker_segments(tmp_path):
    seg_a = SimpleNamespace(start=0.0, end=1.5, text="hi there", speaker="Speaker 1")
    seg_b = SimpleNamespace(start=1.5, end=3.0, text="hello back", speaker="Speaker 2")
    resp = SimpleNamespace(text="hi there hello back", language="en", segments=[seg_a, seg_b])

    fake_client = _fake_client(transcriptions_return=resp)
    provider = _provider("gpt-4o-transcribe-diarize")

    with _patched_openai(fake_client), _patched_open():
        result = provider.transcribe(str(tmp_path / "fake-audio.wav"), ASRConfig(language="en"))

    fake_client.audio.transcriptions.create.assert_called_once()
    _, kwargs = fake_client.audio.transcriptions.create.call_args
    assert kwargs["response_format"] == "diarized_json"
    assert kwargs["model"] == "gpt-4o-transcribe-diarize"

    assert result.has_speakers is True
    # "Speaker 1"/"Speaker 2" are 1-indexed per OpenAI's documented schema —
    # normalize_speaker_label's "speaker N" (space-separated) branch handles it.
    assert [s.speaker for s in result.segments] == ["SPEAKER_00", "SPEAKER_01"]
    assert [s.text for s in result.segments] == ["hi there", "hello back"]


def test_diarize_model_with_no_speaker_field_has_speakers_false(tmp_path):
    # A segment-shaped response missing `speaker` entirely (e.g. a
    # non-diarize model that happened to carry segments) must not crash and
    # must not fabricate a speaker.
    segment = SimpleNamespace(start=0.0, end=1.0, text="solo")
    resp = SimpleNamespace(text="solo", language="en", segments=[segment])

    fake_client = _fake_client(transcriptions_return=resp)
    provider = _provider("gpt-4o-transcribe-diarize")

    with _patched_openai(fake_client), _patched_open():
        result = provider.transcribe(str(tmp_path / "fake-audio.wav"), ASRConfig(language="en"))

    assert result.has_speakers is False
    assert result.segments[0].speaker is None


# --- 2. avg_logprob -> confidence clamp -------------------------------------


def test_avg_logprob_converts_to_confidence_via_exp(tmp_path):
    avg_logprob = -0.1
    segment = SimpleNamespace(start=0.0, end=1.2, text="hello there", avg_logprob=avg_logprob)
    resp = SimpleNamespace(text="hello there", language="en", segments=[segment])

    fake_client = _fake_client(transcriptions_return=resp)
    provider = _provider("whisper-1")
    config = ASRConfig(language="en")

    with _patched_openai(fake_client), _patched_open():
        result = provider.transcribe(str(tmp_path / "fake-audio.wav"), config)

    confidence = result.segments[0].confidence
    assert confidence == pytest.approx(math.exp(avg_logprob))
    # exp() of a <= 0 log-probability is always in (0, 1], so the clamp's
    # lower bound never actually engages for a realistic avg_logprob — this
    # asserts the *typical* value, not the clamp boundary (see next test).
    assert confidence is not None
    assert 0.0 < confidence < 1.0


def test_confidence_is_clamped_to_one_when_exp_would_exceed_it(tmp_path):
    # avg_logprob is documented as always <= 0, but the clamp exists as a
    # defensive bound. A positive avg_logprob (never emitted by OpenAI, but
    # not rejected by this code) makes exp() > 1, exercising min(1.0, ...).
    segment = SimpleNamespace(start=0.0, end=1.0, text="x", avg_logprob=0.5)
    resp = SimpleNamespace(text="x", language="en", segments=[segment])

    fake_client = _fake_client(transcriptions_return=resp)
    provider = _provider("whisper-1")

    with _patched_openai(fake_client), _patched_open():
        result = provider.transcribe(str(tmp_path / "fake-audio.wav"), ASRConfig(language="en"))

    assert math.exp(0.5) > 1.0  # sanity: the input really would exceed 1.0 unclamped
    assert result.segments[0].confidence == 1.0


# --- 3. a segment with no avg_logprob at all --------------------------------


def test_confidence_is_none_when_segment_has_no_avg_logprob(tmp_path):
    # In production gpt-4o-transcribe now requests "json" (fixed above), which
    # has no `segments` at all — this combination can't happen for real
    # anymore. Exercised anyway, via the diarize model (whose diarized_json
    # segments genuinely have no avg_logprob field), to pin that a segment
    # missing the attribute produces confidence=None rather than a crash or a
    # misleading default. SimpleNamespace without the attribute reproduces
    # "attribute genuinely absent", matching getattr(sd, "avg_logprob", None).
    segment = SimpleNamespace(start=0.0, end=2.0, text="no logprob here", speaker="Speaker 1")
    resp = SimpleNamespace(text="no logprob here", language="en", segments=[segment])

    fake_client = _fake_client(transcriptions_return=resp)
    provider = _provider("gpt-4o-transcribe-diarize")

    with _patched_openai(fake_client), _patched_open():
        result = provider.transcribe(str(tmp_path / "fake-audio.wav"), ASRConfig(language="en"))

    assert result.segments[0].confidence is None
    # Not crashing and not defaulting to something misleading like 0.0 or 1.0.
    assert result.segments[0].text == "no logprob here"


# --- 4. language fallback on the translations endpoint ----------------------


def test_translation_response_missing_language_falls_back_to_config_language(tmp_path):
    # whisper-1 + translate_to_english routes through client.audio.translations
    # .create(...), whose response is always-English text and does not echo a
    # detected source language back. SimpleNamespace without a "language"
    # attribute reproduces that omission.
    resp = SimpleNamespace(text="hello world", segments=[])

    fake_client = _fake_client(translations_return=resp)
    provider = _provider("whisper-1")
    config = ASRConfig(language="es", translate_to_english=True)

    with _patched_openai(fake_client), _patched_open():
        result = provider.transcribe(str(tmp_path / "fake-audio.wav"), config)

    # Pinned as current behavior: this is the CONFIG's input language, not a
    # real detected language — the SDK response never told us one.
    assert result.language == "es"
    fake_client.audio.translations.create.assert_called_once()
    fake_client.audio.transcriptions.create.assert_not_called()


def test_transcription_response_with_language_does_not_use_the_fallback(tmp_path):
    # Control for the previous test: when the response DOES carry a language,
    # it wins over config.language rather than the fallback firing regardless.
    segment = SimpleNamespace(start=0.0, end=1.0, text="bonjour", avg_logprob=-0.2)
    resp = SimpleNamespace(text="bonjour", language="fr", segments=[segment])

    fake_client = _fake_client(transcriptions_return=resp)
    provider = _provider("whisper-1")
    config = ASRConfig(language="auto")

    with _patched_openai(fake_client), _patched_open():
        result = provider.transcribe(str(tmp_path / "fake-audio.wav"), config)

    assert result.language == "fr"


# --- 5. file handle lifecycle (positive regression guard) ------------------


def test_audio_file_handle_is_closed_even_when_the_api_call_raises(tmp_path):
    fake_client = _fake_client()
    fake_client.audio.transcriptions.create.side_effect = RuntimeError("upstream boom")
    provider = _provider("gpt-4o-transcribe")

    m_open = mock_open(read_data=b"not-real-audio-bytes")

    with (
        patch("app.services.asr.openai_provider.os.path.exists", return_value=True),
        patch("app.services.asr.openai_provider.os.path.getsize", return_value=1024),
        patch("builtins.open", m_open),
        _patched_openai(fake_client),
    ):
        with pytest.raises(RuntimeError, match="OpenAI transcription failed"):
            provider.transcribe(str(tmp_path / "fake-audio.wav"), ASRConfig(language="en"))

    handle = m_open.return_value
    handle.__enter__.assert_called_once()
    handle.__exit__.assert_called_once()


# --- rate-limit taxonomy (issue Lane 5) --------------------------------------


def _rate_limit_error(retry_after: str | None = "20") -> openai.RateLimitError:
    req = httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions")
    headers = {"Retry-After": retry_after} if retry_after else {}
    resp = httpx.Response(429, request=req, headers=headers)
    # openai's stub types `response` against its own vendored httpx re-export, which
    # mypy sees as a distinct (structurally identical) type from the real httpx.Response
    # constructed above — a real caller passes the SDK's own httpx.Response and never
    # hits this mismatch.
    return openai.RateLimitError("Rate limit reached", response=resp, body=None)  # type: ignore[arg-type]


def test_rate_limit_error_is_classified_as_asr_rate_limited(tmp_path):
    fake_client = _fake_client()
    fake_client.audio.transcriptions.create.side_effect = _rate_limit_error()
    provider = _provider("gpt-4o-transcribe")

    with _patched_open(), _patched_openai(fake_client):
        with pytest.raises(ASRRateLimitedError) as excinfo:
            provider.transcribe(str(tmp_path / "fake-audio.wav"), ASRConfig(language="en"))

    assert excinfo.value.provider == "openai"
    assert excinfo.value.retry_after == 20.0


def test_authentication_error_stays_a_plain_runtime_error(tmp_path):
    """Negative control: a 401 must NOT be classified as retryable — otherwise a test
    that only checked "some exception was rate-limit-typed" would pass even if this
    provider classified everything as retryable.
    """
    fake_client = _fake_client()
    req = httpx.Request("POST", "https://api.openai.com/v1/audio/transcriptions")
    resp = httpx.Response(401, request=req)
    fake_client.audio.transcriptions.create.side_effect = openai.AuthenticationError(
        "Invalid API key",
        response=resp,  # type: ignore[arg-type]  # see _rate_limit_error() above
        body=None,
    )
    provider = _provider("gpt-4o-transcribe")

    with _patched_open(), _patched_openai(fake_client):
        with pytest.raises(RuntimeError, match="OpenAI transcription failed") as excinfo:
            provider.transcribe(str(tmp_path / "fake-audio.wav"), ASRConfig(language="en"))

    assert not isinstance(excinfo.value, ASRRateLimitedError)


# --- shared helpers ----------------------------------------------------------


def _fake_client(transcriptions_return=None, translations_return=None):
    from unittest.mock import MagicMock

    client = MagicMock()
    if transcriptions_return is not None:
        client.audio.transcriptions.create.return_value = transcriptions_return
    if translations_return is not None:
        client.audio.translations.create.return_value = translations_return
    return client


def _patched_openai(fake_client):
    return patch("openai.OpenAI", return_value=fake_client)


def _patched_open():
    """Patch os.path checks and builtins.open so transcribe() can run against a fake path."""
    return _MultiPatch(
        patch("app.services.asr.openai_provider.os.path.exists", return_value=True),
        patch("app.services.asr.openai_provider.os.path.getsize", return_value=1024),
        patch("builtins.open", mock_open(read_data=b"not-real-audio-bytes")),
    )


class _MultiPatch:
    """Combine several ``unittest.mock.patch`` context managers under one ``with``."""

    def __init__(self, *patches):
        self._patches = patches

    def __enter__(self):
        for p in self._patches:
            p.__enter__()
        return self

    def __exit__(self, *exc_info):
        for p in reversed(self._patches):
            p.__exit__(*exc_info)
        return False
