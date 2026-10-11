"""Another provider's speaker detection, used only for its speakers (#1226).

``ASRDiarizationAdapter`` runs a saved cloud ASR configuration with diarization on and keeps
only the speaker turns. Every catalog provider/model that returns speakers is driven here
through its REAL provider module, with a two-speaker vendor-shaped response stubbed at the
HTTP/SDK seam (the fixture shapes the provider modules' own unit tests use; no network, no
key). For each one: the turns are right, the vendor's text is gone, labels come out in the
shared ``SPEAKER_XX`` form, diarization is switched on in the request, the speaker hints the
catalog says the module sends reach the request, and a failure comes back as a fixed sentence.

Speaker output shapes that were not verified against a recorded real response are built from
the provider module's existing parser and fixtures (Azure ``ConversationTranscriber``
events, OpenAI ``diarized_json``, Speechmatics, Gladia); see the module docstring of each
provider and ``docs/design/v060_pipeline_composition_plan.md`` section B.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.asr.base import ASRProvider
from app.services.asr.factory import ASR_PROVIDER_CATALOG
from app.services.asr.factory import ASRProviderFactory
from app.services.asr.types import ASRResult
from app.services.asr.types import ASRSegment
from app.services.asr.types import ASRWord
from app.services.diarization.asr_adapter import ASRDiarizationAdapter
from app.services.diarization.asr_adapter import speaker_turns
from app.services.diarization.errors import DiarizationProviderFailedError
from app.services.diarization.errors import DiarizationRateLimitedError
from app.services.diarization.errors import DiarizerConfigUnusableError
from app.services.diarization.types import DiarizeConfig
from app.services.diarization.types import DiarizeResult

#: Two speakers, each with two words; the second turn starts at 2.0 s.
SECRET_TEXT = ("zylofenix", "quorbantle", "mivrastel", "plethorique")
EXPECTED_TURNS = [(0.0, 1.0, "SPEAKER_00"), (2.0, 3.0, "SPEAKER_01")]
CONFIG = DiarizeConfig(min_speakers=2, max_speakers=4)

SPEAKER_MODELS = [
    (provider, model["id"])
    for provider, entry in ASR_PROVIDER_CATALOG.items()
    if provider != "local"
    for model in entry["models"]
    if ASRProviderFactory.get_model_capabilities(provider, model["id"])["supports_diarization"]
]
TEXT_ONLY_MODELS = [
    (provider, model["id"])
    for provider, entry in ASR_PROVIDER_CATALOG.items()
    if provider != "local"
    for model in entry["models"]
    if not ASRProviderFactory.get_model_capabilities(provider, model["id"])["supports_diarization"]
]


def _audio(tmp_path) -> str:
    path = tmp_path / "audio.wav"
    path.write_bytes(b"RIFF....WAVEfmt fake")
    return str(path)


def _adapter(provider, provider_id: str, model: str) -> ASRDiarizationAdapter:
    caps = ASRProviderFactory.get_model_capabilities(provider_id, model)
    return ASRDiarizationAdapter(
        provider, model_name=model, speaker_hints=caps["speaker_count_hints"]
    )


# --- one runner per vendor: (DiarizeResult, the request the module built) ------------------


def _deepgram(tmp_path, model, monkeypatch):
    from app.services.asr.deepgram_provider import DeepgramProvider
    from tests.unit.test_deepgram_provider import _dg_response
    from tests.unit.test_deepgram_provider import _dg_utterance
    from tests.unit.test_deepgram_provider import _dg_word

    a, b, c, d = SECRET_TEXT
    response = _dg_response(
        utterances=[
            _dg_utterance(0, f"{a} {b}", 0.0, 1.0, [_dg_word(a, 0.0, 0.4), _dg_word(b, 0.5, 1.0)]),
            _dg_utterance(1, f"{c} {d}", 2.0, 3.0, [_dg_word(c, 2.0, 2.4), _dg_word(d, 2.5, 3.0)]),
        ]
    )
    with patch("deepgram.DeepgramClient") as client_cls:
        call = client_cls.return_value.listen.v1.media.transcribe_file
        call.return_value = response
        result = _adapter(DeepgramProvider("k", model), "deepgram", model).diarize(
            _audio(tmp_path), CONFIG
        )
        request = call.call_args.kwargs
    return result, {"diarize": request["diarize"]}


def _assemblyai(tmp_path, model, monkeypatch):
    from app.services.asr.assemblyai_provider import AssemblyAIProvider
    from tests.unit.test_assemblyai_provider import _fake_transcript
    from tests.unit.test_assemblyai_provider import _fake_utterance
    from tests.unit.test_assemblyai_provider import _fake_word

    a, b, c, d = SECRET_TEXT
    transcript = _fake_transcript(
        utterances=[
            _fake_utterance(
                f"{a} {b}", 0, 1000, "A", [_fake_word(a, 0, 400), _fake_word(b, 500, 1000)]
            ),
            _fake_utterance(
                f"{c} {d}", 2000, 3000, "B", [_fake_word(c, 2000, 2400), _fake_word(d, 2500, 3000)]
            ),
        ]
    )
    with patch("assemblyai.Transcriber") as transcriber_cls:
        transcriber_cls.return_value.transcribe.return_value = transcript
        result = _adapter(AssemblyAIProvider("k", model), "assemblyai", model).diarize(
            _audio(tmp_path), CONFIG
        )
        sdk_config = transcriber_cls.return_value.transcribe.call_args.kwargs["config"]
    options = sdk_config.speaker_options
    return result, {
        "diarize": sdk_config.speaker_labels,
        "min": options.min_speakers_expected,
        "max": options.max_speakers_expected,
    }


def _openai(tmp_path, model, monkeypatch):
    from app.services.asr.openai_provider import OpenAIASRProvider
    from tests.unit.test_openai_provider import _fake_client
    from tests.unit.test_openai_provider import _patched_open
    from tests.unit.test_openai_provider import _patched_openai

    a, b, c, d = SECRET_TEXT
    response = SimpleNamespace(
        text=" ".join(SECRET_TEXT),
        language="en",
        segments=[
            SimpleNamespace(start=0.0, end=1.0, text=f"{a} {b}", speaker="Speaker 1"),
            SimpleNamespace(start=2.0, end=3.0, text=f"{c} {d}", speaker="Speaker 2"),
        ],
    )
    client = _fake_client(transcriptions_return=response)
    with _patched_openai(client), _patched_open():
        result = _adapter(OpenAIASRProvider("k", model), "openai", model).diarize(
            str(tmp_path / "a.wav"), CONFIG
        )
    request = client.audio.transcriptions.create.call_args.kwargs
    return result, {"diarize": request["response_format"] == "diarized_json"}


def _google(tmp_path, model, monkeypatch):
    from google.cloud import speech

    from app.services.asr.google_provider import GoogleASRProvider
    from tests.unit.test_google_provider import _mock_client
    from tests.unit.test_google_provider import _word

    a, b, c, d = SECRET_TEXT
    words = [
        _word(a, 0.0, 0.4, 1),
        _word(b, 0.5, 1.0, 1),
        _word(c, 2.0, 2.4, 2),
        _word(d, 2.5, 3.0, 2),
    ]
    response = speech.LongRunningRecognizeResponse(
        results=[
            speech.SpeechRecognitionResult(
                alternatives=[
                    speech.SpeechRecognitionAlternative(
                        transcript=" ".join(SECRET_TEXT), words=words
                    )
                ]
            )
        ]
    )
    client = _mock_client(response)
    with patch.object(speech, "SpeechClient", return_value=client):
        provider = GoogleASRProvider(api_key=None, model_name=model, credentials_file=None)
        result = _adapter(provider, "google", model).diarize(_audio(tmp_path), CONFIG)
    dz = client.long_running_recognize.call_args.kwargs["config"].diarization_config
    return result, {
        "diarize": dz.enable_speaker_diarization,
        "min": dz.min_speaker_count,
        "max": dz.max_speaker_count,
    }


def _azure(tmp_path, model, monkeypatch):
    from app.services.asr.azure_provider import AzureASRProvider
    from tests.unit.test_azure_provider import _install_fake_sdk_module
    from tests.unit.test_azure_provider import _make_fake_sdk
    from tests.unit.test_azure_provider import _transcribed_event
    from tests.unit.test_azure_provider import _transcriber_factory

    a, b, c, d = SECRET_TEXT
    events = [
        _transcribed_event(text=f"{a} {b}", offset=0, duration=10_000_000, speaker_id="Guest-1"),
        _transcribed_event(
            text=f"{c} {d}", offset=20_000_000, duration=10_000_000, speaker_id="Guest-2"
        ),
    ]
    transcriber_cls = _transcriber_factory(queued_events=events)
    _install_fake_sdk_module(monkeypatch, _make_fake_sdk(transcriber_cls=transcriber_cls))
    provider = AzureASRProvider(api_key="k", region="eastus", model_name=model)
    result = _adapter(provider, "azure", model).diarize(_audio(tmp_path), CONFIG)
    return result, {"diarize": len(transcriber_cls.instances) == 1}


def _aws(tmp_path, model, monkeypatch):
    from tests.unit.test_aws_provider import _provider
    from tests.unit.test_aws_provider import _stub_clients
    from tests.unit.test_aws_provider import _wire_provider

    def item(word, start, end):
        return {
            "type": "pronunciation",
            "start_time": f"{start:.3f}",
            "end_time": f"{end:.3f}",
            "alternatives": [{"content": word, "confidence": "0.99"}],
        }

    a, b, c, d = SECRET_TEXT
    items = [item(a, 0.0, 0.4), item(b, 0.5, 1.0), item(c, 2.0, 2.4), item(d, 2.5, 3.0)]
    labels = {
        "segments": [
            {"speaker_label": "spk_0", "items": [{"start_time": "0.0"}, {"start_time": "0.5"}]},
            {"speaker_label": "spk_1", "items": [{"start_time": "2.0"}, {"start_time": "2.5"}]},
        ]
    }
    data = {"results": {"items": items, "speaker_labels": labels}}
    provider = _provider(model_name=model)
    s3, tc = _stub_clients(
        job_response={"TranscriptionJob": {"TranscriptionJobStatus": "COMPLETED"}},
        result_data=data,
    )
    tc.get_medical_transcription_job.return_value = {
        "MedicalTranscriptionJob": {"MedicalTranscriptionJobStatus": "COMPLETED"}
    }
    _wire_provider(provider, monkeypatch, s3, tc)
    result = _adapter(provider, "aws", model).diarize(_audio(tmp_path), CONFIG)
    start = tc.start_medical_transcription_job if model == "medical" else tc.start_transcription_job
    settings = start.call_args.kwargs["Settings"]
    return result, {"diarize": settings["ShowSpeakerLabels"], "max": settings["MaxSpeakerLabels"]}


def _speechmatics(tmp_path, model, monkeypatch):
    from app.services.asr.speechmatics_provider import SpeechmaticsProvider
    from tests.unit.test_speechmatics_provider import _transcript
    from tests.unit.test_speechmatics_provider import _word

    a, b, c, d = SECRET_TEXT
    transcript = _transcript(
        [
            _word(a, 0.0, 0.4, "S1"),
            _word(b, 0.5, 1.0, "S1"),
            _word(c, 2.0, 2.4, "S2"),
            _word(d, 2.5, 3.0, "S2"),
        ]
    )
    sent: dict = {}

    class _Client:
        def __init__(self, api_key):
            pass

        async def submit_job(self, audio_path, transcription_config=None):
            sent["config"] = transcription_config
            return SimpleNamespace(id="job")

        async def wait_for_completion(self, job_id, format_type=None, timeout=None):
            return transcript

        async def close(self):
            return None

    monkeypatch.setattr("speechmatics.batch.AsyncClient", _Client)
    provider = SpeechmaticsProvider("k", model)
    result = _adapter(provider, "speechmatics", model).diarize(_audio(tmp_path), CONFIG)
    return result, {"diarize": sent["config"].diarization == "speaker"}


def _gladia(tmp_path, model, monkeypatch):
    from app.services.asr.gladia_provider import GladiaProvider
    from tests.helpers import stub_public_dns
    from tests.unit.test_gladia_provider import _BASE
    from tests.unit.test_gladia_provider import _dispatch_post
    from tests.unit.test_gladia_provider import _FakeResponse
    from tests.unit.test_gladia_provider import _mock_session

    stub_public_dns(monkeypatch, domain="api.gladia.io")
    a, b, c, d = SECRET_TEXT
    utterances = [
        {
            "text": f"{a} {b}",
            "start": 0.0,
            "end": 1.0,
            "speaker": 0,
            "words": [{"word": a, "start": 0.0, "end": 0.4}, {"word": b, "start": 0.5, "end": 1.0}],
        },
        {
            "text": f"{c} {d}",
            "start": 2.0,
            "end": 3.0,
            "speaker": 1,
            "words": [{"word": c, "start": 2.0, "end": 2.4}, {"word": d, "start": 2.5, "end": 3.0}],
        },
    ]
    done = _FakeResponse(
        json_data={"status": "done", "result": {"transcription": {"utterances": utterances}}}
    )
    captured: dict = {}
    _mock_session(
        monkeypatch,
        post_side_effect=_dispatch_post(
            _FakeResponse(json_data={"audio_url": "https://cdn.gladia.io/a"}),
            _FakeResponse(json_data={"result_url": f"{_BASE}/v2/transcription/j"}),
            captured,
        ),
        get_side_effect=lambda *a, **k: done,
    )
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    provider = GladiaProvider(api_key="k", model_name=model)
    result = _adapter(provider, "gladia", model).diarize(_audio(tmp_path), CONFIG)
    return result, {"diarize": captured["body"]["diarization"]}


def _pyannote(tmp_path, model, monkeypatch):
    from app.services.asr.pyannote_provider import PyAnnoteProvider
    from tests.unit.test_pyannote_provider import _dispatch_post
    from tests.unit.test_pyannote_provider import _FakeResponse

    a, b, c, d = SECRET_TEXT
    output = {
        "turnLevelTranscription": [
            {"start": 0.0, "end": 1.0, "text": f"{a} {b}", "speaker": "SPEAKER_00"},
            {"start": 2.0, "end": 3.0, "text": f"{c} {d}", "speaker": "SPEAKER_01"},
        ],
        "wordLevelTranscription": [
            {"start": 0.0, "end": 0.4, "text": a},
            {"start": 0.5, "end": 1.0, "text": b},
            {"start": 2.0, "end": 2.4, "text": c},
            {"start": 2.5, "end": 3.0, "text": d},
        ],
    }
    captured: dict = {}
    with (
        patch(
            "httpx.post",
            side_effect=_dispatch_post(
                _FakeResponse(json_data={"url": "https://upload.example.com/put"}),
                _FakeResponse(json_data={"jobId": "job-1"}),
                captured,
            ),
        ),
        patch("httpx.put", return_value=_FakeResponse()),
        patch(
            "httpx.get",
            return_value=_FakeResponse(json_data={"status": "succeeded", "output": output}),
        ),
        patch("time.sleep"),
    ):
        provider = PyAnnoteProvider("k", model)
        result = _adapter(provider, "pyannote", model).diarize(_audio(tmp_path), CONFIG)
    body = captured["body"]
    return result, {"diarize": True, "min": body["minSpeakers"], "max": body["maxSpeakers"]}


RUNNERS: dict[str, Callable] = {
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


def test_every_speaker_provider_has_a_runner():
    assert SPEAKER_MODELS, "the catalog lost every speaker model"
    assert {p for p, _ in SPEAKER_MODELS} == set(RUNNERS)


@pytest.mark.parametrize(("provider", "model"), SPEAKER_MODELS)
def test_turns_come_back_without_the_text(provider, model, tmp_path, monkeypatch):
    from app.services.diarization import asr_adapter

    parsed: list[ASRResult] = []

    def _spy(vendor_result: ASRResult, *args, **kwargs):
        parsed.append(vendor_result)
        return speaker_turns(vendor_result, *args, **kwargs)

    monkeypatch.setattr(asr_adapter, "speaker_turns", _spy)
    result, request = RUNNERS[provider](tmp_path, model, monkeypatch)

    # Control: the vendor's parsed result really carried the words the adapter discards.
    assert len(parsed) == 1
    vendor_text = " ".join(segment.text for segment in parsed[0].segments)
    assert all(word in vendor_text for word in SECRET_TEXT)
    assert isinstance(result, DiarizeResult)
    assert [(t.start, t.end, t.speaker) for t in result.segments] == EXPECTED_TURNS
    assert result.num_speakers == 2
    assert result.provider_name == provider
    assert result.model_name == model
    assert request["diarize"] is True
    dumped = repr(result) + json.dumps(result.metadata)
    assert not any(word in dumped for word in SECRET_TEXT)


@pytest.mark.parametrize(("provider", "model"), SPEAKER_MODELS)
def test_the_hints_the_catalog_names_reach_the_request(provider, model, tmp_path, monkeypatch):
    hints = set(ASRProviderFactory.get_model_capabilities(provider, model)["speaker_count_hints"])
    _result, request = RUNNERS[provider](tmp_path, model, monkeypatch)

    sent = {hint for hint in ("min", "max") if hint in request}
    assert sent == hints - {"num"}
    if "min" in sent:
        assert request["min"] == CONFIG.min_speakers
    if "max" in sent:
        assert request["max"] == CONFIG.max_speakers


def test_a_fixed_count_is_forwarded_as_num_speakers():
    seen: list = []

    class _Recorder(_FakeASR):
        def transcribe(self, audio_path, config, progress_callback=None) -> ASRResult:
            seen.append(config)
            return _two_speaker_result()

    _wrap(_Recorder()).diarize(
        "a.wav", DiarizeConfig(min_speakers=1, max_speakers=9, num_speakers=3)
    )
    assert len(seen) == 1
    assert (seen[0].num_speakers, seen[0].min_speakers, seen[0].max_speakers) == (3, 1, 9)
    assert seen[0].enable_diarization is True
    assert seen[0].translate_to_english is False
    assert seen[0].vocabulary is None


@pytest.mark.parametrize(("provider", "model"), TEXT_ONLY_MODELS)
def test_a_text_only_model_is_refused(provider, model):
    from app.services.asr.openai_provider import OpenAIASRProvider

    assert provider == "openai", "only OpenAI has text-only models; add the provider here"
    with pytest.raises(DiarizerConfigUnusableError):
        ASRDiarizationAdapter(OpenAIASRProvider("k", model), model_name=model)


def test_the_local_provider_is_refused():
    from app.services.asr.local_provider import LocalASRProvider

    with pytest.raises(DiarizerConfigUnusableError):
        ASRDiarizationAdapter(LocalASRProvider())


def test_text_only_and_speaker_models_both_exist():
    """Control for the two parametrisations above: neither may be empty."""
    assert TEXT_ONLY_MODELS
    assert ("openai", "gpt-4o-transcribe-diarize") in SPEAKER_MODELS


# --- turn extraction ------------------------------------------------------------------------


class _FakeASR(ASRProvider):
    """A vendor module whose network call is the seam."""

    def __init__(self, result: ASRResult | None = None, error: Exception | None = None):
        self._result, self._error = result, error

    @property
    def provider_name(self) -> str:
        return "fakevendor"

    def supports_diarization(self) -> bool:
        return True

    def supports_vocabulary(self) -> bool:
        return False

    def supports_translation(self) -> bool:
        return False

    def validate_connection(self) -> tuple[bool, str, float]:
        return True, "ok", 1.0

    def transcribe(self, audio_path, config, progress_callback=None) -> ASRResult:
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def _wrap(asr: ASRProvider) -> ASRDiarizationAdapter:
    return ASRDiarizationAdapter(asr, model_name="m")


def _two_speaker_result() -> ASRResult:
    return ASRResult(
        segments=[
            ASRSegment("x", 0.0, 1.0, speaker="A", words=[ASRWord("x", 0.0, 1.0)]),
            ASRSegment("y", 2.0, 3.0, speaker="B", words=[ASRWord("y", 2.0, 3.0)]),
        ],
        language="en",
    )


def test_same_speaker_words_within_the_gap_are_one_turn_and_a_longer_pause_splits():
    result = ASRResult(
        segments=[
            ASRSegment(
                "a b c",
                0.0,
                5.0,
                speaker="SPEAKER_00",
                words=[ASRWord("a", 0.0, 1.0), ASRWord("b", 1.4, 2.0), ASRWord("c", 4.0, 5.0)],
            )
        ],
        language="en",
    )
    turns = speaker_turns(result)
    assert [(t.start, t.end, t.speaker) for t in turns] == [
        (0.0, 2.0, "SPEAKER_00"),
        (4.0, 5.0, "SPEAKER_00"),
    ]


def test_consecutive_segments_of_one_speaker_join_across_the_segment_boundary():
    result = ASRResult(
        segments=[
            ASRSegment("a", 0.0, 1.0, speaker="S1", words=[ASRWord("a", 0.0, 1.0)]),
            ASRSegment("b", 1.2, 2.0, speaker="S1", words=[ASRWord("b", 1.2, 2.0)]),
        ],
        language="en",
    )
    assert [(t.start, t.end, t.speaker) for t in speaker_turns(result)] == [
        (0.0, 2.0, "SPEAKER_00")
    ]


def test_labels_are_normalised_by_the_shared_helper():
    result = ASRResult(
        segments=[
            ASRSegment("a", 0.0, 1.0, speaker="Guest-2"),
            ASRSegment("b", 2.0, 3.0, speaker="spk_4"),
            ASRSegment("c", 4.0, 5.0, speaker="S3"),
        ],
        language="en",
    )
    assert [t.speaker for t in speaker_turns(result)] == ["SPEAKER_01", "SPEAKER_04", "SPEAKER_02"]


def test_unlabelled_and_untimed_pieces_are_skipped_and_output_is_sorted():
    result = ASRResult(
        segments=[
            ASRSegment("late", 5.0, 6.0, speaker="SPEAKER_01"),
            ASRSegment("unknown", 2.0, 3.0, speaker=None),
            ASRSegment("zero", 4.0, 4.0, speaker="SPEAKER_00"),
            ASRSegment(
                "early",
                0.0,
                1.0,
                speaker="SPEAKER_00",
                words=[ASRWord("p", 0.5, 0.5), ASRWord("early", 0.0, 1.0)],
            ),
        ],
        language="en",
    )
    assert [(t.start, t.end, t.speaker) for t in speaker_turns(result)] == [
        (0.0, 1.0, "SPEAKER_00"),
        (5.0, 6.0, "SPEAKER_01"),
    ]


# --- errors ---------------------------------------------------------------------------------

SECRET_KEY = "sk-live-adapter-SECRET-0123456789"


def test_a_vendor_failure_is_a_fixed_sentence_and_the_key_stays_out_of_the_log(caplog):
    vendor_text = f"401 Unauthorized: key {SECRET_KEY} rejected, transcript: zylofenix"
    adapter = _wrap(_FakeASR(error=RuntimeError(vendor_text)))

    with caplog.at_level(logging.ERROR), pytest.raises(DiarizationProviderFailedError) as info:
        adapter.diarize("a.wav", CONFIG)

    assert str(info.value) == DiarizationProviderFailedError.MESSAGE
    assert "zylofenix" not in str(info.value)
    assert SECRET_KEY not in caplog.text
    assert "fakevendor" in caplog.text  # the failure is still attributed in the log


def test_a_vendor_rate_limit_stays_retryable_with_a_fixed_sentence():
    from app.services.asr.errors import ASRRateLimitedError

    adapter = _wrap(_FakeASR(error=ASRRateLimitedError(f"429 {SECRET_KEY}", retry_after=7.0)))

    with pytest.raises(DiarizationRateLimitedError) as info:
        adapter.diarize("a.wav", CONFIG)

    assert isinstance(info.value, ASRRateLimitedError)
    assert info.value.retry_after == 7.0
    assert str(info.value) == DiarizationProviderFailedError.MESSAGE


def test_the_configuration_error_classifies_as_a_permanent_settings_problem():
    from app.services.error_categorization_service import ErrorCategorizationService
    from app.services.error_categorization_service import UserErrorReason
    from app.utils.error_classification import ErrorCategory

    classified = ErrorCategorizationService.classify_failure(str(DiarizerConfigUnusableError()))

    assert classified.reason == UserErrorReason.DIARIZER_CONFIG_UNUSABLE
    assert classified.retry_category == ErrorCategory.CONFIGURATION_REQUIRED
    assert classified.user_message == DiarizerConfigUnusableError.MESSAGE


def test_the_vendor_failure_sentence_reads_back_as_a_generic_failure_not_a_network_one():
    """If a caller ever stores it, it must not borrow a network/auth reason or retry class."""
    from app.services.error_categorization_service import ErrorCategorizationService
    from app.services.error_categorization_service import UserErrorReason
    from app.utils.error_classification import ErrorCategory

    classified = ErrorCategorizationService.classify_failure(DiarizationProviderFailedError.MESSAGE)
    assert classified.reason == UserErrorReason.PROCESSING_ERROR
    assert classified.retry_category == ErrorCategory.UNKNOWN
