"""Characterization tests for ``app/services/asr/azure_provider.py`` (network-free).

The entire ``azure.cognitiveservices.speech`` SDK is faked here — a real
``SpeechRecognizer``/``ConversationTranscriber`` opens a network stream to Azure the
instant ``start_continuous_recognition()``/``start_transcribing_async()`` is called, so no
test may construct a real one. ``_transcribe_without_diarization`` /
``_transcribe_with_diarization`` already take ``sdk`` as a parameter, so most tests pass a
fake module object straight in; the two callers that do ``import
azure.cognitiveservices.speech as sdk`` internally (``validate_connection`` and the public
``transcribe`` entrypoint) get the fake installed into ``sys.modules`` instead.

What is pinned here, in order:

1. **Open defect** (L227, L327): ``done.wait(timeout=7200)`` returns a bool — True if the
   ``done`` event was set before the timeout, False if it timed out — and that return value
   is discarded on both the diarization and non-diarization paths. Execution proceeds to
   ``stop_continuous_recognition()``/``stop_transcribing_async()`` regardless, then returns
   whatever partial segments were collected, with NO timeout error ever raised. A genuine
   2-hour Azure stall today returns a silently truncated transcript that looks complete.
2. ``validate_connection()`` (L66-83) only constructs ``sdk.SpeechConfig(...)`` — the Azure
   SDK docs describe that constructor as pure local configuration state with no auth
   round-trip; the actual auth check happens only when a recognizer starts. So an
   obviously-fake key still reports success — the false positive this package's own
   CLAUDE.md documents ("azure and google validate_connection() make no network call — a
   bad credential still validates").
3. ``_on_recognized``'s JSON-parse-failure fallback (L195-205) has no ``except ... as`` log
   call at all — a malformed ``result.json`` degrades to a text-only segment (no words, no
   confidence) with zero log output, silently.
4. Speaker labels flow ``speaker_id`` (e.g. ``"Guest-1"``) straight into
   ``self._normalize_speaker_label(...)`` with no manual offset applied first — unlike
   ``google_provider.py``, which must subtract 1 before calling the same shared function.
   Confirmed both directly and through ``_transcribe_with_diarization``.
5. **Recognizer selection** (L125-133, verified by reading this file, not just its module
   docstring): ``config.enable_diarization=True`` builds ``sdk.transcription.
   ConversationTranscriber``; ``False`` builds ``sdk.SpeechRecognizer``. Per Microsoft's
   Speech SDK docs, ``ConversationTranscriber`` is the only class in the SDK whose result
   objects carry a ``speaker_id`` — ``SpeechRecognizer`` has no diarization parameter at all
   and its results never carry one, so using it for diarization would silently produce a
   transcript with no speakers. That matches this file's own module docstring (L5-14).
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import types

import pytest

from app.services.asr.azure_provider import AzureASRProvider
from app.services.asr.types import ASRConfig

# ---------------------------------------------------------------------------
# Fake azure.cognitiveservices.speech SDK — no network, no real recognizer.
# ---------------------------------------------------------------------------


class _FakeResultReason:
    RecognizedSpeech = "RecognizedSpeech"
    NoMatch = "NoMatch"


class _FakeCancellationReason:
    Error = "Error"
    EndOfStream = "EndOfStream"


class _FakeOutputFormat:
    Detailed = "Detailed"


class _FakeSpeechConfig:
    """Mirrors the real SDK's ctor: records args, performs no I/O."""

    def __init__(self, subscription=None, region=None):
        self.subscription = subscription
        self.region = region
        self.speech_recognition_language = None
        self.output_format = None


class _FakeAudioConfig:
    def __init__(self, filename=None):
        self.filename = filename


class _FakeEventSignal:
    """Stand-in for the SDK's ``connect()``-based event dispatcher."""

    def __init__(self):
        self._callbacks: list = []

    def connect(self, callback):
        self._callbacks.append(callback)

    def fire(self, evt):
        for cb in self._callbacks:
            cb(evt)


class _FakeAsyncHandle:
    """Stand-in for the SDK's ``ResultFuture`` (``.start_transcribing_async().get()``)."""

    def get(self):
        return None


def _recognizer_factory(queued_events=(), fire_session_stopped=True, fire_canceled=None):
    """Build a fresh fake ``SpeechRecognizer`` class with its own ``instances`` list."""

    class _Recognizer:
        instances: list = []

        def __init__(self, speech_config=None, audio_config=None):
            self.speech_config = speech_config
            self.audio_config = audio_config
            self.recognized = _FakeEventSignal()
            self.session_stopped = _FakeEventSignal()
            self.canceled = _FakeEventSignal()
            self.stopped_called = False
            type(self).instances.append(self)

        def start_continuous_recognition(self):
            for evt in queued_events:
                self.recognized.fire(evt)
            if fire_canceled is not None:
                self.canceled.fire(fire_canceled)
            elif fire_session_stopped:
                self.session_stopped.fire(object())

        def stop_continuous_recognition(self):
            self.stopped_called = True

    return _Recognizer


def _transcriber_factory(queued_events=(), fire_session_stopped=True, fire_canceled=None):
    """Build a fresh fake ``ConversationTranscriber`` class with its own ``instances`` list."""

    class _Transcriber:
        instances: list = []

        def __init__(self, speech_config=None, audio_config=None):
            self.speech_config = speech_config
            self.audio_config = audio_config
            self.transcribed = _FakeEventSignal()
            self.session_stopped = _FakeEventSignal()
            self.canceled = _FakeEventSignal()
            self.stopped_called = False
            type(self).instances.append(self)

        def start_transcribing_async(self):
            for evt in queued_events:
                self.transcribed.fire(evt)
            if fire_canceled is not None:
                self.canceled.fire(fire_canceled)
            elif fire_session_stopped:
                self.session_stopped.fire(object())
            return _FakeAsyncHandle()

        def stop_transcribing_async(self):
            self.stopped_called = True
            return _FakeAsyncHandle()

    return _Transcriber


def _make_fake_sdk(recognizer_cls=None, transcriber_cls=None):
    sdk = types.SimpleNamespace()
    sdk.ResultReason = _FakeResultReason
    sdk.CancellationReason = _FakeCancellationReason
    sdk.OutputFormat = _FakeOutputFormat
    sdk.SpeechConfig = _FakeSpeechConfig
    sdk.audio = types.SimpleNamespace(AudioConfig=_FakeAudioConfig)
    sdk.SpeechRecognizer = recognizer_cls or _recognizer_factory()
    sdk.transcription = types.SimpleNamespace(
        ConversationTranscriber=transcriber_cls or _transcriber_factory()
    )
    return sdk


def _install_fake_sdk_module(monkeypatch, fake_sdk):
    """Patch ``import azure.cognitiveservices.speech as sdk`` to resolve to *fake_sdk*."""
    fake_cognitiveservices_pkg = types.SimpleNamespace(speech=fake_sdk)
    fake_azure_pkg = types.SimpleNamespace(cognitiveservices=fake_cognitiveservices_pkg)
    monkeypatch.setitem(sys.modules, "azure", fake_azure_pkg)
    monkeypatch.setitem(sys.modules, "azure.cognitiveservices", fake_cognitiveservices_pkg)
    monkeypatch.setitem(sys.modules, "azure.cognitiveservices.speech", fake_sdk)


def _recognized_event(text="hello", offset=0, duration=10_000_000, confidence=0.95, json_str=None):
    if json_str is None:
        json_str = json.dumps({"NBest": [{"Confidence": confidence, "Words": []}]})
    result = types.SimpleNamespace(
        reason=_FakeResultReason.RecognizedSpeech,
        json=json_str,
        offset=offset,
        duration=duration,
        text=text,
    )
    return types.SimpleNamespace(result=result)


def _transcribed_event(text="hi", offset=0, duration=10_000_000, confidence=0.9, speaker_id=None):
    json_str = json.dumps({"NBest": [{"Confidence": confidence, "Words": []}]})
    result = types.SimpleNamespace(
        reason=_FakeResultReason.RecognizedSpeech,
        json=json_str,
        offset=offset,
        duration=duration,
        text=text,
        speaker_id=speaker_id,
    )
    return types.SimpleNamespace(result=result)


def _canceled_event(error_code="ConnectionFailure", error_details="boom"):
    return types.SimpleNamespace(
        reason=_FakeCancellationReason.Error, error_code=error_code, error_details=error_details
    )


def _provider(api_key="fake-key-not-real"):
    return AzureASRProvider(api_key=api_key, region="eastus")


# ---------------------------------------------------------------------------
# 1. Discarded done.wait() timeout bool — open defect
# ---------------------------------------------------------------------------


def test_timed_out_wait_is_discarded_and_partial_transcript_returned_no_diarization(monkeypatch):
    """L227: ``done.wait(timeout=7200)`` returning False (a real timeout, no
    ``canceled``/``session_stopped`` ever fired) is never inspected. Pins today's WRONG
    behaviour: the function proceeds to ``stop_continuous_recognition()`` and returns the
    one partial segment collected before the timeout, with NO exception raised. Once fixed,
    this should assert a raised timeout error instead.
    """
    monkeypatch.setattr(threading.Event, "wait", lambda self, timeout=None: False)
    evt = _recognized_event(text="partial before the timeout")
    recognizer_cls = _recognizer_factory(queued_events=[evt], fire_session_stopped=False)
    fake_sdk = _make_fake_sdk(recognizer_cls=recognizer_cls)

    segments = _provider()._transcribe_without_diarization(
        fake_sdk, _FakeSpeechConfig(), _FakeAudioConfig(), "file.wav", None
    )

    assert len(segments) == 1
    assert segments[0].text == "partial before the timeout"
    # stop_continuous_recognition() still runs despite the timeout never being noticed.
    assert recognizer_cls.instances[0].stopped_called is True


def test_timed_out_wait_is_discarded_and_partial_transcript_returned_diarization(monkeypatch):
    """Same defect at L327, ``_transcribe_with_diarization``."""
    monkeypatch.setattr(threading.Event, "wait", lambda self, timeout=None: False)
    evt = _transcribed_event(text="partial diarized text", speaker_id="Guest-1")
    transcriber_cls = _transcriber_factory(queued_events=[evt], fire_session_stopped=False)
    fake_sdk = _make_fake_sdk(transcriber_cls=transcriber_cls)

    segments, has_speakers = _provider()._transcribe_with_diarization(
        fake_sdk,
        _FakeSpeechConfig(),
        _FakeAudioConfig(),
        "file.wav",
        ASRConfig(enable_diarization=True),
        None,
    )

    assert len(segments) == 1
    assert segments[0].text == "partial diarized text"
    assert has_speakers is True
    assert transcriber_cls.instances[0].stopped_called is True


# ---------------------------------------------------------------------------
# 2. validate_connection() false positive
# ---------------------------------------------------------------------------


def test_validate_connection_reports_success_for_an_obviously_fake_key(monkeypatch):
    fake_sdk = _make_fake_sdk()
    _install_fake_sdk_module(monkeypatch, fake_sdk)

    success, message, elapsed_ms = _provider(
        api_key="not-a-real-azure-key-000000"
    ).validate_connection()

    assert success is True
    assert "eastus" in message
    assert elapsed_ms >= 0.0


def test_validate_connection_only_constructs_speechconfig_no_recognizer_touched(monkeypatch):
    """The SDK call surface is exactly one constructor — nothing that could start a stream."""
    recognizer_cls = _recognizer_factory()
    transcriber_cls = _transcriber_factory()
    fake_sdk = _make_fake_sdk(recognizer_cls=recognizer_cls, transcriber_cls=transcriber_cls)
    _install_fake_sdk_module(monkeypatch, fake_sdk)

    _provider().validate_connection()

    assert recognizer_cls.instances == []
    assert transcriber_cls.instances == []


# ---------------------------------------------------------------------------
# 3. Silent JSON-parse-failure fallback in _on_recognized
# ---------------------------------------------------------------------------


def test_malformed_result_json_falls_back_to_a_bare_segment_with_no_log(caplog):
    caplog.set_level(logging.DEBUG)
    evt = _recognized_event(
        text="raw text survives", offset=50_000_000, duration=20_000_000, json_str="{not valid"
    )
    recognizer_cls = _recognizer_factory(queued_events=[evt])
    fake_sdk = _make_fake_sdk(recognizer_cls=recognizer_cls)

    segments = _provider()._transcribe_without_diarization(
        fake_sdk, _FakeSpeechConfig(), _FakeAudioConfig(), "file.wav", None
    )

    assert len(segments) == 1
    seg = segments[0]
    assert seg.text == "raw text survives"
    assert seg.words == []
    assert seg.confidence is None
    assert seg.start == pytest.approx(5.0)
    assert seg.end == pytest.approx(7.0)
    # The except branch (L195-205) has no logger call — degradation is silent.
    assert caplog.records == []


# ---------------------------------------------------------------------------
# 4. Speaker label normalization — no manual offset before normalize_speaker_label
# ---------------------------------------------------------------------------


def test_guest_labels_normalize_via_the_shared_1_indexed_regex_directly():
    provider = _provider()
    assert provider._normalize_speaker_label("Guest-1") == "SPEAKER_00"
    assert provider._normalize_speaker_label("Guest-2") == "SPEAKER_01"


def test_diarization_transcribe_passes_raw_speaker_id_through_unmodified():
    events = [
        _transcribed_event(text="first speaker", speaker_id="Guest-1"),
        _transcribed_event(text="second speaker", offset=20_000_000, speaker_id="Guest-2"),
    ]
    transcriber_cls = _transcriber_factory(queued_events=events)
    fake_sdk = _make_fake_sdk(transcriber_cls=transcriber_cls)

    segments, has_speakers = _provider()._transcribe_with_diarization(
        fake_sdk,
        _FakeSpeechConfig(),
        _FakeAudioConfig(),
        "file.wav",
        ASRConfig(enable_diarization=True),
        None,
    )

    assert len(segments) == 2
    assert segments[0].speaker == "SPEAKER_00"
    assert segments[1].speaker == "SPEAKER_01"
    assert has_speakers is True


# ---------------------------------------------------------------------------
# 5. Recognizer selection: ConversationTranscriber vs SpeechRecognizer
# ---------------------------------------------------------------------------


def test_transcribe_dispatches_to_conversationtranscriber_when_diarization_enabled(
    monkeypatch, tmp_path
):
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(b"not-real-audio-bytes")
    fake_sdk = _make_fake_sdk()
    _install_fake_sdk_module(monkeypatch, fake_sdk)

    result = _provider().transcribe(str(audio_path), ASRConfig(enable_diarization=True))

    assert len(fake_sdk.transcription.ConversationTranscriber.instances) == 1
    assert fake_sdk.SpeechRecognizer.instances == []
    assert result.provider_name == "azure"


def test_transcribe_dispatches_to_speechrecognizer_when_diarization_disabled(monkeypatch, tmp_path):
    audio_path = tmp_path / "sample.wav"
    audio_path.write_bytes(b"not-real-audio-bytes")
    fake_sdk = _make_fake_sdk()
    _install_fake_sdk_module(monkeypatch, fake_sdk)

    result = _provider().transcribe(str(audio_path), ASRConfig(enable_diarization=False))

    assert len(fake_sdk.SpeechRecognizer.instances) == 1
    assert fake_sdk.transcription.ConversationTranscriber.instances == []
    assert result.has_speakers is False


# ---------------------------------------------------------------------------
# Bonus: cancellation IS handled correctly (contrast with the timeout defect above)
# ---------------------------------------------------------------------------


def test_cancellation_error_raises_a_sanitized_runtime_error():
    recognizer_cls = _recognizer_factory(
        fire_canceled=_canceled_event(error_details="key=sk-secret123")
    )
    fake_sdk = _make_fake_sdk(recognizer_cls=recognizer_cls)

    with pytest.raises(RuntimeError) as exc_info:
        _provider(api_key="sk-secret123")._transcribe_without_diarization(
            fake_sdk, _FakeSpeechConfig(), _FakeAudioConfig(), "file.wav", None
        )

    assert "sk-secret123" not in str(exc_info.value)
    assert "***" in str(exc_info.value)
