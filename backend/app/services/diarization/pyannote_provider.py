"""pyannote.ai cloud diarization provider.

Uses the pyannote.ai REST API (v1) to perform speaker diarization on audio
files.  The flow is: upload audio via pre-signed URL, submit a diarization
job, poll for completion, and parse the resulting speaker segments.

API reference: https://api.pyannote.ai/docs
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import Callable

import httpx

from .base import DiarizationProvider
from .types import DiarizeConfig
from .types import DiarizeResult
from .types import DiarizeSegment

logger = logging.getLogger(__name__)

_API_BASE = "https://api.pyannote.ai"

# Per-request timeout for individual HTTP calls (seconds).
_HTTP_TIMEOUT = 30.0

# Polling configuration for job completion.
_POLL_INTERVAL_S = 2.0
_POLL_MAX_ATTEMPTS = 150  # 150 * 2s = 5 minutes

# pyannote.ai job terminal statuses.
_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "canceled"})


class PyAnnoteCloudDiarizationProvider(DiarizationProvider):
    """pyannote.ai cloud diarization provider.

    Implements the full diarization flow:
    1. Get a pre-signed upload URL from ``/v1/media/input``
    2. PUT audio bytes to the pre-signed URL
    3. Submit a diarization job via ``POST /v1/diarize``
    4. Poll ``GET /v1/jobs/{jobId}`` until terminal status
    5. Parse ``output.diarization`` into ``DiarizeSegment`` list
    """

    def __init__(self, api_key: str, model_name: str = "precision-2") -> None:
        self._api_key = api_key
        self._model_name = model_name

    @property
    def provider_name(self) -> str:
        return "pyannote"

    def supports_speaker_count(self) -> bool:
        return True

    # -- Connection validation ---------------------------------------------------

    def validate_connection(self) -> tuple[bool, str, float]:
        """Validate the API key by calling ``GET /v1/test``.

        Returns:
            Tuple of (success, message, response_time_ms).
        """
        start = time.time()

        if not self._api_key:
            ms = (time.time() - start) * 1000
            return False, "API key is required for pyannote.ai", ms

        try:
            resp = httpx.get(
                f"{_API_BASE}/v1/test",
                headers=self._auth_headers(),
                timeout=_HTTP_TIMEOUT,
            )
            ms = (time.time() - start) * 1000

            if resp.status_code == 200:
                return True, "Connected to pyannote.ai", ms
            if resp.status_code == 401:
                return False, "Invalid pyannote.ai API key", ms
            return False, f"pyannote.ai returned HTTP {resp.status_code}", ms
        except Exception as exc:
            ms = (time.time() - start) * 1000
            sanitized = self._sanitize_error(str(exc), self._api_key)
            return False, f"Connection failed: {sanitized}", ms

    # -- Main diarization flow ---------------------------------------------------

    def diarize(
        self,
        audio_path: str,
        config: DiarizeConfig,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> DiarizeResult:
        """Run cloud diarization via pyannote.ai.

        Args:
            audio_path: Path to audio file on disk (WAV, MP3, etc.).
            config: Diarization configuration (speaker counts, API key).
            progress_callback: Optional ``(fraction, message)`` callback.

        Returns:
            DiarizeResult with normalised SPEAKER_XX segments.

        Raises:
            FileNotFoundError: If *audio_path* does not exist.
            RuntimeError: On upload failure, job failure, or timeout.
        """
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        api_key = config.api_key or self._api_key
        if not api_key:
            raise RuntimeError("pyannote.ai API key is required for cloud diarization")

        model = config.model_name or self._model_name
        filename = os.path.basename(audio_path)
        t_start = time.time()

        logger.info(
            "pyannote.ai diarize start: file=%s model=%s",
            filename,
            model,
        )

        # Step 1 — Generate a unique media object key and get upload URL.
        if progress_callback:
            progress_callback(0.05, "Requesting upload URL from pyannote.ai...")

        object_key = f"media://diarize/{uuid.uuid4()}.wav"
        upload_url = self._get_upload_url(object_key, api_key)

        # Step 2 — Upload the audio file.
        if progress_callback:
            progress_callback(0.10, "Uploading audio to pyannote.ai...")

        self._upload_audio(audio_path, upload_url, api_key)

        logger.info("pyannote.ai upload complete: file=%s key=%s", filename, object_key)

        # Step 3 — Submit diarization job.
        if progress_callback:
            progress_callback(0.25, "Submitting diarization job...")

        job_id = self._submit_job(object_key, model, config, api_key)

        logger.info("pyannote.ai job submitted: jobId=%s file=%s", job_id, filename)

        # Step 4 — Poll for completion.
        if progress_callback:
            progress_callback(0.30, "Waiting for diarization to complete...")

        job_output = self._poll_job(job_id, api_key, progress_callback)

        # Step 5 — Parse diarization segments.
        if progress_callback:
            progress_callback(0.95, "Parsing diarization results...")

        segments = self._parse_segments(job_output)

        speaker_set = {s.speaker for s in segments}
        elapsed_ms = (time.time() - t_start) * 1000

        logger.info(
            "pyannote.ai diarize complete: file=%s jobId=%s "
            "speakers=%d segments=%d duration_ms=%.0f",
            filename,
            job_id,
            len(speaker_set),
            len(segments),
            elapsed_ms,
        )

        if progress_callback:
            progress_callback(1.0, "pyannote.ai diarization complete")

        return DiarizeResult(
            segments=segments,
            num_speakers=len(speaker_set),
            provider_name="pyannote",
            model_name=model,
            metadata={"job_id": job_id, "elapsed_ms": round(elapsed_ms, 1)},
        )

    # -- Internal helpers --------------------------------------------------------

    def _auth_headers(self, api_key: str | None = None) -> dict[str, str]:
        """Build Authorization header dict."""
        key = api_key or self._api_key
        return {"Authorization": f"Bearer {key}"}

    def _get_upload_url(self, object_key: str, api_key: str) -> str:
        """Request a pre-signed upload URL from ``POST /v1/media/input``.

        Args:
            object_key: The ``media://...`` object key.
            api_key: Bearer token.

        Returns:
            Pre-signed PUT URL string.

        Raises:
            RuntimeError: If the request fails.
        """
        try:
            resp = httpx.post(
                f"{_API_BASE}/v1/media/input",
                headers=self._auth_headers(api_key),
                json={"url": object_key},
                timeout=_HTTP_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            sanitized = self._sanitize_error(str(exc), api_key)
            raise RuntimeError(f"pyannote.ai: failed to get upload URL: {sanitized}") from exc

        if resp.status_code == 401:
            raise RuntimeError("pyannote.ai: invalid API key (401 Unauthorized)")

        if resp.status_code not in (200, 201):
            body = self._safe_response_text(resp, api_key)
            raise RuntimeError(
                f"pyannote.ai: /v1/media/input returned HTTP {resp.status_code}: {body}"
            )

        data = resp.json()
        url: str = data.get("url", "")
        if not url:
            raise RuntimeError("pyannote.ai: /v1/media/input response missing 'url' field")
        return url

    def _upload_audio(self, audio_path: str, upload_url: str, api_key: str) -> None:
        """Upload audio file bytes to the pre-signed URL via PUT.

        Args:
            audio_path: Local path to the audio file.
            upload_url: Pre-signed PUT URL from ``/v1/media/input``.
            api_key: Used only for error sanitization.

        Raises:
            RuntimeError: If the upload fails.
        """
        file_size = os.path.getsize(audio_path)
        logger.debug(
            "pyannote.ai uploading %s (%.1f MB)",
            os.path.basename(audio_path),
            file_size / (1024 * 1024),
        )

        try:
            with open(audio_path, "rb") as f:
                resp = httpx.put(
                    upload_url,
                    content=f.read(),
                    headers={"Content-Type": "application/octet-stream"},
                    timeout=max(_HTTP_TIMEOUT, 120.0),  # Allow more time for large files
                )
        except httpx.HTTPError as exc:
            sanitized = self._sanitize_error(str(exc), api_key)
            raise RuntimeError(f"pyannote.ai: audio upload failed: {sanitized}") from exc

        if resp.status_code not in (200, 201):
            body = self._safe_response_text(resp, api_key)
            raise RuntimeError(
                f"pyannote.ai: audio upload returned HTTP {resp.status_code}: {body}"
            )

    def _submit_job(
        self,
        object_key: str,
        model: str,
        config: DiarizeConfig,
        api_key: str,
    ) -> str:
        """Submit a diarization job via ``POST /v1/diarize``.

        Args:
            object_key: The ``media://...`` key of the uploaded audio.
            model: Diarization model name (e.g. ``precision-2``).
            config: Diarization config with speaker count hints.
            api_key: Bearer token.

        Returns:
            The ``jobId`` string.

        Raises:
            RuntimeError: If the submission fails.
        """
        body: dict = {
            "url": object_key,
            "model": model,
            "transcription": False,
        }

        # Speaker count hints — numSpeakers takes priority over min/max.
        if config.num_speakers is not None:
            body["numSpeakers"] = config.num_speakers
        else:
            if config.min_speakers > 1:
                body["minSpeakers"] = config.min_speakers
            if config.max_speakers < 50:
                body["maxSpeakers"] = config.max_speakers

        try:
            resp = httpx.post(
                f"{_API_BASE}/v1/diarize",
                headers=self._auth_headers(api_key),
                json=body,
                timeout=_HTTP_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            sanitized = self._sanitize_error(str(exc), api_key)
            raise RuntimeError(
                f"pyannote.ai: failed to submit diarization job: {sanitized}"
            ) from exc

        if resp.status_code == 401:
            raise RuntimeError("pyannote.ai: invalid API key (401 Unauthorized)")

        if resp.status_code not in (200, 201):
            body_text = self._safe_response_text(resp, api_key)
            raise RuntimeError(
                f"pyannote.ai: /v1/diarize returned HTTP {resp.status_code}: {body_text}"
            )

        data = resp.json()
        job_id: str = data.get("jobId", "")
        if not job_id:
            raise RuntimeError("pyannote.ai: /v1/diarize response missing 'jobId' field")
        return job_id

    def _poll_job(
        self,
        job_id: str,
        api_key: str,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> dict:
        """Poll ``GET /v1/jobs/{jobId}`` until the job reaches a terminal status.

        Args:
            job_id: The job identifier from ``/v1/diarize``.
            api_key: Bearer token.
            progress_callback: Optional progress reporter.

        Returns:
            The ``output`` dict from the final job response.

        Raises:
            RuntimeError: On job failure, cancellation, or timeout.
        """
        url = f"{_API_BASE}/v1/jobs/{job_id}"

        for attempt in range(1, _POLL_MAX_ATTEMPTS + 1):
            try:
                resp = httpx.get(
                    url,
                    headers=self._auth_headers(api_key),
                    timeout=_HTTP_TIMEOUT,
                )
            except httpx.HTTPError as exc:
                # Transient network errors during polling — log and retry.
                sanitized = self._sanitize_error(str(exc), api_key)
                logger.warning(
                    "pyannote.ai: poll attempt %d/%d network error: %s",
                    attempt,
                    _POLL_MAX_ATTEMPTS,
                    sanitized,
                )
                time.sleep(_POLL_INTERVAL_S)
                continue

            if resp.status_code == 401:
                raise RuntimeError("pyannote.ai: invalid API key (401 Unauthorized)")

            if resp.status_code != 200:
                # Non-200 during poll — log and retry (may be transient).
                logger.warning(
                    "pyannote.ai: poll attempt %d/%d returned HTTP %d",
                    attempt,
                    _POLL_MAX_ATTEMPTS,
                    resp.status_code,
                )
                time.sleep(_POLL_INTERVAL_S)
                continue

            data = resp.json()
            status = data.get("status", "unknown")

            if status in _TERMINAL_STATUSES:
                output = data.get("output", {})
                return self._handle_terminal_status(status, output, job_id)

            # Report progress — map polling position to [0.30, 0.90] range.
            if progress_callback and attempt % 5 == 0:
                fraction = 0.30 + (attempt / _POLL_MAX_ATTEMPTS) * 0.60
                fraction = min(fraction, 0.90)
                progress_callback(fraction, f"Diarization in progress (status: {status})...")

            time.sleep(_POLL_INTERVAL_S)

        # Exhausted all polling attempts.
        max_seconds = int(_POLL_MAX_ATTEMPTS * _POLL_INTERVAL_S)
        raise RuntimeError(
            f"pyannote.ai: diarization job {job_id} did not complete within "
            f"{max_seconds}s ({_POLL_MAX_ATTEMPTS} polls)"
        )

    def _handle_terminal_status(self, status: str, output: dict, job_id: str) -> dict:
        """Validate the terminal job status and return the output dict.

        Args:
            status: Terminal status string.
            output: The ``output`` dict from the job response.
            job_id: Job identifier for error messages.

        Returns:
            The ``output`` dict on success.

        Raises:
            RuntimeError: If the job failed or was canceled.
        """
        if status == "succeeded":
            # Check for warnings even on success.
            warning = output.get("warning")
            if warning:
                logger.warning("pyannote.ai job %s warning: %s", job_id, warning)
            return output

        if status == "failed":
            error_msg = output.get("error") or "unknown error"
            raise RuntimeError(f"pyannote.ai: diarization job {job_id} failed: {error_msg}")

        if status == "canceled":
            raise RuntimeError(f"pyannote.ai: diarization job {job_id} was canceled")

        # Should not happen given _TERMINAL_STATUSES, but guard anyway.
        raise RuntimeError(
            f"pyannote.ai: diarization job {job_id} reached unexpected terminal status: {status}"
        )

    def _parse_segments(self, output: dict) -> list[DiarizeSegment]:
        """Parse ``output.diarization`` into a list of DiarizeSegment.

        Args:
            output: The ``output`` dict from a succeeded job.

        Returns:
            List of DiarizeSegment with normalised speaker labels, sorted
            by start time.

        Raises:
            RuntimeError: If no diarization data is present.
        """
        raw_segments = output.get("diarization")
        if not raw_segments:
            raise RuntimeError(
                "pyannote.ai: job succeeded but response contains no diarization segments"
            )

        segments: list[DiarizeSegment] = []
        for entry in raw_segments:
            speaker_raw = entry.get("speaker")
            start = entry.get("start")
            end = entry.get("end")

            if start is None or end is None:
                logger.warning("pyannote.ai: skipping segment with missing timestamps: %s", entry)
                continue

            speaker = self._normalize_speaker_label(speaker_raw) or "SPEAKER_00"

            segments.append(
                DiarizeSegment(
                    start=float(start),
                    end=float(end),
                    speaker=speaker,
                )
            )

        # Sort by start time for deterministic output.
        segments.sort(key=lambda s: s.start)

        if not segments:
            raise RuntimeError(
                "pyannote.ai: job succeeded but all diarization segments had invalid data"
            )

        return segments

    def _safe_response_text(self, resp: httpx.Response, api_key: str) -> str:
        """Extract response body text, sanitizing any credentials.

        Truncates long responses to avoid log pollution.
        """
        try:
            text = resp.text[:500]
        except Exception:
            text = "<unreadable response body>"
        return self._sanitize_error(text, api_key)
