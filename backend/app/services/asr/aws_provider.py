"""Amazon Transcribe ASR provider.

Targets boto3 >= 1.26.0.  Supports Standard and Medical transcription
(HIPAA-eligible).

S3 lifecycle notes
------------------
Audio and result files are uploaded to a temporary S3 bucket and cleaned up
after the job completes.  On failure the cleanup is still attempted so stale
objects are not left behind in S3.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Callable

from .base import ASRProvider
from .types import ASRConfig
from .types import ASRResult
from .types import ASRSegment
from .types import ASRWord

logger = logging.getLogger(__name__)

# Segment-grouping thresholds (no-diarization path).
_MAX_DUR = 30.0  # seconds — max segment duration before a forced split
_MIN_GAP = 0.5  # seconds — silence gap that triggers a new segment

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

    def _boto_client(self, service: str):
        import boto3

        kw: dict = {"region_name": self._region}
        if self._access_key_id and self._secret_access_key:
            kw["aws_access_key_id"] = self._access_key_id
            kw["aws_secret_access_key"] = self._secret_access_key
        return boto3.client(service, **kw)

    def validate_connection(self) -> tuple[bool, str, float]:
        """Test credentials by listing transcription jobs (MaxResults=1)."""
        start = time.time()
        try:
            import boto3  # noqa: F401
        except ImportError:
            return False, "boto3 not installed. Run: pip install boto3", 0.0
        try:
            self._boto_client("transcribe").list_transcription_jobs(MaxResults=1)
            ms = (time.time() - start) * 1000
            return True, f"AWS Transcribe validated (region: {self._region})", ms
        except Exception as e:
            ms = (time.time() - start) * 1000
            # Sanitize error — boto3 exceptions may reflect back credential values
            # in debug/error strings in some SDK versions.
            sanitized = self._sanitize_error(str(e), self._secret_access_key)
            sanitized = self._sanitize_error(sanitized, self._access_key_id)
            return False, sanitized, ms

    def transcribe(  # noqa: C901
        self,
        audio_path: str,
        config: ASRConfig,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> ASRResult:
        try:
            import boto3  # noqa: F401
        except ImportError as err:
            raise RuntimeError("boto3 not installed. Run: pip install boto3") from err

        # Validate the file exists before attempting network I/O.
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

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
        bucket = os.getenv("AWS_TRANSCRIBE_BUCKET", f"opentranscribe-tmp-{self._region}")

        if progress_callback:
            progress_callback(0.1, "Uploading to S3 for AWS Transcribe…")

        try:
            s3.head_bucket(Bucket=bucket)
        except Exception:
            if self._region == "us-east-1":
                s3.create_bucket(Bucket=bucket)
            else:
                s3.create_bucket(
                    Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": self._region}
                )

        job_name = f"opentranscribe-{uuid.uuid4().hex}"
        s3_key = f"jobs/{job_name}.audio"
        result_key = f"jobs/{job_name}-result.json"

        try:
            s3.upload_file(audio_path, bucket, s3_key)

            job_kw: dict = {
                "TranscriptionJobName": job_name,
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
                else:
                    job_kw["IdentifyLanguage"] = True
            settings_kw: dict = {}
            if config.enable_diarization:
                settings_kw["ShowSpeakerLabels"] = True
                # AWS Transcribe supports up to 30 speakers (per the diarization docs).
                settings_kw["MaxSpeakerLabels"] = max(2, min(config.max_speakers, 30))
            if config.vocabulary:
                # AWS Transcribe requires a pre-created custom vocabulary by name.
                vocab_name = os.getenv("AWS_TRANSCRIBE_VOCABULARY_NAME")
                if vocab_name:
                    settings_kw["VocabularyName"] = vocab_name
                else:
                    logger.warning(
                        "config.vocabulary provided but AWS_TRANSCRIBE_VOCABULARY_NAME is not set. "
                        "AWS Transcribe requires a pre-created custom vocabulary. "
                        "Create one in the AWS console and set AWS_TRANSCRIBE_VOCABULARY_NAME."
                    )
            if settings_kw:
                job_kw["Settings"] = settings_kw

            if self._model_name == "medical":
                job_kw["Specialty"] = "PRIMARYCARE"
                job_kw["Type"] = "CONVERSATION"
                tc.start_medical_transcription_job(**job_kw)
            else:
                tc.start_transcription_job(**job_kw)

            if progress_callback:
                progress_callback(0.25, "AWS Transcribe job running…")

            elapsed = 0
            status = ""
            while elapsed < 7200:
                time.sleep(15)
                elapsed += 15
                if self._model_name == "medical":
                    r = tc.get_medical_transcription_job(MedicalTranscriptionJobName=job_name)
                    status = r["MedicalTranscriptionJob"].get("MedicalTranscriptionJobStatus", "")
                else:
                    r = tc.get_transcription_job(TranscriptionJobName=job_name)
                    status = r["TranscriptionJob"].get("TranscriptionJobStatus", "")
                if status == "COMPLETED":
                    break
                if status == "FAILED":
                    failure_reason = r.get(
                        "TranscriptionJob", r.get("MedicalTranscriptionJob", {})
                    ).get("FailureReason", "unknown reason")
                    raise RuntimeError(f"AWS Transcribe job failed: {failure_reason}")
                if progress_callback:
                    progress_callback(0.25 + min(elapsed / 7200, 0.5), "AWS Transcribe processing…")

            if status != "COMPLETED":
                raise RuntimeError("AWS Transcribe job timed out after 7200 seconds")

            data = json.loads(s3.get_object(Bucket=bucket, Key=result_key)["Body"].read())

        finally:
            # Always clean up S3 objects, even on failure.
            for key in [s3_key, result_key]:
                try:
                    s3.delete_object(Bucket=bucket, Key=key)
                except Exception as del_exc:
                    logger.warning(
                        "Failed to clean up S3 object s3://%s/%s: %s", bucket, key, del_exc
                    )

        elapsed_ms = (time.time() - t_start) * 1000
        logger.info("AWS Transcribe complete: file=%s duration_ms=%.0f", filename, elapsed_ms)

        if progress_callback:
            progress_callback(0.9, "Parsing AWS Transcribe results…")

        tr = data.get("results", {})
        segments: list[ASRSegment] = []
        has_speakers = False

        if config.enable_diarization and "speaker_labels" in tr:
            has_speakers = True
            items = tr.get("items", [])
            # Build start_time → speaker_label map from the speaker_labels block.
            # Each entry in speaker_labels.segments[].items[] carries the item's
            # start_time and inherits the speaker_label from the parent segment.
            spk_map: dict = {}
            for seg in tr["speaker_labels"].get("segments", []):
                for sl_item in seg.get("items", []):
                    spk_map[sl_item["start_time"]] = seg["speaker_label"]

            cur_spk = None
            cur_words: list[ASRWord] = []
            cur_start = 0.0

            for item in items:
                if item["type"] != "pronunciation":
                    # Punctuation items have no timestamps; attach to the last word.
                    if cur_words and item["type"] == "punctuation":
                        punct = item["alternatives"][0]["content"]
                        cur_words[-1] = ASRWord(
                            cur_words[-1].word + punct,
                            cur_words[-1].start,
                            cur_words[-1].end,
                            cur_words[-1].confidence,
                        )
                    continue
                st = item.get("start_time", "0")
                word = item["alternatives"][0]["content"]
                conf_raw = item["alternatives"][0].get("confidence", "1.0")
                conf = float(conf_raw) if conf_raw not in (None, "") else 1.0
                spk = spk_map.get(st, cur_spk)
                end_s = float(item.get("end_time", st))

                if spk != cur_spk:
                    if cur_words:
                        avg_conf = sum(w.confidence for w in cur_words) / len(cur_words)
                        segments.append(
                            ASRSegment(
                                text=" ".join(w.word for w in cur_words),
                                start=cur_start,
                                end=cur_words[-1].end,
                                speaker=self._normalize_speaker_label(cur_spk),
                                confidence=avg_conf,
                                words=cur_words,
                            )
                        )
                    cur_spk, cur_words, cur_start = spk, [], float(st)

                cur_words.append(ASRWord(word, float(st), end_s, conf))

            if cur_words:
                avg_conf = sum(w.confidence for w in cur_words) / len(cur_words)
                segments.append(
                    ASRSegment(
                        text=" ".join(w.word for w in cur_words),
                        start=cur_start,
                        end=cur_words[-1].end,
                        speaker=self._normalize_speaker_label(cur_spk),
                        confidence=avg_conf,
                        words=cur_words,
                    )
                )
        else:
            # No diarization — build word list from pronunciation items, attach punctuation,
            # then split into segments on silence gaps or max-duration threshold.
            all_words: list[ASRWord] = []
            for item in tr.get("items", []):
                if item["type"] == "pronunciation":
                    st = float(item.get("start_time", 0))
                    et = float(item.get("end_time", st))
                    conf_raw = item["alternatives"][0].get("confidence", "1.0")
                    conf = float(conf_raw) if conf_raw not in (None, "") else 1.0
                    all_words.append(ASRWord(item["alternatives"][0]["content"], st, et, conf))
                elif item["type"] == "punctuation" and all_words:
                    punct = item["alternatives"][0]["content"]
                    all_words[-1] = ASRWord(
                        all_words[-1].word + punct,
                        all_words[-1].start,
                        all_words[-1].end,
                        all_words[-1].confidence,
                    )

            if not all_words:
                segments = [ASRSegment(text="", start=0.0, end=0.0)]
            else:
                cur_chunk: list[ASRWord] = [all_words[0]]
                for w in all_words[1:]:
                    gap = w.start - cur_chunk[-1].end
                    duration = cur_chunk[-1].end - cur_chunk[0].start
                    if gap >= _MIN_GAP or duration >= _MAX_DUR:
                        avg_conf = sum(x.confidence for x in cur_chunk) / len(cur_chunk)
                        segments.append(
                            ASRSegment(
                                text=" ".join(x.word for x in cur_chunk),
                                start=cur_chunk[0].start,
                                end=cur_chunk[-1].end,
                                confidence=avg_conf,
                                words=list(cur_chunk),
                            )
                        )
                        cur_chunk = [w]
                    else:
                        cur_chunk.append(w)
                if cur_chunk:
                    avg_conf = sum(x.confidence for x in cur_chunk) / len(cur_chunk)
                    segments.append(
                        ASRSegment(
                            text=" ".join(x.word for x in cur_chunk),
                            start=cur_chunk[0].start,
                            end=cur_chunk[-1].end,
                            confidence=avg_conf,
                            words=list(cur_chunk),
                        )
                    )

        if progress_callback:
            progress_callback(1.0, "AWS Transcribe complete")

        return ASRResult(
            segments=segments,
            language=tr.get("language_code", config.language),
            has_speakers=has_speakers,
            provider_name="aws",
            model_name=self._model_name,
        )
