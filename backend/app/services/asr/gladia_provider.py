"""Gladia ASR provider.

Targets Gladia API v2 (REST, no dedicated Python SDK needed — uses requests).
All features (diarization, language detection, custom vocabulary) are included
in the standard tier at a single flat price.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable

from app.core.exceptions import ASRConfigurationError

from .base import ASRProvider
from .errors import ASRRateLimitedError
from .errors import retry_after_from_headers
from .types import ASRConfig
from .types import ASRResult
from .types import ASRSegment
from .types import ASRWord

logger = logging.getLogger(__name__)

#: The real Gladia API. A literal in this file, never user- or operator-supplied, so it
#: is exempt from the SSRF guard below — guarding it would mean every provider
#: construction (including every test that never touches base_url at all) pays a live
#: DNS lookup against api.gladia.io.
DEFAULT_BASE_URL = "https://api.gladia.io"


class GladiaProvider(ASRProvider):
    def __init__(self, api_key: str, model_name: str = "standard", base_url: str | None = None):
        self._api_key = api_key
        self._model_name = model_name
        self._base = self._resolve_base_url(base_url)

    @staticmethod
    def _resolve_base_url(configured_base_url: str | None) -> str:
        """Resolve the Gladia API base URL, guarded against SSRF (issue #594).

        Priority: an explicit ``base_url`` — normally ``UserASRSettings.base_url``,
        threaded through ``ASRProviderFactory.create_from_config`` — beats the
        deployment-level ``GLADIA_API_BASE_URL`` env var (the ``--with-mock-asr``
        overlay's override, resolved per-instance so tests can ``monkeypatch.setenv``
        before construction), which beats the real Gladia API.

        Anything that overrides :data:`DEFAULT_BASE_URL` is, by construction, either a
        user-supplied config value or operator-set deployment config — exactly the kind
        of live outbound target ``app/services/llm_service.py`` guards behind
        ``LLM_ALLOW_PRIVATE_ENDPOINTS``. This mirrors that pattern with its own
        ``ASR_ALLOW_PRIVATE_ENDPOINTS`` flag (a deliberately separate setting — see
        ``core/config.py``) so a private/loopback/RFC1918/link-local target — including
        a docker-compose hostname like ``mock-asr`` — is refused unless explicitly
        allowed. Metadata addresses (169.254.169.254 and friends) are refused even then;
        see ``app/utils/url_validation.py``.

        Raises:
            ASRConfigurationError: The resolved base URL is not a permitted outbound
                target. Raised at construction time (like ``_guard_local_allowed``)
                rather than deep inside ``transcribe()``, so a bad config fails fast
                instead of surfacing as an opaque connection error inside a Celery task.
        """
        candidate = (configured_base_url or os.environ.get("GLADIA_API_BASE_URL") or "").strip()
        if not candidate:
            return DEFAULT_BASE_URL
        candidate = candidate.rstrip("/")
        if candidate == DEFAULT_BASE_URL:
            return candidate

        from app.core.config import settings as _settings
        from app.utils.url_validation import is_safe_url

        safe, reason = is_safe_url(candidate, allow_private=_settings.ASR_ALLOW_PRIVATE_ENDPOINTS)
        if not safe:
            logger.warning("Refusing Gladia base_url %r: %s", candidate, reason)
            raise ASRConfigurationError(
                "The configured Gladia base URL is not a permitted outbound target. It "
                "must be a publicly reachable http(s) address. Set "
                "ASR_ALLOW_PRIVATE_ENDPOINTS=true to allow a self-hosted endpoint on a "
                "private network."
            )
        return candidate

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

    def _raise_if_rate_limited(self, resp, context: str) -> None:
        """Raise ASRRateLimitedError when *resp* is a 429, else no-op.

        Gladia's responses have no dedicated exception type (this provider uses plain
        ``requests``, checking ``resp.status_code`` directly), so this is checked before
        ``raise_for_status()`` at each of the three HTTP call sites (upload, job submission,
        poll) rather than caught from an exception.
        """
        if resp.status_code != 429:
            return
        detail = self._sanitize_error(resp.text.strip()[:500] if resp.text else "", self._api_key)
        message = (
            f"Gladia {context} rate limited: {detail}"
            if detail
            else f"Gladia {context} rate limited"
        )
        logger.warning("Gladia %s rate-limited: %s", context, detail)
        raise ASRRateLimitedError(
            message,
            provider="gladia",
            retry_after=retry_after_from_headers(getattr(resp, "headers", None)),
        )

    def validate_connection(self) -> tuple[bool, str, float]:
        """Test API key by hitting the /v2/live endpoint (lightweight, no audio needed)."""
        start = time.time()
        try:
            import requests
        except ImportError:
            return False, "requests not installed. Run: pip install requests", 0.0
        try:
            r = requests.get(f"{self._base}/v2/live", headers=self._hdr(), timeout=10)
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
                    f"{self._base}/v2/upload",
                    headers={"x-gladia-key": self._api_key},
                    files={"audio": (filename, f, content_type)},
                    timeout=300,
                )
            self._raise_if_rate_limited(up, "upload")
            up.raise_for_status()
        except ASRRateLimitedError:
            raise
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
            if len(config.vocabulary) > 100:
                logger.warning(
                    "Gladia custom vocabulary truncated for file=%s: %d terms submitted, "
                    "only the first 100 will be sent (%d dropped). Gladia's custom_vocabulary "
                    "field accepts at most 100 terms.",
                    filename,
                    len(config.vocabulary),
                    len(config.vocabulary) - 100,
                )
            body["custom_vocabulary"] = config.vocabulary[:100]

        try:
            job_r = requests.post(
                f"{self._base}/v2/transcription", headers=self._hdr(), json=body, timeout=30
            )
            self._raise_if_rate_limited(job_r, "job submission")
            job_r.raise_for_status()
        except ASRRateLimitedError:
            raise
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
                self._raise_if_rate_limited(poll, "poll")
                poll.raise_for_status()
                data = poll.json()
            except ASRRateLimitedError:
                raise
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
