"""OpenAI Whisper-1 / GPT-4o Transcribe ASR provider.

Targets openai >= 1.0.0 (Python SDK v1).  Maximum file size: 25 MB.

Notes on diarization
--------------------
Neither ``whisper-1`` nor ``gpt-4o-transcribe`` expose a speaker-diarization
API.  The ``gpt-4o-transcribe-diarize`` model string used in earlier versions
of this file was fictional and has been removed.  If diarization is required,
use a provider that genuinely supports it (Deepgram, AssemblyAI, Azure
ConversationTranscriber, etc.).

Notes on confidence
-------------------
``whisper-1`` verbose_json segments include ``avg_logprob`` (a log-probability
averaged over the segment tokens).  We convert it to a 0-1 probability via
``exp(avg_logprob)`` and clamp to [0, 1].  ``gpt-4o-transcribe`` does not
return segment-level log-probabilities; those segments use ``confidence=None``.
"""

from __future__ import annotations

import logging
import math
import os
import time
from typing import Callable

from .base import ASRProvider
from .types import ASRConfig
from .types import ASRResult
from .types import ASRSegment

logger = logging.getLogger(__name__)
_MAX_MB = 25


class OpenAIASRProvider(ASRProvider):
    def __init__(self, api_key: str, model_name: str = "gpt-4o-transcribe"):
        self._api_key = api_key
        self._model_name = model_name

    @property
    def provider_name(self) -> str:
        return "openai"

    def supports_diarization(self) -> bool:
        # Neither whisper-1 nor gpt-4o-transcribe expose a diarization API.
        return False

    def supports_vocabulary(self) -> bool:
        return False

    def supports_translation(self) -> bool:
        return self._model_name == "whisper-1"

    def validate_connection(self) -> tuple[bool, str, float]:
        """Test connectivity by listing models. Confirms the API key is valid."""
        start = time.time()
        try:
            from openai import OpenAI
        except ImportError:
            return False, "openai not installed. Run: pip install openai", 0.0
        try:
            OpenAI(api_key=self._api_key).models.list()
            ms = (time.time() - start) * 1000
            return True, "OpenAI connection successful", ms
        except Exception as e:
            ms = (time.time() - start) * 1000
            return False, self._sanitize_error(str(e), self._api_key), ms

    def transcribe(  # noqa: C901
        self,
        audio_path: str,
        config: ASRConfig,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> ASRResult:
        try:
            from openai import OpenAI
        except ImportError as err:
            raise RuntimeError("openai not installed. Run: pip install openai") from err

        # Validate the file exists before attempting network I/O.
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        size_mb = os.path.getsize(audio_path) / (1024 * 1024)
        if size_mb > _MAX_MB:
            raise RuntimeError(
                f"File {size_mb:.1f} MB exceeds OpenAI's {_MAX_MB} MB limit. "
                "Compress or trim the audio first."
            )

        filename = os.path.basename(audio_path)
        t_start = time.time()
        logger.info(
            "OpenAI transcribe start: file=%s model=%s lang=%s translate=%s",
            filename,
            self._model_name,
            config.language,
            config.translate_to_english,
        )

        client = OpenAI(api_key=self._api_key)
        lang = None if config.language == "auto" else config.language

        if progress_callback:
            progress_callback(0.2, f"Transcribing with OpenAI {self._model_name}…")

        try:
            with open(audio_path, "rb") as af:
                if self._model_name == "whisper-1" and config.translate_to_english:
                    resp = client.audio.translations.create(
                        model="whisper-1", file=af, response_format="verbose_json"
                    )
                elif self._model_name == "whisper-1":
                    # language may be None (auto-detect); SDK stub types it as str | Omit
                    resp = client.audio.transcriptions.create(  # type: ignore[call-overload]
                        model="whisper-1",
                        file=af,
                        language=lang,
                        response_format="verbose_json",
                    )
                else:
                    resp = client.audio.transcriptions.create(  # type: ignore[call-overload]
                        model=self._model_name,
                        file=af,
                        language=lang,
                        response_format="verbose_json",
                    )
        except Exception as exc:
            sanitized = self._sanitize_error(str(exc), self._api_key)
            logger.error("OpenAI transcription failed for file=%s: %s", filename, sanitized)
            raise RuntimeError(f"OpenAI transcription failed: {sanitized}") from exc

        elapsed_ms = (time.time() - t_start) * 1000
        logger.info("OpenAI transcribe complete: file=%s duration_ms=%.0f", filename, elapsed_ms)

        if progress_callback:
            progress_callback(0.85, "Parsing OpenAI response…")

        segments: list[ASRSegment] = []

        if hasattr(resp, "segments") and resp.segments:
            for sd in resp.segments:
                start = getattr(sd, "start", 0.0)
                end = getattr(sd, "end", 0.0)
                text = getattr(sd, "text", str(sd))
                # whisper-1 verbose_json exposes avg_logprob per segment.
                # Convert log-probability → probability: p = exp(avg_logprob),
                # then clamp to [0, 1] (avg_logprob is <= 0, so exp() is <= 1).
                avg_logprob = getattr(sd, "avg_logprob", None)
                if avg_logprob is not None:
                    confidence: float | None = max(0.0, min(1.0, math.exp(avg_logprob)))
                else:
                    confidence = None
                segments.append(ASRSegment(text=text, start=start, end=end, confidence=confidence))
        else:
            segments = [ASRSegment(text=getattr(resp, "text", ""), start=0.0, end=0.0)]

        if progress_callback:
            progress_callback(1.0, "OpenAI transcription complete")

        return ASRResult(
            segments=segments,
            language=getattr(resp, "language", None) or config.language,
            has_speakers=False,  # OpenAI ASR API does not provide speaker diarization
            provider_name="openai",
            model_name=self._model_name,
        )
