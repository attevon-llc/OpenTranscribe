"""Speechmatics batch ASR provider.

Uses the current ``speechmatics-batch`` SDK (async ``AsyncClient``). The legacy
``speechmatics-python`` package is deprecated and, in our testing, silently dropped
speaker labels even with diarization enabled; the new SDK returns them correctly.

Supports 55+ languages and speaker diarization. Per the Speechmatics docs, the speaker
label lives at ``results[].alternatives[0].speaker`` (``"S1"``/``"S2"``/``"UU"``); ``"UU"``
means the speaker could not be identified and is treated as no speaker.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from typing import Any

from .base import ASRProvider
from .types import ASRConfig
from .types import ASRResult
from .types import ASRSegment
from .types import ASRWord

logger = logging.getLogger(__name__)

_JOB_TIMEOUT = 1800.0  # seconds — hard cap on wait_for_completion


class SpeechmaticsProvider(ASRProvider):
    def __init__(self, api_key: str, model_name: str = "standard"):
        self._api_key = api_key
        self._model_name = model_name

    @property
    def provider_name(self) -> str:
        return "speechmatics"

    def supports_diarization(self) -> bool:
        return True

    def supports_vocabulary(self) -> bool:
        return True

    def supports_translation(self) -> bool:
        return False

    def validate_connection(self) -> tuple[bool, str, float]:
        """Test credentials with a lightweight list-jobs call (limit=1)."""
        start = time.time()
        try:
            import speechmatics.batch  # noqa: F401
        except ImportError:
            return (
                False,
                "speechmatics-batch not installed. Run: pip install speechmatics-batch",
                0.0,
            )
        try:
            import requests as _requests

            resp = _requests.get(
                "https://asr.api.speechmatics.com/v2/jobs",
                headers={"Authorization": f"Bearer {self._api_key}"},
                params={"limit": 1},
                timeout=10,
            )
            ms = (time.time() - start) * 1000
            if resp.status_code == 401:
                return (
                    False,
                    self._sanitize_error(
                        "Invalid Speechmatics API key (401 Unauthorized)", self._api_key
                    ),
                    ms,
                )
            return True, "Speechmatics connection successful", ms
        except Exception as e:
            ms = (time.time() - start) * 1000
            return False, self._sanitize_error(str(e), self._api_key), ms

    def transcribe(
        self,
        audio_path: str,
        config: ASRConfig,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> ASRResult:
        try:
            from speechmatics.batch import AsyncClient
            from speechmatics.batch import FormatType
            from speechmatics.batch import TranscriptionConfig
        except ImportError as err:
            raise RuntimeError(
                "speechmatics-batch not installed. Run: pip install speechmatics-batch"
            ) from err

        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        filename = os.path.basename(audio_path)
        t_start = time.time()
        lang = config.language if config.language != "auto" else "en"
        logger.info(
            "Speechmatics transcribe start: file=%s model=%s diarize=%s lang=%s",
            filename,
            self._model_name,
            config.enable_diarization,
            lang,
        )

        tc_kwargs: dict[str, Any] = {"language": lang}
        if config.enable_diarization:
            tc_kwargs["diarization"] = "speaker"
        if config.vocabulary:
            tc_kwargs["additional_vocab"] = list(config.vocabulary[:1000])
        tc = TranscriptionConfig(**tc_kwargs)

        if progress_callback:
            progress_callback(0.2, "Submitting Speechmatics job…")

        async def _run() -> Any:
            client = AsyncClient(api_key=self._api_key)
            try:
                job = await client.submit_job(audio_path, transcription_config=tc)
                return await client.wait_for_completion(
                    job.id, format_type=FormatType.JSON, timeout=_JOB_TIMEOUT
                )
            finally:
                await client.close()

        try:
            transcript = asyncio.run(_run())
        except Exception as exc:
            sanitized = self._sanitize_error(str(exc), self._api_key)
            logger.error("Speechmatics transcription failed for file=%s: %s", filename, sanitized)
            raise RuntimeError(f"Speechmatics transcription failed: {sanitized}") from exc

        logger.info(
            "Speechmatics transcribe complete: file=%s duration_ms=%.0f",
            filename,
            (time.time() - t_start) * 1000,
        )
        if progress_callback:
            progress_callback(0.85, "Parsing Speechmatics response…")

        segments = self._build_segments(transcript)

        if progress_callback:
            progress_callback(1.0, "Speechmatics transcription complete")

        # True only when speaker labels were actually returned (short/silent audio may
        # come back unlabelled even with diarization enabled).
        has_speakers = config.enable_diarization and any(s.speaker for s in segments)
        return ASRResult(
            segments=segments,
            language=lang,
            has_speakers=has_speakers,
            provider_name="speechmatics",
            model_name=self._model_name,
        )

    def _build_segments(self, transcript: Any) -> list[ASRSegment]:
        """Group the SDK's per-word results into speaker-contiguous segments."""
        segments: list[ASRSegment] = []
        cur_spk: str | None = None
        cur_words: list[ASRWord] = []
        cur_start = 0.0

        def flush() -> None:
            if not cur_words:
                return
            avg_conf = sum(w.confidence for w in cur_words) / len(cur_words)
            segments.append(
                ASRSegment(
                    text=" ".join(w.word for w in cur_words),
                    start=cur_start,
                    end=cur_words[-1].end,
                    speaker=self._normalize_speaker_label(cur_spk) if cur_spk is not None else None,
                    confidence=avg_conf,
                    words=list(cur_words),
                )
            )

        for r in getattr(transcript, "results", None) or []:
            if getattr(r, "type", None) != "word" or not r.alternatives:
                continue
            alt = r.alternatives[0]
            raw_spk = getattr(alt, "speaker", None)  # "S1" | "S2" | "UU" | None
            spk = None if (raw_spk is None or raw_spk == "UU") else raw_spk
            start = float(getattr(r, "start_time", 0.0) or 0.0)
            end = float(getattr(r, "end_time", start) or start)
            conf = float(getattr(alt, "confidence", 1.0) or 1.0)

            if spk != cur_spk:
                flush()
                cur_spk, cur_words, cur_start = spk, [], start

            cur_words.append(ASRWord(getattr(alt, "content", ""), start, end, conf))

        flush()
        return segments
