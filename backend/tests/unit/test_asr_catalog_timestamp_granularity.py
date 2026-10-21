"""``timestamp_granularity`` in the ASR catalog matches what each PARSER produces (#1224).

The pipeline plan refuses speaker detection for a model whose text carries no times
(``asr_no_timing_for_speakers``) and warns for segment-only timing, both from this field.
So it is checked against the provider module's own parsing: each provider is fed the
fixture responses its unit tests already use (vendor-shaped, no network) and the parsed
result is classified:

- ``word``: every word carries a time span, and there is at least one word;
- ``segment``: no words, but every segment spans ``end > start``;
- ``none``: neither (``gpt-4o-transcribe``'s single ``0.0/0.0`` segment).

A catalog that says ``word`` for a parser that yields no word times fails here. Azure is
declared ``segment`` and fed a response without ``Words``: word-level timestamps are never
requested from the SDK, so a word-timed Azure response is unproven (plan A10).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.asr.factory import ASR_PROVIDER_CATALOG
from app.services.asr.factory import ASRProviderFactory
from app.services.asr.types import ASRConfig
from app.services.asr.types import ASRSegment
from app.services.asr.types import ASRWord


def _classify(segments: list[ASRSegment]) -> str:
    words = [w for s in segments for w in s.words]
    if words and all(w.end > w.start for w in words):
        return "word"
    if segments and not words and all(s.end > s.start for s in segments):
        return "segment"
    return "none"


# --- one runner per provider, driving its real parser -------------------------------


def _deepgram(tmp_path, model: str, monkeypatch) -> list[ASRSegment]:
    from app.services.asr.deepgram_provider import DeepgramProvider
    from tests.unit.test_deepgram_provider import _dg_response
    from tests.unit.test_deepgram_provider import _dg_utterance
    from tests.unit.test_deepgram_provider import _dg_word

    audio = tmp_path / "a.wav"
    audio.write_bytes(b"audio")
    words = [_dg_word("hello", 0.0, 0.5), _dg_word("there", 0.5, 1.0)]
    response = _dg_response(utterances=[_dg_utterance(0, "hello there", 0.0, 1.0, words)])
    with patch("deepgram.DeepgramClient") as client_cls:
        client_cls.return_value.listen.v1.media.transcribe_file.return_value = response
        result = DeepgramProvider(api_key="k", model_name=model).transcribe(
            str(audio), ASRConfig(enable_diarization=True)
        )
    return result.segments


def _assemblyai(tmp_path, model: str, monkeypatch) -> list[ASRSegment]:
    from tests.unit.test_assemblyai_provider import _fake_transcript
    from tests.unit.test_assemblyai_provider import _fake_utterance
    from tests.unit.test_assemblyai_provider import _fake_word
    from tests.unit.test_assemblyai_provider import _run_transcribe

    words = [_fake_word("hello", 0, 400), _fake_word("there", 450, 900)]
    transcript = _fake_transcript(utterances=[_fake_utterance("hello there", 0, 900, "A", words)])
    result, _ = _run_transcribe(
        tmp_path, ASRConfig(enable_diarization=True, vocabulary=None), transcript, model
    )
    segments: list[ASRSegment] = result.segments
    return segments


def _openai(tmp_path, model: str, monkeypatch) -> list[ASRSegment]:
    from app.services.asr.openai_provider import OpenAIASRProvider
    from tests.unit.test_openai_provider import _fake_client
    from tests.unit.test_openai_provider import _patched_open
    from tests.unit.test_openai_provider import _patched_openai

    segment = SimpleNamespace(start=0.0, end=1.5, text="hi there", speaker="Speaker 1")
    responses = {
        # verbose_json: segments with times, no words requested
        "whisper-1": SimpleNamespace(text="hi there", language="en", segments=[segment]),
        # diarized_json: speaker segments with times
        "gpt-4o-transcribe-diarize": SimpleNamespace(
            text="hi there", language="en", segments=[segment]
        ),
        # json: text only
        "gpt-4o-transcribe": SimpleNamespace(text="hi there", language="en"),
    }
    client = _fake_client(transcriptions_return=responses[model])
    with _patched_openai(client), _patched_open():
        result = OpenAIASRProvider(api_key="k", model_name=model).transcribe(
            str(tmp_path / "a.wav"), ASRConfig(language="en")
        )
    return result.segments


def _google(tmp_path, model: str, monkeypatch) -> list[ASRSegment]:
    from google.cloud import speech

    from app.services.asr.google_provider import GoogleASRProvider
    from tests.unit.test_google_provider import _config
    from tests.unit.test_google_provider import _mock_client
    from tests.unit.test_google_provider import _word
    from tests.unit.test_google_provider import _write_audio

    response = speech.LongRunningRecognizeResponse(
        results=[
            speech.SpeechRecognitionResult(
                alternatives=[
                    speech.SpeechRecognitionAlternative(
                        transcript="hi there",
                        words=[_word("hi", 0.0, 0.3, 1), _word("there", 0.3, 0.8, 1)],
                    )
                ]
            )
        ]
    )
    with patch.object(speech, "SpeechClient", return_value=_mock_client(response)):
        result = GoogleASRProvider(
            api_key=None, model_name=model, credentials_file=None
        ).transcribe(_write_audio(tmp_path), _config())
    return result.segments


def _azure(tmp_path, model: str, monkeypatch) -> list[ASRSegment]:
    from tests.unit.test_azure_provider import _FakeAudioConfig
    from tests.unit.test_azure_provider import _FakeSpeechConfig
    from tests.unit.test_azure_provider import _make_fake_sdk
    from tests.unit.test_azure_provider import _provider
    from tests.unit.test_azure_provider import _recognized_event
    from tests.unit.test_azure_provider import _recognizer_factory

    sdk = _make_fake_sdk(recognizer_cls=_recognizer_factory(queued_events=[_recognized_event()]))
    segments: list[ASRSegment] = _provider()._transcribe_without_diarization(
        sdk, _FakeSpeechConfig(), _FakeAudioConfig(), "file.wav", None
    )
    return segments


def _aws(tmp_path, model: str, monkeypatch) -> list[ASRSegment]:
    from tests.unit.test_aws_provider import _provider
    from tests.unit.test_aws_provider import _stub_clients
    from tests.unit.test_aws_provider import _wire_provider

    item = {
        "type": "pronunciation",
        "start_time": "0.000",
        "end_time": "1.000",
        "alternatives": [{"content": "hello", "confidence": "0.99"}],
    }
    data = {"results": {"items": [item]}}
    provider = _provider(model_name=model)
    s3, tc = _stub_clients(
        job_response={"TranscriptionJob": {"TranscriptionJobStatus": "COMPLETED"}},
        result_data=data,
    )
    tc.get_medical_transcription_job.return_value = {
        "MedicalTranscriptionJob": {"MedicalTranscriptionJobStatus": "COMPLETED"}
    }
    _wire_provider(provider, monkeypatch, s3, tc)
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF....WAVEfmt ")
    return provider.transcribe(str(audio), ASRConfig(language="en")).segments


def _speechmatics(tmp_path, model: str, monkeypatch) -> list[ASRSegment]:
    from tests.unit.test_speechmatics_provider import _provider
    from tests.unit.test_speechmatics_provider import _transcript
    from tests.unit.test_speechmatics_provider import _word

    transcript = _transcript([_word("Hello", 0.0, 0.5, "S1"), _word("world", 0.6, 1.0, "S1")])
    segments: list[ASRSegment] = _provider(model_name=model)._build_segments(transcript)
    return segments


def _gladia(tmp_path, model: str, monkeypatch) -> list[ASRSegment]:
    from app.services.asr.gladia_provider import GladiaProvider
    from tests.helpers import stub_public_dns
    from tests.unit.test_gladia_provider import _BASE
    from tests.unit.test_gladia_provider import _dispatch_post
    from tests.unit.test_gladia_provider import _FakeResponse
    from tests.unit.test_gladia_provider import _make_audio_file
    from tests.unit.test_gladia_provider import _mock_session

    stub_public_dns(monkeypatch, domain="api.gladia.io")
    words = [
        {"word": "hello", "start": 0.0, "end": 0.5},
        {"word": "world", "start": 0.6, "end": 1.0},
    ]
    utterance = {"text": "hello world", "start": 0.0, "end": 1.0, "words": words}
    done = _FakeResponse(
        json_data={"status": "done", "result": {"transcription": {"utterances": [utterance]}}}
    )
    _mock_session(
        monkeypatch,
        post_side_effect=_dispatch_post(
            _FakeResponse(json_data={"audio_url": "https://cdn.gladia.io/a"}),
            _FakeResponse(json_data={"result_url": f"{_BASE}/v2/transcription/j"}),
            {},
        ),
        get_side_effect=lambda *a, **k: done,
    )
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    provider = GladiaProvider(api_key="k", model_name=model)
    return provider.transcribe(_make_audio_file(tmp_path), ASRConfig(language="en")).segments


def _pyannote(tmp_path, model: str, monkeypatch) -> list[ASRSegment]:
    from tests.unit.test_pyannote_provider import _provider

    output = {
        "turnLevelTranscription": [{"start": 0.5, "end": 1.6, "text": "hi there", "speaker": "S"}],
        "wordLevelTranscription": [
            {"start": 0.5, "end": 0.8, "text": "hi", "speaker": "S"},
            {"start": 0.9, "end": 1.6, "text": "there", "speaker": "S"},
        ],
    }
    segments: list[ASRSegment] = _provider(model_name=model)._parse_response(output)
    return segments


RUNNERS = {
    "deepgram": _deepgram,
    "assemblyai": _assemblyai,
    "openai": _openai,
    "google": _google,
    "azure": _azure,
    "aws": _aws,
    "speechmatics": _speechmatics,
    "gladia": _gladia,
    "pyannote": _pyannote,
}

CLOUD_MODELS = [
    (provider, model["id"])
    for provider, entry in ASR_PROVIDER_CATALOG.items()
    if provider != "local"
    for model in entry["models"]
]


def test_every_cloud_provider_has_a_runner():
    assert set(RUNNERS) == {p for p, _ in CLOUD_MODELS}


def test_every_provider_and_model_resolves_a_granularity():
    assert len(ASR_PROVIDER_CATALOG) >= 10, "the catalog lost its providers"
    for provider, entry in ASR_PROVIDER_CATALOG.items():
        assert entry.get("timestamp_granularity") in {"word", "segment", "none"}, provider
        for model in entry["models"]:
            caps = ASRProviderFactory.get_model_capabilities(provider, model["id"])
            assert caps["timestamp_granularity"] in {"word", "segment", "none"}


@pytest.mark.parametrize(("provider", "model"), CLOUD_MODELS)
def test_declared_granularity_matches_the_parsed_result(provider, model, tmp_path, monkeypatch):
    declared = ASRProviderFactory.get_model_capabilities(provider, model)["timestamp_granularity"]
    segments = RUNNERS[provider](tmp_path, model, monkeypatch)
    assert segments, f"{provider}/{model}: the fixture produced no segments"
    assert _classify(segments) == declared


def test_local_whisper_is_word_timed():
    """faster-whisper runs with word_timestamps=True (transcriber.py)."""
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "app" / "transcription" / "transcriber.py"
    ).read_text()
    assert "word_timestamps=True" in source
    assert (
        ASRProviderFactory.get_model_capabilities("local", "large-v3")["timestamp_granularity"]
        == "word"
    )


def test_the_classifier_tells_the_three_apart():
    """Control: a classifier that answered one value for everything would pass a uniform
    catalog."""
    word = ASRSegment("a b", 0.0, 1.0, words=[ASRWord("a", 0.0, 0.4), ASRWord("b", 0.5, 1.0)])
    assert _classify([word]) == "word"
    assert _classify([ASRSegment("a b", 0.0, 1.0)]) == "segment"
    assert _classify([ASRSegment("a b", 0.0, 0.0)]) == "none"
    assert _classify([]) == "none"
