"""Characterization + regression tests for `app/services/asr/aws_provider.py`.

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
2. **Fixed bug** in the diarization word-to-speaker join (~L293-318): `spk_map`
   is now keyed on `float(start_time)` instead of the raw JSON string, and each
   lookup parses the pronunciation item's `start_time` the same way, so
   formatting drift between the `items[]` and `speaker_labels[]` blocks (e.g.
   `"2.0"` vs `"2.000"`) no longer causes a silent fall-back to the previous
   word's speaker — the word is attributed to its true speaker segment.
3. **Fixed bug** in `head_bucket` handling (~L153-177): only a `ClientError`
   whose `response["Error"]["Code"]` is a documented "not found" code (`"404"`
   — HeadBucket returns no body, so botocore reports the bare HTTP status as
   the code; also `"NoSuchBucket"`/`"NotFound"` for safety) falls through to
   `create_bucket`. Any other exception — a permissions error, throttling, or
   anything that isn't even a `ClientError` — now propagates instead of being
   silently swallowed and masked as "bucket doesn't exist yet".
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


TEST_BUCKET = "my-transcribe-scratch"
TEST_ACCOUNT = "123456789012"


def _sts(account: str | None = TEST_ACCOUNT) -> MagicMock:
    sts = MagicMock(name="sts")
    sts.get_caller_identity.return_value = {"Account": account} if account else {}
    return sts


@pytest.fixture(autouse=True)
def _bucket_configured(monkeypatch):
    """The bucket is required with no default (#1235); tests that need it unset delete it."""
    monkeypatch.setenv("AWS_TRANSCRIBE_BUCKET", TEST_BUCKET)
    monkeypatch.delenv("AWS_TRANSCRIBE_BUCKET_OWNER", raising=False)


def _wire_provider(provider: AWSTranscribeProvider, monkeypatch, s3, tc, sts=None) -> None:
    clients = {"s3": s3, "transcribe": tc, "sts": sts or _sts()}
    monkeypatch.setattr(provider, "_boto_client", lambda service: clients[service])
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


# ── 2. spk_map lookup is numeric, so start_time formatting drift is ignored ──


def test_speaker_segment_lookup_matches_on_numeric_value_despite_formatting_drift(
    monkeypatch, audio_file
):
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
                    # Fixed behavior: both sides are parsed to float before
                    # comparison, so this still resolves to spk_1.
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

    # Fixed behavior: "world"'s start_time ("2.0") is parsed to the same float
    # as the speaker_labels key ("2.000"), so it correctly resolves to its
    # true speaker ("spk_1" -> SPEAKER_01), producing a speaker-change
    # boundary and two segments instead of one silently-misattributed blob.
    assert len(result.segments) == 2
    assert result.segments[0].speaker == "SPEAKER_00"
    assert [w.word for w in result.segments[0].words] == ["hello"]
    assert result.segments[1].speaker == "SPEAKER_01"
    assert [w.word for w in result.segments[1].words] == ["world"]


# ── 3. head_bucket: only a real "not found" falls through to create_bucket ───


def test_head_bucket_access_denied_propagates_instead_of_creating_bucket(monkeypatch, audio_file):
    """A permissions-style failure from head_bucket must not be masked.

    Fixed behavior: an exception that is not a botocore ClientError carrying a
    documented "not found" error code (here a generic ``Exception`` worded
    like an AccessDenied failure) now propagates out of ``transcribe()``
    instead of being swallowed and treated as "the bucket doesn't exist yet".
    """
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
    with pytest.raises(Exception, match="AccessDenied"):
        provider.transcribe(audio_file, config)

    assert not s3.create_bucket.called


def test_head_bucket_not_found_still_falls_through_to_create_bucket(monkeypatch, audio_file):
    """The case that must keep working: a genuinely missing bucket.

    ``HeadBucket`` is a HEAD request with no response body, so botocore can't
    parse a named error code and reports the bare HTTP status ("404") as
    ``response["Error"]["Code"]`` instead — this is documented, verified
    botocore/boto3 behavior for a missing bucket (see boto/boto3#2499,
    boto/boto3#4092). That specific code must still trigger create_bucket.
    """
    from botocore.exceptions import ClientError

    provider = _provider()
    job_response = {"TranscriptionJob": {"TranscriptionJobStatus": "COMPLETED"}}
    data: dict[str, Any] = {"results": {"items": []}}
    not_found_exc = ClientError(
        error_response={"Error": {"Code": "404", "Message": "Not Found"}},
        operation_name="HeadBucket",
    )
    s3, tc = _stub_clients(
        job_response=job_response, result_data=data, s3_head_bucket_side_effect=not_found_exc
    )
    _wire_provider(provider, monkeypatch, s3, tc)

    config = ASRConfig(language="en", enable_diarization=False)
    result = provider.transcribe(audio_file, config)  # must not raise

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


# ── Rate-limit taxonomy (issue Lane 5) ────────────────────────────────────────


def _client_error(code: str, message: str = "boom"):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": message}}, "StartTranscriptionJob")


def test_throttling_exception_on_job_start_is_classified_as_asr_rate_limited(
    monkeypatch, audio_file
):
    from app.services.asr.errors import ASRRateLimitedError

    provider = _provider()
    s3 = MagicMock(name="s3")
    tc = MagicMock(name="transcribe")
    tc.start_transcription_job.side_effect = _client_error("ThrottlingException")
    _wire_provider(provider, monkeypatch, s3, tc)

    config = ASRConfig(language="en", enable_diarization=False)
    with pytest.raises(ASRRateLimitedError) as excinfo:
        provider.transcribe(audio_file, config)

    assert excinfo.value.provider == "aws"
    # Cleanup is still attempted even though the job never started.
    assert s3.delete_object.call_count == 2


def test_limit_exceeded_exception_on_job_start_is_classified_as_asr_rate_limited(
    monkeypatch, audio_file
):
    """LimitExceededException is Transcribe's concurrent-job cap — transient, not a
    permanent misconfiguration, so it must classify the same as ThrottlingException.
    """
    from app.services.asr.errors import ASRRateLimitedError

    provider = _provider()
    s3 = MagicMock(name="s3")
    tc = MagicMock(name="transcribe")
    tc.start_transcription_job.side_effect = _client_error("LimitExceededException")
    _wire_provider(provider, monkeypatch, s3, tc)

    config = ASRConfig(language="en", enable_diarization=False)
    with pytest.raises(ASRRateLimitedError):
        provider.transcribe(audio_file, config)


def test_access_denied_exception_on_job_start_stays_a_plain_client_error(monkeypatch, audio_file):
    """Negative control: a permissions failure must NOT be classified as retryable —
    without this, classifying every ClientError as retryable would still pass the two
    tests above.
    """
    from botocore.exceptions import ClientError

    from app.services.asr.errors import ASRRateLimitedError

    provider = _provider()
    s3 = MagicMock(name="s3")
    tc = MagicMock(name="transcribe")
    tc.start_transcription_job.side_effect = _client_error("AccessDeniedException")
    _wire_provider(provider, monkeypatch, s3, tc)

    config = ASRConfig(language="en", enable_diarization=False)
    with pytest.raises(ClientError) as excinfo:
        provider.transcribe(audio_file, config)

    assert not isinstance(excinfo.value, ASRRateLimitedError)


# ── 8. The bucket is required (no predictable default) and every S3 call is owner-pinned ──


def test_transcribe_without_a_bucket_fails_fast_before_any_aws_call(monkeypatch, audio_file):
    monkeypatch.delenv("AWS_TRANSCRIBE_BUCKET", raising=False)
    provider = _provider()
    boto = MagicMock(name="boto_client")
    monkeypatch.setattr(provider, "_boto_client", boto)

    with pytest.raises(RuntimeError, match="AWS_TRANSCRIBE_BUCKET is not set"):
        provider.transcribe(audio_file, ASRConfig(language="en", enable_diarization=False))

    boto.assert_not_called()


def test_a_blank_bucket_is_treated_as_unset(monkeypatch, audio_file):
    monkeypatch.setenv("AWS_TRANSCRIBE_BUCKET", "   ")
    provider = _provider()
    monkeypatch.setattr(provider, "_boto_client", MagicMock(name="boto_client"))

    with pytest.raises(RuntimeError, match="AWS_TRANSCRIBE_BUCKET is not set"):
        provider.transcribe(audio_file, ASRConfig(language="en", enable_diarization=False))


def test_the_old_predictable_default_bucket_is_never_used(monkeypatch, audio_file):
    """Region-derived names (``opentranscribe-tmp-<region>``) are guessable by anyone."""
    monkeypatch.delenv("AWS_TRANSCRIBE_BUCKET", raising=False)
    provider = _provider(region="eu-west-1")
    s3 = MagicMock(name="s3")
    monkeypatch.setattr(provider, "_boto_client", lambda service: s3)

    with pytest.raises(RuntimeError):
        provider.transcribe(audio_file, ASRConfig(language="en", enable_diarization=False))

    assert s3.mock_calls == []


def test_validate_connection_names_the_missing_bucket_without_calling_aws(monkeypatch):
    monkeypatch.delenv("AWS_TRANSCRIBE_BUCKET", raising=False)
    provider = _provider()
    boto = MagicMock(name="boto_client")
    monkeypatch.setattr(provider, "_boto_client", boto)

    ok, message, _ms = provider.validate_connection()

    assert ok is False
    assert "AWS_TRANSCRIBE_BUCKET" in message
    boto.assert_not_called()


def test_every_s3_call_carries_expected_bucket_owner(monkeypatch, audio_file):
    provider = _provider()
    job_response = {"TranscriptionJob": {"TranscriptionJobStatus": "COMPLETED"}}
    s3, tc = _stub_clients(job_response=job_response, result_data={"results": {"items": []}})
    sts = _sts()
    _wire_provider(provider, monkeypatch, s3, tc, sts=sts)

    provider.transcribe(audio_file, ASRConfig(language="en", enable_diarization=False))

    s3.head_bucket.assert_called_once_with(Bucket=TEST_BUCKET, ExpectedBucketOwner=TEST_ACCOUNT)
    (upload_args, upload_kwargs) = s3.upload_file.call_args
    assert upload_args[1] == TEST_BUCKET
    assert upload_kwargs["ExtraArgs"] == {"ExpectedBucketOwner": TEST_ACCOUNT}
    assert s3.get_object.call_args.kwargs["ExpectedBucketOwner"] == TEST_ACCOUNT
    assert s3.delete_object.call_count == 2
    for call in s3.delete_object.call_args_list:
        assert call.kwargs["ExpectedBucketOwner"] == TEST_ACCOUNT
        assert call.kwargs["Bucket"] == TEST_BUCKET
    sts.get_caller_identity.assert_called_once_with()


def test_cleanup_deletes_stay_owner_pinned_when_the_job_fails(monkeypatch, audio_file):
    provider = _provider()
    job_response = {
        "TranscriptionJob": {"TranscriptionJobStatus": "FAILED", "FailureReason": "nope"}
    }
    s3, tc = _stub_clients(job_response=job_response)
    _wire_provider(provider, monkeypatch, s3, tc)

    with pytest.raises(RuntimeError, match="job failed"):
        provider.transcribe(audio_file, ASRConfig(language="en", enable_diarization=False))

    assert s3.delete_object.call_count == 2
    assert all(
        c.kwargs["ExpectedBucketOwner"] == TEST_ACCOUNT for c in s3.delete_object.call_args_list
    )


def test_the_owner_override_replaces_the_sts_lookup(monkeypatch, audio_file):
    monkeypatch.setenv("AWS_TRANSCRIBE_BUCKET_OWNER", "999988887777")
    provider = _provider()
    job_response = {"TranscriptionJob": {"TranscriptionJobStatus": "COMPLETED"}}
    s3, tc = _stub_clients(job_response=job_response, result_data={"results": {"items": []}})
    sts = _sts()
    _wire_provider(provider, monkeypatch, s3, tc, sts=sts)

    result = provider.transcribe(audio_file, ASRConfig(language="en", enable_diarization=False))

    assert result.provider_name == "aws"
    assert s3.head_bucket.call_args.kwargs["ExpectedBucketOwner"] == "999988887777"
    sts.get_caller_identity.assert_not_called()


def test_an_unresolvable_account_id_stops_the_job_before_anything_is_uploaded(
    monkeypatch, audio_file
):
    provider = _provider()
    s3, tc = _stub_clients(job_response={})
    _wire_provider(provider, monkeypatch, s3, tc, sts=_sts(account=None))

    with pytest.raises(RuntimeError, match="AWS_TRANSCRIBE_BUCKET_OWNER"):
        provider.transcribe(audio_file, ASRConfig(language="en", enable_diarization=False))

    s3.upload_file.assert_not_called()
