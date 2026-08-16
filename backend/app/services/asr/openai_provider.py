"""OpenAI Whisper-1 / GPT-4o Transcribe ASR provider.

Targets openai >= 1.0.0 (Python SDK v1).  Maximum file size: 25 MB.

Notes on diarization
--------------------
``whisper-1`` and ``gpt-4o-transcribe`` do not expose a speaker-diarization
API. ``gpt-4o-transcribe-diarize`` does (confirmed against OpenAI's public API
reference and the "Introducing GPT-4o Transcribe Diarize" announcement,
2026): requesting ``response_format="diarized_json"`` returns
``segments: [{"speaker": "Speaker 1", "text": ..., "start": ..., "end": ...}, ...]``.
An earlier version of this file's docstring claimed this model id "was
fictional and has been removed" -- that was wrong; it is a real, documented
model. Schema verified via OpenAI's docs and community bug reports, not a
live call (no API key configured in this environment) -- validate against a
real response before relying on this in production.

Not implemented: ``known_speaker_names``/``known_speaker_references`` (map
segments onto named reference speakers) and the >30s ``chunking_strategy``
requirement some docs mention for this model -- neither is exercised by
this provider today.

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
from collections.abc import Callable

from .base import ASRProvider
from .base import normalize_speaker_label
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
        # Only gpt-4o-transcribe-diarize (response_format="diarized_json") returns
        # speaker labels; whisper-1 and plain gpt-4o-transcribe never do.
        return self._model_name == "gpt-4o-transcribe-diarize"

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
                elif self._model_name == "gpt-4o-transcribe-diarize":
                    # diarized_json is the only response_format that returns speaker
                    # labels for this model; verbose_json is explicitly rejected.
                    resp = client.audio.transcriptions.create(  # type: ignore[call-overload]
                        model=self._model_name,
                        file=af,
                        language=lang,
                        response_format="diarized_json",
                    )
                else:
                    # gpt-4o-transcribe / gpt-4o-mini-transcribe reject verbose_json
                    # outright ("response_format 'verbose_json' is not compatible
                    # with model ...") -- only "json" is supported. That response
                    # has no `segments`/`avg_logprob`, which the parsing below
                    # already handles via its no-segments fallback.
                    resp = client.audio.transcriptions.create(  # type: ignore[call-overload]
                        model=self._model_name,
                        file=af,
                        language=lang,
                        response_format="json",
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
                # Only diarized_json segments (gpt-4o-transcribe-diarize) carry a
                # `speaker` field ("Speaker 1", "Speaker 2", ...); absent for every
                # other model/response_format, so this is a no-op for them.
                raw_speaker = getattr(sd, "speaker", None)
                speaker = normalize_speaker_label(raw_speaker) if raw_speaker else None
                segments.append(
                    ASRSegment(
                        text=text, start=start, end=end, speaker=speaker, confidence=confidence
                    )
                )
        else:
            segments = [ASRSegment(text=getattr(resp, "text", ""), start=0.0, end=0.0)]

        has_speakers = any(s.speaker for s in segments)

        if progress_callback:
            progress_callback(1.0, "OpenAI transcription complete")

        return ASRResult(
            segments=segments,
            language=getattr(resp, "language", None) or config.language,
            has_speakers=has_speakers,
            provider_name="openai",
            model_name=self._model_name,
        )
