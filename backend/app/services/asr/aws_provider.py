"""Amazon Transcribe ASR provider.

Targets boto3 >= 1.26.0.  Supports Standard and Medical transcription
(HIPAA-eligible).

S3 lifecycle notes
------------------
Audio and result files are uploaded to a temporary S3 bucket and cleaned up
after the job completes.  On failure the cleanup is still attempted so stale
objects are not left behind in S3.

The bucket is ``AWS_TRANSCRIBE_BUCKET`` and has NO default (issue #1235): S3 bucket
names are global, so a predictable default such as ``opentranscribe-tmp-<region>``
could send a deployment's audio to a bucket owned by someone else. Every S3 call
also carries ``ExpectedBucketOwner`` (the caller's account, or
``AWS_TRANSCRIBE_BUCKET_OWNER`` for a bucket in another account of the same
organization), so S3 refuses with 403 if the bucket is not the expected account's.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from app.core.config import settings
from app.core.env import env_str
from app.core.pipeline_constants import ASR_SEGMENT_MAX_DURATION_SECONDS
from app.core.pipeline_constants import ASR_SEGMENT_MIN_GAP_SECONDS
from app.utils.exception_log_safety import MessageFreeTracebackFilter
from app.utils.module_probe import module_available

from .base import ASRProvider
from .types import ASRConfig
from .types import ASRResult
from .types import ASRSegment
from .types import ASRWord
from .types import mean_word_confidence

logger = logging.getLogger(__name__)
# Provider SDK errors can quote credentials in their message: log frames only.
logger.addFilter(MessageFreeTracebackFilter())

# botocore ClientError codes that represent a transient throttle rather than a permanent
# failure. LimitExceededException is Transcribe's concurrent-job cap — genuinely transient,
# not a permanent misconfiguration — so it is included alongside the more obvious
# throttling codes.
_AWS_THROTTLE_CODES = frozenset(
    {
        "ThrottlingException",
        "ThrottledException",
        "TooManyRequestsException",
        "LimitExceededException",
        "RequestLimitExceeded",
        # STS (GetCallerIdentity) and the older query-protocol services spell it this way.
        "Throttling",
        "RequestThrottled",
    }
)

#: How long a resolved account id is trusted. The account behind a set of credentials does
#: not change while they are in use; the bound only covers credentials that were swapped.
BUCKET_OWNER_CACHE_SECONDS = 3600
_monotonic = time.monotonic
_owner_cache: dict[tuple[str, str], tuple[str, float]] = {}
_owner_cache_lock = threading.Lock()


# AWS Transcribe LanguageCode is BCP-47 (e.g. "en-US"), NOT a bare ISO-639 code ("en").
# Map common ISO codes to a sensible AWS default; pass through codes already BCP-47.
_AWS_LANGUAGE_MAP = {
    "en": "en-US",
    "es": "es-US",
    "fr": "fr-FR",
    "de": "de-DE",
    "it": "it-IT",
    "pt": "pt-BR",
    "ja": "ja-JP",
    "ko": "ko-KR",
    "zh": "zh-CN",
    "ru": "ru-RU",
    "ar": "ar-SA",
    "hi": "hi-IN",
    "nl": "nl-NL",
}


BUCKET_ENV = "AWS_TRANSCRIBE_BUCKET"
BUCKET_OWNER_ENV = "AWS_TRANSCRIBE_BUCKET_OWNER"

#: Shown by ``transcribe()`` and the admin connection test when the bucket is not set.
BUCKET_REQUIRED_MESSAGE = (
    "AWS Transcribe needs an S3 bucket for its temporary audio and results, and "
    f"{BUCKET_ENV} is not set. Create a private bucket in your own AWS account in the "
    f"same region as AWS_REGION and set {BUCKET_ENV} to its name. There is no default: "
    "bucket names are global, so a guessable one could send audio to another account."
)


def _aws_language_code(language: str) -> str | None:
    """ISO-639 / BCP-47 → AWS LanguageCode, or None to use IdentifyLanguage.

    "auto" and unknown codes return None so the caller sets IdentifyLanguage=True
    (LanguageCode and IdentifyLanguage are mutually exclusive in AWS Transcribe).
    """
    if not language or language == "auto":
        return None
    if "-" in language:  # already BCP-47 (e.g. en-GB, pt-PT)
        return language
    return _AWS_LANGUAGE_MAP.get(language.lower())


class AWSTranscribeProvider(ASRProvider):
    def __init__(
        self,
        region: str = "us-east-1",
        model_name: str = "standard",
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        api_key: str | None = None,
    ):
        self._region = region
        self._model_name = model_name
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key

    @property
    def provider_name(self) -> str:
        return "aws"

    def supports_diarization(self) -> bool:
        return True

    def supports_vocabulary(self) -> bool:
        return True

    def supports_translation(self) -> bool:
        return False

    @staticmethod
    def _configured_bucket() -> str:
        """The deployer's bucket, or a clear RuntimeError (issue #1235: no default)."""
        bucket = settings.AWS_TRANSCRIBE_BUCKET.strip()
        if not bucket:
            raise RuntimeError(BUCKET_REQUIRED_MESSAGE)
        return bucket

    def _bucket_owner(self) -> str:
        """Account id every S3 call must find the bucket owned by (``ExpectedBucketOwner``).

        ``AWS_TRANSCRIBE_BUCKET_OWNER`` wins when set (a bucket in another account of the
        deployer's organization); otherwise it is the caller's own account, from STS, which
        needs no IAM permission. The STS answer is cached per credentials and region for
        ``BUCKET_OWNER_CACHE_SECONDS``, so a job costs no extra round trip; a failed lookup
        is never cached.

        Raises:
            ASRRateLimitedError: STS throttled the call (retryable).
            RuntimeError: STS could not be reached or refused, or returned no account. A VPC
                with no STS endpoint must set ``AWS_TRANSCRIBE_BUCKET_OWNER``.
        """
        override = env_str(BUCKET_OWNER_ENV)
        if override:
            return override
        key = (self._access_key_id or "", self._region)
        with _owner_cache_lock:
            cached = _owner_cache.get(key)
        if cached is not None and _monotonic() < cached[1]:
            return cached[0]
        account = self._lookup_account_id()
        with _owner_cache_lock:
            _owner_cache[key] = (account, _monotonic() + BUCKET_OWNER_CACHE_SECONDS)
        return account

    def _lookup_account_id(self) -> str:
        """STS ``GetCallerIdentity``, with its failures classified (never a raw botocore trace)."""
        from botocore.exceptions import BotoCoreError
        from botocore.exceptions import ClientError

        needs_override = (
            f"Set {BUCKET_OWNER_ENV} to the 12-digit account id that owns the bucket "
            "(required where STS is not reachable, for example a VPC with no STS endpoint)."
        )
        try:
            account = self._boto_client("sts").get_caller_identity().get("Account")
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            sanitized = self._sanitize_error(str(exc), self._secret_access_key)
            if code in _AWS_THROTTLE_CODES:
                from .errors import ASRRateLimitedError

                logger.warning("AWS STS rate-limited while resolving the account id: %s", sanitized)
                raise ASRRateLimitedError(
                    f"AWS STS rate limited: {sanitized}", provider="aws"
                ) from exc
            raise RuntimeError(
                f"Could not determine the AWS account id from STS ({code or 'error'}). "
                f"{needs_override}"
            ) from exc
        except BotoCoreError as exc:
            sanitized = self._sanitize_error(str(exc), self._secret_access_key)
            raise RuntimeError(
                f"Could not reach AWS STS to determine the account id ({sanitized}). "
                f"{needs_override}"
            ) from exc
        if not account:
            raise RuntimeError(
                f"Could not determine the AWS account id for the S3 bucket-owner check. "
                f"{needs_override}"
            )
        return str(account)

    def _boto_client(self, service: str) -> Any:
        import boto3

        kw: dict = {"region_name": self._region}
        if self._access_key_id and self._secret_access_key:
            kw["aws_access_key_id"] = self._access_key_id
            kw["aws_secret_access_key"] = self._secret_access_key
        return boto3.client(service, **kw)

    def validate_connection(self) -> tuple[bool, str, float]:
        """Test credentials by listing transcription jobs (MaxResults=1)."""
        start = time.time()
        if not module_available("boto3"):
            return False, "boto3 not installed. Run: pip install boto3", 0.0
        if not settings.AWS_TRANSCRIBE_BUCKET.strip():
            return False, BUCKET_REQUIRED_MESSAGE, 0.0
        try:
            self._boto_client("transcribe").list_transcription_jobs(MaxResults=1)
            ms = (time.time() - start) * 1000
            return True, f"AWS Transcribe validated (region: {self._region})", ms
        except Exception as e:  # provider-SDK boundary: the probe reports, never raises
            logger.warning("%s connection test failed", "AWS Transcribe", exc_info=True)
            ms = (time.time() - start) * 1000
            # Sanitize error — boto3 exceptions may reflect back credential values
            # in debug/error strings in some SDK versions.
            sanitized = self._sanitize_error(str(e), self._secret_access_key)
            sanitized = self._sanitize_error(sanitized, self._access_key_id)
            return False, sanitized, ms

    def transcribe(
        self,
        audio_path: str,
        config: ASRConfig,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> ASRResult:
        try:
            from botocore.exceptions import ClientError
        except ImportError as err:
            raise RuntimeError("boto3 not installed. Run: pip install boto3") from err

        # Validate the file exists before attempting network I/O.
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        # Fail before any network I/O, and before the job is billed or any object exists.
        bucket = self._configured_bucket()

        filename = os.path.basename(audio_path)
        t_start = time.time()
        logger.info(
            "AWS Transcribe start: file=%s model=%s diarize=%s lang=%s",
            filename,
            self._model_name,
            config.enable_diarization,
            config.language,
        )

        s3 = self._boto_client("s3")
        tc = self._boto_client("transcribe")
        owner = self._bucket_owner()

        if progress_callback:
            progress_callback(0.1, "Uploading to S3 for AWS Transcribe…")

        self._ensure_bucket(s3, bucket, owner)

        job_name = f"opentranscribe-{uuid.uuid4().hex}"
        s3_key = f"jobs/{job_name}.audio"
        result_key = f"jobs/{job_name}-result.json"

        try:
            s3.upload_file(audio_path, bucket, s3_key, ExtraArgs={"ExpectedBucketOwner": owner})
            self._start_job(
                tc, self._build_job_request(job_name, bucket, s3_key, result_key, config)
            )
            if progress_callback:
                progress_callback(0.25, "AWS Transcribe job running…")
            self._poll_job(tc, job_name, progress_callback)
            result_obj = s3.get_object(Bucket=bucket, Key=result_key, ExpectedBucketOwner=owner)
            data = json.loads(result_obj["Body"].read())
        except ClientError as exc:
            self._raise_if_throttled(exc, filename)
            raise
        finally:
            self._cleanup_s3_objects(s3, bucket, owner, [s3_key, result_key])

        elapsed_ms = (time.time() - t_start) * 1000
        logger.info("AWS Transcribe complete: file=%s duration_ms=%.0f", filename, elapsed_ms)

        if progress_callback:
            progress_callback(0.9, "Parsing AWS Transcribe results…")

        tr = data.get("results", {})
        if config.enable_diarization and "speaker_labels" in tr:
            has_speakers = True
            segments = self._segments_with_speakers(tr)
        else:
            has_speakers = False
            segments = self._segments_by_silence_gap(tr)

        if progress_callback:
            progress_callback(1.0, "AWS Transcribe complete")

        return ASRResult(
            segments=segments,
            language=tr.get("language_code", config.language),
            has_speakers=has_speakers,
            provider_name="aws",
            model_name=self._model_name,
        )

    def _ensure_bucket(self, s3: Any, bucket: str, owner: str) -> None:
        """Create the scratch bucket only when HeadBucket reports it is missing."""
        from botocore.exceptions import ClientError

        try:
            s3.head_bucket(Bucket=bucket, ExpectedBucketOwner=owner)
        except Exception as head_exc:
            # HeadBucket is a HEAD request — S3 returns no body on failure, so
            # botocore cannot parse a named error code like "NoSuchBucket" and
            # fabricates the Code from the HTTP status instead. Per AWS's
            # documented HeadBucket behavior a missing bucket is an unambiguous
            # 404; anything else (403 AccessDenied, throttling, etc.) is a real
            # error and must propagate rather than be masked by a create_bucket
            # attempt.
            not_found_codes = {"404", "NoSuchBucket", "NotFound"}
            is_not_found = (
                isinstance(head_exc, ClientError)
                and str(head_exc.response.get("Error", {}).get("Code", "")) in not_found_codes
            )
            if not is_not_found:
                raise
            if self._region == "us-east-1":
                s3.create_bucket(Bucket=bucket)
            else:
                s3.create_bucket(
                    Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": self._region}
                )

    def _build_job_request(
        self, job_name: str, bucket: str, s3_key: str, result_key: str, config: ASRConfig
    ) -> dict:
        """Assemble the Start(Medical)TranscriptionJob keyword arguments."""
        # StartMedicalTranscriptionJob names the job MedicalTranscriptionJobName.
        name_key = (
            "MedicalTranscriptionJobName"
            if self._model_name == "medical"
            else "TranscriptionJobName"
        )
        job_kw: dict = {
            name_key: job_name,
            "Media": {"MediaFileUri": f"s3://{bucket}/{s3_key}"},
            "OutputBucketName": bucket,
            "OutputKey": result_key,
        }
        # LanguageCode, IdentifyLanguage, and IdentifyMultipleLanguages are mutually
        # exclusive — set exactly one. The "multilingual" model transcribes a file that
        # mixes languages (code-switching); otherwise use the explicit language, or
        # auto-detect a single dominant language.
        if self._model_name == "multilingual":
            job_kw["IdentifyMultipleLanguages"] = True
        else:
            aws_lang = _aws_language_code(config.language)
            if aws_lang:
                job_kw["LanguageCode"] = aws_lang
            elif self._model_name == "medical":
                # StartMedicalTranscriptionJob requires LanguageCode and has no
                # IdentifyLanguage; Amazon Transcribe Medical supports en-US only, so
                # "auto" has exactly one possible answer.
                job_kw["LanguageCode"] = "en-US"
            else:
                job_kw["IdentifyLanguage"] = True
        settings_kw = self._job_settings(config)
        if settings_kw:
            job_kw["Settings"] = settings_kw
        if self._model_name == "medical":
            job_kw["Specialty"] = "PRIMARYCARE"
            job_kw["Type"] = "CONVERSATION"
        return job_kw

    @staticmethod
    def _job_settings(config: ASRConfig) -> dict:
        settings_kw: dict = {}
        if config.enable_diarization:
            settings_kw["ShowSpeakerLabels"] = True
            # AWS Transcribe supports up to 30 speakers (per the diarization docs).
            settings_kw["MaxSpeakerLabels"] = max(2, min(config.max_speakers, 30))
        if config.vocabulary:
            # AWS Transcribe requires a pre-created custom vocabulary by name.
            vocab_name = env_str("AWS_TRANSCRIBE_VOCABULARY_NAME")
            if vocab_name:
                settings_kw["VocabularyName"] = vocab_name
            else:
                logger.warning(
                    "config.vocabulary provided but AWS_TRANSCRIBE_VOCABULARY_NAME is not set. "
                    "AWS Transcribe requires a pre-created custom vocabulary. "
                    "Create one in the AWS console and set AWS_TRANSCRIBE_VOCABULARY_NAME."
                )
        return settings_kw

    def _start_job(self, tc: Any, job_kw: dict) -> None:
        if self._model_name == "medical":
            tc.start_medical_transcription_job(**job_kw)
        else:
            tc.start_transcription_job(**job_kw)

    def _poll_job(
        self,
        tc: Any,
        job_name: str,
        progress_callback: Callable[[float, str], None] | None,
    ) -> None:
        """Poll until COMPLETED; raise on FAILED or after the 7200 s budget."""
        elapsed = 0
        status = ""
        while elapsed < 7200:
            time.sleep(15)
            elapsed += 15
            if self._model_name == "medical":
                r = tc.get_medical_transcription_job(MedicalTranscriptionJobName=job_name)
                # The medical job's status field is TranscriptionJobStatus (botocore
                # MedicalTranscriptionJob shape); there is no MedicalTranscriptionJobStatus.
                status = r["MedicalTranscriptionJob"].get("TranscriptionJobStatus", "")
            else:
                r = tc.get_transcription_job(TranscriptionJobName=job_name)
                status = r["TranscriptionJob"].get("TranscriptionJobStatus", "")
            if status == "COMPLETED":
                return
            if status == "FAILED":
                failure_reason = r.get(
                    "TranscriptionJob", r.get("MedicalTranscriptionJob", {})
                ).get("FailureReason", "unknown reason")
                raise RuntimeError(
                    f"AWS Transcribe job failed: {self._sanitize_error(str(failure_reason))}"
                )
            if progress_callback:
                progress_callback(0.25 + min(elapsed / 7200, 0.5), "AWS Transcribe processing…")
        raise RuntimeError("AWS Transcribe job timed out after 7200 seconds")

    def _raise_if_throttled(self, exc: Any, filename: str) -> None:
        # Everything in transcribe()'s try block (S3 upload, job start, polling, result
        # fetch) can throttle. Classify only the throttle codes; every other ClientError
        # propagates unchanged (fail-closed — see app/services/asr/errors.py).
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in _AWS_THROTTLE_CODES:
            from .errors import ASRRateLimitedError

            sanitized = self._sanitize_error(str(exc))
            logger.warning("AWS Transcribe rate-limited for file=%s: %s", filename, sanitized)
            raise ASRRateLimitedError(
                f"AWS Transcribe rate limited: {sanitized}",
                provider="aws",
            ) from exc

    @staticmethod
    def _cleanup_s3_objects(s3: Any, bucket: str, owner: str, keys: list[str]) -> None:
        """Always clean up S3 objects, even on failure."""
        for key in keys:
            try:
                s3.delete_object(Bucket=bucket, Key=key, ExpectedBucketOwner=owner)
            except Exception:  # per-object isolation: clean up the rest regardless
                logger.warning(
                    "Failed to clean up S3 object s3://%s/%s", bucket, key, exc_info=True
                )

    @staticmethod
    def _word_from_item(item: dict) -> ASRWord:
        st = float(item.get("start_time", 0))
        et = float(item.get("end_time", st))
        alt = item["alternatives"][0]
        conf_raw = alt.get("confidence", "1.0")
        conf = float(conf_raw) if conf_raw not in (None, "") else 1.0
        return ASRWord(alt["content"], st, et, conf)

    @staticmethod
    def _with_punctuation(word: ASRWord, item: dict) -> ASRWord:
        """Punctuation items carry no timestamps; glue them onto the preceding word."""
        punct = item["alternatives"][0]["content"]
        return ASRWord(word.word + punct, word.start, word.end, word.confidence)

    def _segments_with_speakers(self, tr: dict) -> list[ASRSegment]:
        # Build start_time → speaker_label map from the speaker_labels block.
        # Each entry in speaker_labels.segments[].items[] carries the item's
        # start_time and inherits the speaker_label from the parent segment.
        # Key on the parsed float, not the raw JSON string — AWS's two
        # blocks (items[] and speaker_labels[]) aren't guaranteed to
        # serialize the same start_time with identical trailing-zero
        # precision (e.g. "2.0" vs "2.000"), and a raw-string lookup treats
        # that formatting drift as a genuine mismatch, silently falling
        # back to the previous word's speaker instead of the true one.
        spk_map: dict = {}
        for seg in tr["speaker_labels"].get("segments", []):
            for sl_item in seg.get("items", []):
                spk_map[float(sl_item["start_time"])] = seg["speaker_label"]

        segments: list[ASRSegment] = []
        cur_spk = None
        cur_words: list[ASRWord] = []
        cur_start = 0.0

        for item in tr.get("items", []):
            if item["type"] != "pronunciation":
                if cur_words and item["type"] == "punctuation":
                    cur_words[-1] = self._with_punctuation(cur_words[-1], item)
                continue
            st = item.get("start_time", "0")
            spk = spk_map.get(float(st), cur_spk)
            if spk != cur_spk:
                if cur_words:
                    segments.append(self._speaker_segment(cur_words, cur_start, cur_spk))
                cur_spk, cur_words, cur_start = spk, [], float(st)
            cur_words.append(self._word_from_item(item))

        if cur_words:
            segments.append(self._speaker_segment(cur_words, cur_start, cur_spk))
        return segments

    def _speaker_segment(self, words: list[ASRWord], start: float, speaker: Any) -> ASRSegment:
        return ASRSegment(
            text=" ".join(w.word for w in words),
            start=start,
            end=words[-1].end,
            speaker=self._normalize_speaker_label(speaker),
            confidence=mean_word_confidence(words),
            words=words,
        )

    def _segments_by_silence_gap(self, tr: dict) -> list[ASRSegment]:
        """No diarization: split the word stream on silence gaps or max duration."""
        all_words: list[ASRWord] = []
        for item in tr.get("items", []):
            if item["type"] == "pronunciation":
                all_words.append(self._word_from_item(item))
            elif item["type"] == "punctuation" and all_words:
                all_words[-1] = self._with_punctuation(all_words[-1], item)

        if not all_words:
            return [ASRSegment(text="", start=0.0, end=0.0)]

        segments: list[ASRSegment] = []
        cur_chunk: list[ASRWord] = [all_words[0]]
        for w in all_words[1:]:
            gap = w.start - cur_chunk[-1].end
            duration = cur_chunk[-1].end - cur_chunk[0].start
            if gap >= ASR_SEGMENT_MIN_GAP_SECONDS or duration >= ASR_SEGMENT_MAX_DURATION_SECONDS:
                segments.append(self._chunk_segment(cur_chunk))
                cur_chunk = [w]
            else:
                cur_chunk.append(w)
        segments.append(self._chunk_segment(cur_chunk))
        return segments

    @staticmethod
    def _chunk_segment(chunk: list[ASRWord]) -> ASRSegment:
        return ASRSegment(
            text=" ".join(x.word for x in chunk),
            start=chunk[0].start,
            end=chunk[-1].end,
            confidence=mean_word_confidence(chunk),
            words=list(chunk),
        )
