"""Gladia ASR provider.

Targets Gladia API v2 (REST, no dedicated Python SDK needed — uses requests).
All features (diarization, language detection, custom vocabulary) are included
in the standard tier at a single flat price.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Callable

from .base import ASRProvider
from .types import ASRConfig
from .types import ASRResult
from .types import ASRSegment
from .types import ASRWord

logger = logging.getLogger(__name__)


class GladiaProvider(ASRProvider):
    _BASE = "https://api.gladia.io"

    def __init__(self, api_key: str, model_name: str = "standard"):
        self._api_key = api_key
        self._model_name = model_name

    @property
    def provider_name(self) -> str:
        return "gladia"

    def supports_diarization(self) -> bool:
        return True

    def supports_vocabulary(self) -> bool:
        return True

    def supports_translation(self) -> bool:
        return False

    def _hdr(self) -> dict:
        return {"x-gladia-key": self._api_key, "Content-Type": "application/json"}

    def _err_detail(self, exc: Exception) -> str:
        """Sanitized error including the API response body for HTTP errors."""
        detail = str(exc)
        resp = getattr(exc, "response", None)
        if resp is not None:
            try:
                body = resp.text.strip()
            except Exception:  # noqa: BLE001
                body = ""
            if body:
                detail = f"{detail} — {body[:500]}"
        return self._sanitize_error(detail, self._api_key)

    def validate_connection(self) -> tuple[bool, str, float]:
        """Test API key by hitting the /v2/live endpoint (lightweight, no audio needed)."""
        start = time.time()
        try:
            import requests
        except ImportError:
            return False, "requests not installed. Run: pip install requests", 0.0
        try:
            r = requests.get(f"{self._BASE}/v2/live", headers=self._hdr(), timeout=10)
            ms = (time.time() - start) * 1000
            if r.status_code == 401:
                return False, "Invalid Gladia API key", ms
            return True, f"Gladia reachable (HTTP {r.status_code})", ms
        except Exception as e:
            ms = (time.time() - start) * 1000
            return False, self._sanitize_error(str(e), self._api_key), ms

    def transcribe(  # noqa: C901
        self,
        audio_path: str,
        config: ASRConfig,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> ASRResult:
        import requests

        # Validate the file exists before attempting network I/O.
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        filename = os.path.basename(audio_path)
        t_start = time.time()
        logger.info(
            "Gladia transcribe start: file=%s diarize=%s lang=%s",
            filename,
            config.enable_diarization,
            config.language,
        )

        if progress_callback:
            progress_callback(0.1, "Uploading to Gladia…")

        try:
            import mimetypes

            content_type = mimetypes.guess_type(audio_path)[0] or "application/octet-stream"
            with open(audio_path, "rb") as f:
                # The multipart part MUST carry a filename + content-type, else Gladia
                # rejects with 400 "Missing audio file".
                up = requests.post(
                    f"{self._BASE}/v2/upload",
                    headers={"x-gladia-key": self._api_key},
                    files={"audio": (filename, f, content_type)},
                    timeout=300,
                )
            up.raise_for_status()
        except Exception as exc:
            sanitized = self._err_detail(exc)
            logger.error("Gladia upload failed for file=%s: %s", filename, sanitized)
            raise RuntimeError(f"Gladia upload failed: {sanitized}") from exc

        audio_url = up.json()["audio_url"]

        if progress_callback:
            progress_callback(0.2, "Starting Gladia job…")

        body: dict = {
            "audio_url": audio_url,
            "diarization": config.enable_diarization,
            "detect_language": config.language == "auto",
        }
        if config.language != "auto":
            body["language"] = config.language
        if config.vocabulary:
            body["custom_vocabulary"] = config.vocabulary[:100]

        try:
            job_r = requests.post(
                f"{self._BASE}/v2/transcription", headers=self._hdr(), json=body, timeout=30
            )
            job_r.raise_for_status()
        except Exception as exc:
            sanitized = self._sanitize_error(str(exc), self._api_key)
            logger.error("Gladia job submission failed for file=%s: %s", filename, sanitized)
            raise RuntimeError(f"Gladia job submission failed: {sanitized}") from exc

        result_url = job_r.json().get("result_url")
        if not result_url:
            raise RuntimeError("Gladia did not return a result_url for polling")

        if progress_callback:
            progress_callback(0.3, "Gladia transcription in progress…")

        data: dict = {}
        completed = False
        for i in range(720):
            time.sleep(10)
            try:
                poll = requests.get(result_url, headers=self._hdr(), timeout=30)
                poll.raise_for_status()
                data = poll.json()
            except Exception as exc:
                sanitized = self._sanitize_error(str(exc), self._api_key)
                logger.warning(
                    "Gladia poll error (attempt %d) for file=%s: %s", i, filename, sanitized
                )
                continue
            if data.get("status") == "done":
                completed = True
                break
            if data.get("status") == "error":
                err_msg = self._sanitize_error(
                    str(data.get("error_message", "unknown error")), self._api_key
                )
                logger.error("Gladia job error for file=%s: %s", filename, err_msg)
                raise RuntimeError(f"Gladia error: {err_msg}")
            if progress_callback:
                progress_callback(0.3 + min(i / 720, 0.5), "Gladia processing…")

        if not completed:
            raise RuntimeError("Gladia transcription timed out after 7200 seconds")

        elapsed_ms = (time.time() - t_start) * 1000
        logger.info("Gladia transcribe complete: file=%s duration_ms=%.0f", filename, elapsed_ms)

        if progress_callback:
            progress_callback(0.9, "Parsing Gladia results…")

        utts = data.get("result", {}).get("transcription", {}).get("utterances", [])
        segments = [
            ASRSegment(
                text=u.get("text", ""),
                start=u.get("start", 0.0),
                end=u.get("end", 0.0),
                speaker=self._normalize_speaker_label(u.get("speaker"))
                if u.get("speaker") is not None
                else None,
                confidence=u.get("confidence"),
                words=[
                    ASRWord(
                        w.get("word", ""),
                        w.get("start", 0.0),
                        w.get("end", 0.0),
                        w.get("confidence", 1.0),
                    )
                    for w in u.get("words", [])
                ],
            )
            for u in utts
        ]

        langs = data.get("result", {}).get("transcription", {}).get("languages", [])
        detected = langs[0] if langs else config.language

        if progress_callback:
            progress_callback(1.0, "Gladia transcription complete")

        return ASRResult(
            segments=segments,
            language=detected,
            has_speakers=config.enable_diarization and any(s.speaker for s in segments),
            provider_name="gladia",
            model_name=self._model_name,
        )
