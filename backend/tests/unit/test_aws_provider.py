"""Characterization tests for `app/services/asr/aws_provider.py`.

Network-free: `AWSTranscribeProvider._boto_client` is monkeypatched to return
`MagicMock` S3/Transcribe clients, so no boto3 call ever leaves the process. The
Amazon Transcribe response shape used to build fake job results (`results.items[]`
with `type`/`start_time`/`end_time`/`alternatives[].content`/`.confidence`, plus
`results.speaker_labels.segments[].items[].start_time` keyed to a parent
`speaker_label`) matches AWS's documented Transcribe JSON output format.

What is pinned here, in order:

1. **Now-fixed regression**: the `FAILED`-status `RuntimeError` wraps
   `FailureReason` in `self._sanitize_error(...)` before raising — a credential
   embedded in the AWS-reported failure reason no longer reaches the exception
   message verbatim.
2. **Open defect** in the diarization word-to-speaker join (~L296): the
   `spk_map` built from `speaker_labels.segments[].items[].start_time` is keyed
   on the raw JSON string, and a pronunciation item's `start_time` string that
   doesn't match *exactly* (e.g. differing trailing-zero formatting) silently
   falls back to the previous word's speaker (`cur_spk`) instead of erroring or
   using the item's true speaker segment.
3. **Open defect** in `head_bucket` handling (~L153-161): ANY exception from
   `head_bucket` — not just "bucket not found" — falls through to attempting
   `create_bucket`, masking a genuine permissions error as a missing bucket.
4. AWS's `spk_N` speaker labels (e.g. `"spk_0"`) hit `normalize_speaker_label`'s
   0-indexed `spk_(\\d+)` branch.
5. `validate_connection()` makes a REAL network call
   (`list_transcription_jobs(MaxResults=1)`) and surfaces a bad-credential
   failure as `False` — unlike azure/google's `validate_connection()`, which
   never dials out and false-positives on a bad key.

Following the characterization-test convention of
``tests/unit/test_transcription_storage.py``.
"""

from __future__ import annotations

import io
import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.services.asr import aws_provider as aws_provider_module
from app.services.asr.aws_provider import AWSTranscribeProvider
from app.services.asr.types import ASRConfig


def _provider(**kwargs) -> AWSTranscribeProvider:
    kwargs.setdefault("region", "us-east-1")
    kwargs.setdefault("model_name", "standard")
    return AWSTranscribeProvider(**kwargs)


def _stub_clients(
    *,
    job_response: dict,
    result_data: dict | None = None,
    s3_head_bucket_side_effect: Exception | None = None,
) -> tuple[MagicMock, MagicMock]:
    """Build fake S3 + Transcribe boto3 clients wired for one job run."""
    s3 = MagicMock(name="s3")
    if s3_head_bucket_side_effect is not None:
        s3.head_bucket.side_effect = s3_head_bucket_side_effect
    if result_data is not None:
        body_bytes = json.dumps(result_data).encode()
        s3.get_object.return_value = {"Body": io.BytesIO(body_bytes)}

    tc = MagicMock(name="transcribe")
    tc.get_transcription_job.return_value = job_response
    return s3, tc


def _wire_provider(provider: AWSTranscribeProvider, monkeypatch, s3, tc) -> None:
    monkeypatch.setattr(
        provider, "_boto_client", lambda service: {"s3": s3, "transcribe": tc}[service]
    )
    # The polling loop sleeps 15s per iteration against a real AWS job; skip it.
    monkeypatch.setattr(aws_provider_module.time, "sleep", lambda _seconds: None)


@pytest.fixture
def audio_file(tmp_path):
    p = tmp_path / "sample.wav"
    p.write_bytes(b"RIFF....WAVEfmt ")
    return str(p)


# ── 1. FAILED status now sanitizes FailureReason ─────────────────────────────


def test_failed_job_sanitizes_failure_reason_in_raised_error(monkeypatch, audio_file):
    provider = _provider()
    fake_key = "sk-liveFAKEKEY1234567890abcdef"
    job_response = {
        "TranscriptionJob": {
            "TranscriptionJobStatus": "FAILED",
            "FailureReason": f"Access denied using credential {fake_key} for this account",
        }
    }
    s3, tc = _stub_clients(job_response=job_response)
    _wire_provider(provider, monkeypatch, s3, tc)

    config = ASRConfig(language="en", enable_diarization=False)
    with pytest.raises(RuntimeError) as exc_info:
        provider.transcribe(audio_file, config)

    message = str(exc_info.value)
    assert fake_key not in message
    assert "sk-***" in message
    # Cleanup is still attempted even though the job failed.
    assert s3.delete_object.call_count == 2


# ── 2. spk_map lookup silently falls back on a start_time mismatch ──────────


def test_speaker_segment_lookup_falls_back_silently_on_start_time_mismatch(monkeypatch, audio_file):
    provider = _provider()
    data = {
        "results": {
            "speaker_labels": {
                "segments": [
                    {"speaker_label": "spk_0", "items": [{"start_time": "0.000"}]},
                    {"speaker_label": "spk_1", "items": [{"start_time": "2.000"}]},
                ]
            },
            "items": [
                {
                    "type": "pronunciation",
                    "start_time": "0.000",
                    "end_time": "1.000",
                    "alternatives": [{"content": "hello", "confidence": "0.99"}],
                },
                {
                    # Deliberately mismatched string formatting vs the
                    # speaker_labels entry above ("2.000") — the real-world
                    # trap when float serialization differs between the
                    # items[] and speaker_labels[] blocks of one response.
                    "type": "pronunciation",
                    "start_time": "2.0",
                    "end_time": "3.000",
                    "alternatives": [{"content": "world", "confidence": "0.98"}],
                },
            ],
        }
    }
    job_response = {"TranscriptionJob": {"TranscriptionJobStatus": "COMPLETED"}}
    s3, tc = _stub_clients(job_response=job_response, result_data=data)
    _wire_provider(provider, monkeypatch, s3, tc)

    config = ASRConfig(language="en", enable_diarization=True, max_speakers=2)
    result = provider.transcribe(audio_file, config)

    # Today's behavior: "world"'s start_time ("2.0") doesn't match the
    # speaker_labels key ("2.000"), so spk_map.get() falls back to cur_spk
    # (the PREVIOUS word's speaker) instead of the word's true speaker
    # ("spk_1"). No speaker-change boundary is created, and both words land
    # in one segment under the first speaker.
    assert len(result.segments) == 1
    assert result.segments[0].speaker == "SPEAKER_00"
    assert [w.word for w in result.segments[0].words] == ["hello", "world"]


# ── 3. head_bucket exception falls through to create_bucket unconditionally ──


def test_head_bucket_exception_falls_through_to_create_bucket(monkeypatch, audio_file):
    provider = _provider()
    job_response = {"TranscriptionJob": {"TranscriptionJobStatus": "COMPLETED"}}
    data: dict[str, Any] = {"results": {"items": []}}
    generic_exc = Exception(
        "AccessDenied: User arn:aws:iam::123456789012:user/ci is not authorized "
        "to perform: s3:HeadBucket"
    )
    s3, tc = _stub_clients(
        job_response=job_response, result_data=data, s3_head_bucket_side_effect=generic_exc
    )
    _wire_provider(provider, monkeypatch, s3, tc)

    config = ASRConfig(language="en", enable_diarization=False)
    result = provider.transcribe(audio_file, config)  # must not raise

    # Today's behavior: ANY head_bucket exception — including a permissions
    # error unrelated to the bucket existing — is swallowed and create_bucket
    # is attempted anyway, masking the real error.
    assert s3.create_bucket.called
    assert result.provider_name == "aws"


# ── 4. AWS spk_N labels normalize 0-indexed ───────────────────────────────────


def test_aws_spk_label_normalizes_zero_indexed():
    provider = _provider()
    assert provider._normalize_speaker_label("spk_0") == "SPEAKER_00"
    assert provider._normalize_speaker_label("spk_5") == "SPEAKER_05"


# ── 5. validate_connection() makes a real network call ───────────────────────


def test_validate_connection_makes_a_real_network_call_and_reports_success(monkeypatch):
    provider = _provider(access_key_id="AKIAEXAMPLE", secret_access_key="secretvalue")
    tc = MagicMock(name="transcribe")
    tc.list_transcription_jobs.return_value = {"TranscriptionJobSummaries": []}
    monkeypatch.setattr(provider, "_boto_client", lambda service: tc)

    ok, message, ms = provider.validate_connection()

    assert ok is True
    assert "AWS Transcribe validated" in message
    tc.list_transcription_jobs.assert_called_once_with(MaxResults=1)
    assert ms >= 0.0


def test_validate_connection_fails_and_sanitizes_credentials_on_a_bad_key(monkeypatch):
    access_key = "AKIA-FAKE-TEST-ACCESS-KEY"
    secret_key = "FakeSecretValueThatShouldNeverAppearInLogs123"
    provider = _provider(access_key_id=access_key, secret_access_key=secret_key)
    tc = MagicMock(name="transcribe")
    tc.list_transcription_jobs.side_effect = Exception(
        "UnrecognizedClientException: The security token included in the request "
        f"is invalid for access key {access_key} secret {secret_key}"
    )
    monkeypatch.setattr(provider, "_boto_client", lambda service: tc)

    ok, message, ms = provider.validate_connection()

    # A REAL network call was made and its failure is surfaced as an overall
    # failure — not a false-positive "validated" like azure/google's
    # validate_connection(), which never dials out at all.
    assert ok is False
    tc.list_transcription_jobs.assert_called_once_with(MaxResults=1)
    assert access_key not in message
    assert secret_key not in message
    assert "***" in message
