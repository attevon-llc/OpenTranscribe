"""Characterization tests for ``app/services/asr/openai_provider.py``.

Network-free: the ``openai`` SDK's ``OpenAI`` class is patched at
``openai.OpenAI`` (the module the provider does its lazy ``from openai import
OpenAI`` against), so nothing here reaches api.openai.com. Response objects are
built with ``types.SimpleNamespace`` to mimic the SDK's attribute-style access
(``getattr(sd, "avg_logprob", None)`` etc.) without depending on the SDK's
actual pydantic model classes.

What is pinned here, in order:

1. **A real cross-file inconsistency between ``factory.py`` and this module.**
   ``factory.py``'s ``ASR_PROVIDER_CATALOG["openai"]["models"]`` lists a model
   entry ``{"id": "gpt-4o-transcribe-diarize", ..., "supports_diarization":
   True}`` (factory.py L227 for the ``id``, L232 for the
   ``supports_diarization`` flag). But this module's own module docstring
   (L7-11) says that model id "was fictional and has been removed," and
   ``supports_diarization()`` (L47-49) unconditionally returns ``False`` no
   matter what ``model_name`` the provider was constructed with. The catalog
   entry is therefore advertising a capability the provider can never deliver
   for that exact model id — an admin UI reading the catalog would show a
   "Diarization: yes" badge for a model this provider will silently ignore.
   ``test_supports_diarization_is_false_even_for_the_catalog_advertised_diarize_model``
   pins the provider side so a future factory.py edit that changes the catalog
   without touching the provider is caught immediately, and so a future
   provider edit that starts branching on ``model_name`` here is a deliberate,
   visible change to this test rather than a silent capability flip.

2. **The ``avg_logprob`` -> confidence conversion** (L149-156):
   ``confidence = clamp(exp(avg_logprob), 0.0, 1.0)``. Verified against the
   documented ``whisper-1`` ``verbose_json`` segment shape, which exposes
   ``avg_logprob`` per segment (a <= 0 log-probability).

3. **``gpt-4o-transcribe`` segments carry no ``avg_logprob``.** Per OpenAI's
   own API reference, ``gpt-4o-transcribe``/``gpt-4o-transcribe-diarize``
   reject ``response_format="verbose_json"`` entirely (only ``json``/``text``/
   ``diarized_json`` are accepted) — so in production this module's
   unconditional ``response_format="verbose_json"`` request (L129) for any
   non-``whisper-1`` model would raise at the SDK/HTTP layer before any
   segment is ever parsed, not silently degrade to ``confidence=None``. That
   is a second, more consequential inconsistency than the one pinned above,
   but it is out of this file's assigned scope (see the task's item 3, which
   asks only to pin the segment-parsing behavior once a segment-shaped
   response *is* received) and is reported separately rather than fixed here,
   per the "characterization tests only, no production changes" instruction.
   The test below therefore exercises the parsing logic in isolation, the way
   L149-156 would behave against a segment object OpenAI's own
   ``whisper-1``-only ``avg_logprob`` field is absent from.

4. **Language fallback** (L166): ``getattr(resp, "language", None) or
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

import pytest

from app.services.asr.openai_provider import OpenAIASRProvider
from app.services.asr.types import ASRConfig


def _provider(model_name: str = "gpt-4o-transcribe") -> OpenAIASRProvider:
    return OpenAIASRProvider(api_key="test-key", model_name=model_name)  # gitleaks:allow


# --- 1. factory.py catalog vs. provider capability -------------------------


def test_supports_diarization_is_false_even_for_the_catalog_advertised_diarize_model():
    """factory.py L227/L232 advertises diarization for this exact model id.

    ``ASR_PROVIDER_CATALOG["openai"]["models"]`` contains:

        {"id": "gpt-4o-transcribe-diarize", ..., "supports_diarization": True}

    (factory.py L227 for the id, L232 for the flag). This module's docstring
    says that id "was fictional and has been removed," and
    ``supports_diarization()`` never branches on ``model_name`` at all — it is
    a hardcoded ``return False``. Constructing the provider with the exact
    catalog model id and confirming ``supports_diarization()`` is still
    ``False`` proves the catalog entry cannot be trusted for this provider:
    something reading the catalog (e.g. the admin UI) would show a
    diarization-capable badge for a model this class can never diarize.
    """
    provider = _provider(model_name="gpt-4o-transcribe-diarize")
    assert provider.supports_diarization() is False


def test_supports_diarization_is_false_for_every_known_model_name():
    # Control: the flag is unconditionally False, not just for the fictional
    # diarize id — whisper-1 and gpt-4o-transcribe never claimed diarization.
    for model_name in ("whisper-1", "gpt-4o-transcribe", "gpt-4o-transcribe-diarize"):
        assert _provider(model_name).supports_diarization() is False


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


# --- 3. gpt-4o-transcribe segments have no avg_logprob ----------------------


def test_confidence_is_none_when_segment_has_no_avg_logprob(tmp_path):
    # gpt-4o-transcribe verbose_json-shaped segments (per this module's own
    # docstring) carry no avg_logprob field at all — SimpleNamespace without
    # the attribute reproduces "attribute genuinely absent", matching
    # getattr(sd, "avg_logprob", None) returning None rather than a
    # sentinel/exception.
    segment = SimpleNamespace(start=0.0, end=2.0, text="no logprob here")
    resp = SimpleNamespace(text="no logprob here", language="en", segments=[segment])

    fake_client = _fake_client(transcriptions_return=resp)
    provider = _provider("gpt-4o-transcribe")

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
