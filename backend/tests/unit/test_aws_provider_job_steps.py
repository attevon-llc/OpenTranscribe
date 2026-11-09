"""Characterization of the AWS Transcribe job steps through a botocore ``Stubber``.

The Transcribe client is a REAL botocore client wrapped in
``botocore.stub.Stubber`` (https://botocore.amazonaws.com/v1/documentation/api/latest/reference/stubber.html):
every ``StartTranscriptionJob`` / ``StartMedicalTranscriptionJob`` /
``GetTranscriptionJob`` request is validated against the service model and
compared with the exact expected parameters, so a wrong or missing field fails
the test. S3 is a ``MagicMock`` because ``upload_file`` is an s3transfer managed
call the Stubber does not intercept. No network, no credentials.

API shapes: AWS Transcribe ``StartTranscriptionJob`` / ``GetTranscriptionJob`` /
``StartMedicalTranscriptionJob`` / ``GetMedicalTranscriptionJob``.
"""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.stub import ANY
from botocore.stub import Stubber

from app.core.config import settings
from app.services.asr import aws_provider as aws_module
from app.services.asr.aws_provider import AWSTranscribeProvider
from app.services.asr.types import ASRConfig

BUCKET = "scratch-bucket"
OWNER = "123456789012"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(settings, "AWS_TRANSCRIBE_BUCKET", BUCKET)
    monkeypatch.setenv("AWS_TRANSCRIBE_BUCKET_OWNER", OWNER)
    monkeypatch.delenv("AWS_TRANSCRIBE_VOCABULARY_NAME", raising=False)
    monkeypatch.setattr(aws_module.time, "sleep", lambda _s: None)


@pytest.fixture
def audio(tmp_path):
    p = tmp_path / "a.wav"
    p.write_bytes(b"RIFF")
    return str(p)


def _setup(monkeypatch, model_name="standard", result=None):
    provider = AWSTranscribeProvider(region="us-east-1", model_name=model_name)
    tc = boto3.client(
        "transcribe",
        region_name="us-east-1",
        aws_access_key_id="AKIAFAKE",
        aws_secret_access_key="fake",
    )
    stub = Stubber(tc)
    s3 = MagicMock(name="s3")
    s3.get_object.return_value = {
        "Body": io.BytesIO(json.dumps(result or {"results": {}}).encode())
    }
    clients = {"s3": s3, "transcribe": tc}
    monkeypatch.setattr(provider, "_boto_client", lambda svc: clients[svc])
    return provider, stub, s3


def _job(status, medical=False, **extra):
    key = "MedicalTranscriptionJob" if medical else "TranscriptionJob"
    status_key = "TranscriptionJobStatus"
    name_key = "MedicalTranscriptionJobName" if medical else "TranscriptionJobName"
    return {key: {name_key: "j", status_key: status, **extra}}


def _start_params(**extra):
    base = {
        "TranscriptionJobName": ANY,
        "Media": {"MediaFileUri": ANY},
        "OutputBucketName": BUCKET,
        "OutputKey": ANY,
    }
    base.update(extra)
    return base


def _medical_start_params():
    # StartMedicalTranscriptionJob names the job MedicalTranscriptionJobName (not
    # TranscriptionJobName) and has no IdentifyLanguage, so LanguageCode is required.
    return {
        "MedicalTranscriptionJobName": ANY,
        "Media": {"MediaFileUri": ANY},
        "OutputBucketName": BUCKET,
        "OutputKey": ANY,
        "LanguageCode": "en-US",
        "Specialty": "PRIMARYCARE",
        "Type": "CONVERSATION",
    }


def _run(provider, audio, **cfg):
    return provider.transcribe(audio, ASRConfig(**cfg))


def test_explicit_language_starts_job_with_language_code_and_cleans_up(monkeypatch, audio):
    provider, stub, s3 = _setup(monkeypatch)
    stub.add_response(
        "start_transcription_job",
        {"TranscriptionJob": {"TranscriptionJobName": "j"}},
        _start_params(LanguageCode="en-US"),
    )
    stub.add_response("get_transcription_job", _job("COMPLETED"), {"TranscriptionJobName": ANY})
    with stub:
        result = _run(provider, audio, language="en", enable_diarization=False)
    stub.assert_no_pending_responses()
    assert result.provider_name == "aws"
    assert result.has_speakers is False
    s3.upload_file.assert_called_once()
    assert s3.upload_file.call_args.kwargs["ExtraArgs"] == {"ExpectedBucketOwner": OWNER}
    assert s3.delete_object.call_count == 2


def test_auto_language_uses_identify_language(monkeypatch, audio):
    provider, stub, _ = _setup(monkeypatch)
    stub.add_response(
        "start_transcription_job",
        {"TranscriptionJob": {"TranscriptionJobName": "j"}},
        _start_params(IdentifyLanguage=True),
    )
    stub.add_response("get_transcription_job", _job("COMPLETED"), {"TranscriptionJobName": ANY})
    with stub:
        _run(provider, audio, language="auto", enable_diarization=False)
    stub.assert_no_pending_responses()


def test_multilingual_model_identifies_multiple_languages(monkeypatch, audio):
    provider, stub, _ = _setup(monkeypatch, model_name="multilingual")
    stub.add_response(
        "start_transcription_job",
        {"TranscriptionJob": {"TranscriptionJobName": "j"}},
        _start_params(IdentifyMultipleLanguages=True),
    )
    stub.add_response("get_transcription_job", _job("COMPLETED"), {"TranscriptionJobName": ANY})
    with stub:
        _run(provider, audio, language="en", enable_diarization=False)
    stub.assert_no_pending_responses()


def test_diarization_and_vocabulary_become_job_settings(monkeypatch, audio):
    monkeypatch.setenv("AWS_TRANSCRIBE_VOCABULARY_NAME", "my-vocab")
    provider, stub, _ = _setup(monkeypatch)
    stub.add_response(
        "start_transcription_job",
        {"TranscriptionJob": {"TranscriptionJobName": "j"}},
        _start_params(
            LanguageCode="en-US",
            Settings={
                "ShowSpeakerLabels": True,
                "MaxSpeakerLabels": 30,
                "VocabularyName": "my-vocab",
            },
        ),
    )
    stub.add_response("get_transcription_job", _job("COMPLETED"), {"TranscriptionJobName": ANY})
    with stub:
        _run(
            provider,
            audio,
            language="en",
            enable_diarization=True,
            max_speakers=99,
            vocabulary=["x"],
        )
    stub.assert_no_pending_responses()


def test_max_speakers_has_a_floor_of_two(monkeypatch, audio):
    provider, stub, _ = _setup(monkeypatch)
    stub.add_response(
        "start_transcription_job",
        {"TranscriptionJob": {"TranscriptionJobName": "j"}},
        _start_params(
            LanguageCode="en-US", Settings={"ShowSpeakerLabels": True, "MaxSpeakerLabels": 2}
        ),
    )
    stub.add_response("get_transcription_job", _job("COMPLETED"), {"TranscriptionJobName": ANY})
    with stub:
        _run(provider, audio, language="en", enable_diarization=True, max_speakers=1)
    stub.assert_no_pending_responses()


def test_vocabulary_without_configured_name_is_dropped_with_a_warning(monkeypatch, audio, caplog):
    provider, stub, _ = _setup(monkeypatch)
    stub.add_response(
        "start_transcription_job",
        {"TranscriptionJob": {"TranscriptionJobName": "j"}},
        _start_params(LanguageCode="en-US"),
    )
    stub.add_response("get_transcription_job", _job("COMPLETED"), {"TranscriptionJobName": ANY})
    with stub, caplog.at_level("WARNING"):
        _run(provider, audio, language="en", enable_diarization=False, vocabulary=["x"])
    assert "AWS_TRANSCRIBE_VOCABULARY_NAME is not set" in caplog.text


def test_medical_model_uses_the_medical_job_api(monkeypatch, audio):
    provider, stub, _ = _setup(monkeypatch, model_name="medical")
    stub.add_response(
        "start_medical_transcription_job",
        {"MedicalTranscriptionJob": {"MedicalTranscriptionJobName": "j"}},
        _medical_start_params(),
    )
    stub.add_response(
        "get_medical_transcription_job",
        _job("COMPLETED", medical=True),
        {"MedicalTranscriptionJobName": ANY},
    )
    with stub:
        result = _run(provider, audio, language="en", enable_diarization=False)
    stub.assert_no_pending_responses()
    assert result.model_name == "medical"


def test_polling_reports_progress_until_completed(monkeypatch, audio):
    provider, stub, _ = _setup(monkeypatch)
    stub.add_response(
        "start_transcription_job",
        {"TranscriptionJob": {"TranscriptionJobName": "j"}},
        _start_params(LanguageCode="en-US"),
    )
    for status in ("QUEUED", "IN_PROGRESS", "COMPLETED"):
        stub.add_response("get_transcription_job", _job(status), {"TranscriptionJobName": ANY})
    events: list[tuple[float, str]] = []
    with stub:
        provider.transcribe(
            audio,
            ASRConfig(language="en", enable_diarization=False),
            progress_callback=lambda p, m: events.append((p, m)),
        )
    assert events[0] == (0.1, "Uploading to S3 for AWS Transcribe…")
    assert events[1] == (0.25, "AWS Transcribe job running…")
    assert events[2][1] == events[3][1] == "AWS Transcribe processing…"
    assert events[2][0] == pytest.approx(0.25 + 15 / 7200)
    assert events[3][0] == pytest.approx(0.25 + 30 / 7200)
    assert events[-2] == (0.9, "Parsing AWS Transcribe results…")
    assert events[-1] == (1.0, "AWS Transcribe complete")


def test_job_that_never_completes_times_out_and_still_cleans_up(monkeypatch, audio):
    provider, stub, s3 = _setup(monkeypatch)
    stub.add_response(
        "start_transcription_job",
        {"TranscriptionJob": {"TranscriptionJobName": "j"}},
        _start_params(LanguageCode="en-US"),
    )
    for _ in range(7200 // 15):
        stub.add_response(
            "get_transcription_job", _job("IN_PROGRESS"), {"TranscriptionJobName": ANY}
        )
    with stub, pytest.raises(RuntimeError, match="timed out after 7200 seconds"):
        _run(provider, audio, language="en", enable_diarization=False)
    stub.assert_no_pending_responses()
    assert s3.delete_object.call_count == 2
    s3.get_object.assert_not_called()


def test_medical_failed_job_reports_sanitized_reason(monkeypatch, audio):
    provider, stub, _ = _setup(monkeypatch, model_name="medical")
    stub.add_response(
        "start_medical_transcription_job",
        {"MedicalTranscriptionJob": {"MedicalTranscriptionJobName": "j"}},
        _medical_start_params(),
    )
    stub.add_response(
        "get_medical_transcription_job",
        _job("FAILED", medical=True, FailureReason="bad media"),
        {"MedicalTranscriptionJobName": ANY},
    )
    with stub, pytest.raises(RuntimeError, match="AWS Transcribe job failed: bad media"):
        _run(provider, audio, language="en", enable_diarization=False)


def test_service_throttle_on_start_is_a_rate_limit_error(monkeypatch, audio):
    from app.services.asr.errors import ASRRateLimitedError

    provider, stub, s3 = _setup(monkeypatch)
    stub.add_client_error(
        "start_transcription_job", service_error_code="LimitExceededException", http_status_code=400
    )
    with stub, pytest.raises(ASRRateLimitedError, match="AWS Transcribe rate limited"):
        _run(provider, audio, language="en", enable_diarization=False)
    assert s3.delete_object.call_count == 2


def _word(text, start, end, conf="0.9"):
    return {
        "type": "pronunciation",
        "start_time": str(start),
        "end_time": str(end),
        "alternatives": [{"content": text, "confidence": conf}],
    }


def _punct(text):
    return {"type": "punctuation", "alternatives": [{"content": text}]}


def _parse(monkeypatch, audio, items, diarize=False, speaker_labels=None):
    results: dict = {"items": items, "language_code": "en-US"}
    if speaker_labels is not None:
        results["speaker_labels"] = speaker_labels
    provider, stub, _ = _setup(monkeypatch, result={"results": results})
    extra = {"Settings": {"ShowSpeakerLabels": True, "MaxSpeakerLabels": 20}} if diarize else {}
    stub.add_response(
        "start_transcription_job",
        {"TranscriptionJob": {"TranscriptionJobName": "j"}},
        _start_params(LanguageCode="en-US", **extra),
    )
    stub.add_response("get_transcription_job", _job("COMPLETED"), {"TranscriptionJobName": ANY})
    with stub:
        return _run(provider, audio, language="en", enable_diarization=diarize)


def test_parse_splits_on_silence_gap_and_attaches_punctuation(monkeypatch, audio):
    items = [
        _word("Hello", 0.0, 0.4),
        _punct(","),
        _word("world", 0.5, 0.9),
        _word("again", 2.0, 2.5, ""),
    ]
    result = _parse(monkeypatch, audio, items)
    assert [s.text for s in result.segments] == ["Hello, world", "again"]
    assert result.segments[0].start == 0.0 and result.segments[0].end == 0.9
    assert result.segments[0].confidence == pytest.approx(0.9)
    assert result.segments[1].words[0].confidence == 1.0
    assert result.segments[0].speaker is None
    assert result.language == "en"  # ASRResult normalizes en-US


def test_parse_splits_a_long_continuous_run_at_max_duration(monkeypatch, audio):
    items = [
        _word("a", 0.0, 10.0),
        _word("b", 10.0, 20.0),
        _word("c", 20.0, 31.0),
        _word("d", 31.0, 32.0),
    ]
    result = _parse(monkeypatch, audio, items)
    assert [s.text for s in result.segments] == ["a b c", "d"]


def test_parse_with_no_words_yields_one_empty_segment(monkeypatch, audio):
    result = _parse(monkeypatch, audio, [_punct(".")])
    assert [(s.text, s.start, s.end) for s in result.segments] == [("", 0.0, 0.0)]


def test_parse_diarized_groups_by_speaker_and_attaches_punctuation(monkeypatch, audio):
    items = [
        _punct("?"),
        _word("hi", 0.0, 0.5),
        _punct("!"),
        _word("yo", 1.0, 1.5),
        _word("ok", 2.0, 2.5),
    ]
    labels = {
        "segments": [
            {"speaker_label": "spk_0", "items": [{"start_time": "0.0"}]},
            {"speaker_label": "spk_1", "items": [{"start_time": "1.0"}, {"start_time": "2.0"}]},
        ]
    }
    result = _parse(monkeypatch, audio, items, diarize=True, speaker_labels=labels)
    assert result.has_speakers is True
    assert [(s.text, s.speaker, s.start, s.end) for s in result.segments] == [
        ("hi!", "SPEAKER_00", 0.0, 0.5),
        ("yo ok", "SPEAKER_01", 1.0, 2.5),
    ]


@pytest.mark.parametrize("language", ["auto", ""])
def test_medical_model_with_auto_language_sends_en_us_not_identify_language(
    monkeypatch, audio, language
):
    provider, stub, _ = _setup(monkeypatch, model_name="medical")
    stub.add_response(
        "start_medical_transcription_job",
        {"MedicalTranscriptionJob": {"MedicalTranscriptionJobName": "j"}},
        _medical_start_params(),
    )
    stub.add_response(
        "get_medical_transcription_job",
        _job("COMPLETED", medical=True),
        {"MedicalTranscriptionJobName": ANY},
    )
    with stub:
        _run(provider, audio, language=language, enable_diarization=False)
    stub.assert_no_pending_responses()
