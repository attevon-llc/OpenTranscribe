"""pyannote.ai STT Orchestration ASR provider.

pyannote.ai offers premium diarization bundled with transcription (Nvidia Parakeet
or whisper-large-v3-turbo) via their STT Orchestration API.  The provider uploads
audio via a pre-signed URL, submits a diarize+transcribe job, polls for completion,
and normalizes the response into the shared ASR result types.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable

from .base import ASRProvider
from .errors import ASRRateLimitedError
from .errors import http_status_of
from .errors import is_rate_limit_status
from .errors import retry_after_of
from .types import ASRConfig
from .types import ASRResult
from .types import ASRSegment
from .types import ASRWord

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.pyannote.ai"

# Polling configuration.
_POLL_INTERVAL = 2.0  # seconds between polls
_POLL_TIMEOUT = 300.0  # 5 minutes max

# Map user-facing model_name → (diarization model, transcription model).
# Transcription model ids are pyannote.ai's transcriptionConfig.model enum values (verified
# against the live API's 400 validation message — the published docs' "nvidia-" prefix is
# WRONG). Diarization model is precision-2 (the only model that supports transcription).
_MODEL_MAP: dict[str, tuple[str, str]] = {
    "parakeet": ("precision-2", "parakeet-tdt-0.6b-v3"),
    "whisper-large-v3-turbo": ("precision-2", "faster-whisper-large-v3-turbo"),
}


class PyAnnoteProvider(ASRProvider):
    """pyannote.ai STT Orchestration provider.

    Combines premium speaker diarization with transcription (Parakeet or Whisper)
    in a single API call.
    """

    def __init__(self, api_key: str, model_name: str = "parakeet"):
        self._api_key = api_key
        self._model_name = model_name

    @property
    def provider_name(self) -> str:
        return "pyannote"

    def supports_diarization(self) -> bool:
        return True

    def supports_vocabulary(self) -> bool:
        return False

    def supports_translation(self) -> bool:
        return False

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _raise_if_rate_limited(self, exc: Exception, context: str) -> None:
        """Re-raise *exc* as ASRRateLimitedError when it is an HTTP 429, else no-op.

        Used only at the three PRE-poll HTTP call sites (upload-URL request, audio PUT,
        job submission). Deliberately NOT used inside the poll loop: a 429 seen while
        polling an already-submitted job must stay transient (the loop's own retry-and-
        continue is correct there — see the poll loop's ``except`` block and Item 2's
        401/canceled fail-fast rewrite, which this module also carries).
        """
        status = http_status_of(exc)
        if not is_rate_limit_status(status):
            return
        sanitized = self._sanitize_error(str(exc), self._api_key)
        logger.warning("pyannote.ai %s rate-limited: %s", context, sanitized)
        raise ASRRateLimitedError(
            f"pyannote.ai {context} rate limited: {sanitized}",
            provider="pyannote",
            retry_after=retry_after_of(exc),
        ) from exc

    def _err_detail(self, exc: Exception) -> str:
        """Sanitized error string, including the API response body for HTTP errors."""
        import httpx

        detail = str(exc)
        if isinstance(exc, httpx.HTTPStatusError):
            try:
                body = exc.response.text.strip()
            except Exception:  # noqa: BLE001
                body = ""
            if body:
                detail = f"{detail} — {body[:500]}"
        return self._sanitize_error(detail, self._api_key)

    def _resolve_models(self) -> tuple[str, str]:
        """Return (diarization_model, transcription_model) for the configured model_name."""
        if self._model_name in _MODEL_MAP:
            return _MODEL_MAP[self._model_name]
        # Fallback: default to parakeet if the model_name is unrecognised.
        logger.warning("Unknown pyannote.ai model '%s', falling back to parakeet", self._model_name)
        return _MODEL_MAP["parakeet"]

    # ── Connection validation ─────────────────────────────────────────────────

    def validate_connection(self) -> tuple[bool, str, float]:
        """Validate the API key by calling the pyannote.ai /v1/test endpoint."""
        start = time.time()

        if not self._api_key:
            ms = (time.time() - start) * 1000
            return False, "API key is required for pyannote.ai", ms

        try:
            import httpx

            resp = httpx.get(
                f"{_BASE_URL}/v1/test",
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=15.0,
            )
            ms = (time.time() - start) * 1000
            if resp.status_code == 200:
                return True, "Connected to pyannote.ai", ms
            if resp.status_code == 401:
                return False, "Invalid API key", ms
            return False, f"pyannote.ai returned HTTP {resp.status_code}", ms
        except ImportError:
            ms = (time.time() - start) * 1000
            return False, "httpx package not installed (required for pyannote.ai)", ms
        except Exception as exc:
            ms = (time.time() - start) * 1000
            sanitized = self._sanitize_error(str(exc), self._api_key)
            return False, f"Connection failed: {sanitized}", ms

    # ── Transcription ─────────────────────────────────────────────────────────

    def transcribe(  # noqa: C901
        self,
        audio_path: str,
        config: ASRConfig,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> ASRResult:
        """Transcribe audio via pyannote.ai STT Orchestration API.

        Flow:
        1. Request a pre-signed upload URL via POST /v1/media/input
        2. PUT the audio file to the pre-signed URL
        3. Submit a diarize+transcribe job via POST /v1/diarize
        4. Poll GET /v1/jobs/{jobId} until succeeded or failed
        5. Parse turnLevelTranscription + wordLevelTranscription into ASRResult
        """
        import httpx

        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        filename = os.path.basename(audio_path)
        t_start = time.time()
        diarization_model, transcription_model = self._resolve_models()

        logger.info(
            "pyannote.ai transcribe start: file=%s diarization_model=%s "
            "transcription_model=%s lang=%s",
            filename,
            diarization_model,
            transcription_model,
            config.language,
        )

        headers = self._headers()

        # ── Step 1: Get pre-signed upload URL ─────────────────────────────────
        if progress_callback:
            progress_callback(0.05, "Requesting upload URL from pyannote.ai...")

        object_key = f"upload/{int(time.time())}_{filename}"
        media_uri = f"media://{object_key}"

        try:
            resp = httpx.post(
                f"{_BASE_URL}/v1/media/input",
                headers=headers,
                json={"url": media_uri},
                timeout=30.0,
            )
            resp.raise_for_status()
            upload_url = resp.json()["url"]
        except Exception as exc:
            self._raise_if_rate_limited(exc, "upload URL request")
            sanitized = self._sanitize_error(str(exc), self._api_key)
            logger.error(
                "pyannote.ai upload URL request failed for file=%s: %s", filename, sanitized
            )
            raise RuntimeError(f"pyannote.ai upload URL request failed: {sanitized}") from exc

        # ── Step 2: Upload audio to pre-signed URL ────────────────────────────
        if progress_callback:
            progress_callback(0.1, "Uploading audio to pyannote.ai...")

        try:
            with open(audio_path, "rb") as f:
                audio_bytes = f.read()
            put_resp = httpx.put(
                upload_url,
                content=audio_bytes,
                headers={"Content-Type": "application/octet-stream"},
                timeout=300.0,
            )
            put_resp.raise_for_status()
        except Exception as exc:
            self._raise_if_rate_limited(exc, "audio upload")
            sanitized = self._sanitize_error(str(exc), self._api_key)
            logger.error("pyannote.ai audio upload failed for file=%s: %s", filename, sanitized)
            raise RuntimeError(f"pyannote.ai audio upload failed: {sanitized}") from exc

        logger.info("pyannote.ai audio uploaded: file=%s object_key=%s", filename, object_key)

        # ── Step 3: Submit diarize + transcribe job ───────────────────────────
        if progress_callback:
            progress_callback(0.2, "Submitting pyannote.ai transcription job...")

        job_body: dict = {
            "url": media_uri,
            "model": diarization_model,
            "transcription": True,
            "transcriptionConfig": {"model": transcription_model},
            "confidence": True,
        }

        # Speaker count hints — only include if the user configured them.
        if config.num_speakers is not None:
            job_body["numSpeakers"] = config.num_speakers
        else:
            if config.min_speakers > 1:
                job_body["minSpeakers"] = config.min_speakers
            if config.max_speakers < 20:
                job_body["maxSpeakers"] = config.max_speakers

        try:
            resp = httpx.post(
                f"{_BASE_URL}/v1/diarize",
                headers=headers,
                json=job_body,
                timeout=30.0,
            )
            resp.raise_for_status()
            job_data = resp.json()
            job_id = job_data["jobId"]
        except Exception as exc:
            self._raise_if_rate_limited(exc, "job submission")
            sanitized = self._err_detail(exc)
            logger.error("pyannote.ai job submission failed for file=%s: %s", filename, sanitized)
            raise RuntimeError(f"pyannote.ai job submission failed: {sanitized}") from exc

        logger.info("pyannote.ai job submitted: file=%s job_id=%s", filename, job_id)

        # ── Step 4: Poll for completion ───────────────────────────────────────
        if progress_callback:
            progress_callback(0.3, "pyannote.ai transcription in progress...")

        poll_start = time.time()
        result_data: dict = {}

        while True:
            elapsed = time.time() - poll_start
            if elapsed > _POLL_TIMEOUT:
                logger.error(
                    "pyannote.ai job timed out after %.0fs for file=%s job_id=%s",
                    elapsed,
                    filename,
                    job_id,
                )
                raise RuntimeError(
                    f"pyannote.ai transcription timed out after {int(_POLL_TIMEOUT)} seconds"
                )

            time.sleep(_POLL_INTERVAL)

            try:
                poll_resp = httpx.get(
                    f"{_BASE_URL}/v1/jobs/{job_id}",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    timeout=30.0,
                )
                poll_resp.raise_for_status()
                result_data = poll_resp.json()
            except Exception as exc:
                sanitized = self._sanitize_error(str(exc), self._api_key)
                logger.warning(
                    "pyannote.ai poll error (elapsed=%.0fs) for file=%s job_id=%s: %s",
                    elapsed,
                    filename,
                    job_id,
                    sanitized,
                )
                continue

            status = result_data.get("status", "")

            if status == "succeeded":
                break

            if status == "failed":
                error_msg = (
                    result_data.get("output", {}).get("error")
                    or result_data.get("output", {}).get("warning")
                    or "unknown error"
                )
                sanitized = self._sanitize_error(str(error_msg), self._api_key)
                logger.error(
                    "pyannote.ai job failed for file=%s job_id=%s: %s",
                    filename,
                    job_id,
                    sanitized,
                )
                raise RuntimeError(f"pyannote.ai transcription failed: {sanitized}")

            # Update progress during polling (0.3 → 0.85 range).
            if progress_callback:
                poll_progress = min(elapsed / _POLL_TIMEOUT, 1.0)
                progress_callback(
                    0.3 + poll_progress * 0.55,
                    f"pyannote.ai processing ({status})...",
                )

        total_ms = (time.time() - t_start) * 1000
        logger.info(
            "pyannote.ai transcribe complete: file=%s job_id=%s duration_ms=%.0f",
            filename,
            job_id,
            total_ms,
        )

        # ── Step 5: Parse response ────────────────────────────────────────────
        if progress_callback:
            progress_callback(0.9, "Parsing pyannote.ai results...")

        output = result_data.get("output", {})
        segments = self._parse_response(output)

        if progress_callback:
            progress_callback(1.0, "pyannote.ai transcription complete")

        has_speakers = any(s.speaker is not None for s in segments)

        return ASRResult(
            segments=segments,
            language=config.language,
            has_speakers=has_speakers,
            provider_name="pyannote",
            model_name=self._model_name,
        )

    def _parse_response(self, output: dict) -> list[ASRSegment]:
        """Parse pyannote.ai output into ASRSegment list.

        Uses turnLevelTranscription for segments (each entry = one speaker turn)
        and wordLevelTranscription to attach word-level timestamps to each segment.
        Falls back to diarization-only segments if transcription data is missing.
        """
        turns: list[dict] = output.get("turnLevelTranscription") or []
        words_raw: list[dict] = output.get("wordLevelTranscription") or []

        # If no turn-level transcription, try to build from diarization only.
        if not turns:
            diarization: list[dict] = output.get("diarization") or []
            if not diarization:
                return [ASRSegment(text="", start=0.0, end=0.0)]
            return [
                ASRSegment(
                    text="",
                    start=seg.get("start", 0.0),
                    end=seg.get("end", 0.0),
                    speaker=self._normalize_speaker_label(seg.get("speaker")),
                )
                for seg in diarization
            ]

        # Index words by their start time for efficient assignment to turns.
        # pyannote.ai's wordLevelTranscription entries use the key "text" for the token
        # (per the get-job TranscriptionSegment schema); accept "word" as a fallback.
        asr_words = [
            ASRWord(
                word=w.get("text") or w.get("word") or "",
                start=w.get("start", 0.0),
                end=w.get("end", 0.0),
                confidence=w.get("confidence", 1.0),
            )
            for w in words_raw
        ]

        segments: list[ASRSegment] = []

        for turn in turns:
            turn_start = turn.get("start", 0.0)
            turn_end = turn.get("end", 0.0)
            turn_text = turn.get("text", "")
            turn_speaker = turn.get("speaker")

            # Collect words that fall within this turn's time range.
            # A word belongs to a turn if its start time is >= turn_start
            # and < turn_end (with a small tolerance for the last word).
            turn_words = [
                w for w in asr_words if w.start >= turn_start - 0.01 and w.start < turn_end + 0.01
            ]

            # Compute average confidence from words if available.
            avg_confidence: float | None = None
            if turn_words:
                avg_confidence = sum(w.confidence for w in turn_words) / len(turn_words)

            segments.append(
                ASRSegment(
                    text=turn_text,
                    start=turn_start,
                    end=turn_end,
                    speaker=self._normalize_speaker_label(turn_speaker),
                    confidence=avg_confidence,
                    words=turn_words,
                )
            )

        return segments
