"""AssemblyAI ASR provider.

Targets assemblyai >= 0.30.0 (SDK v2 synchronous Transcriber API).
"""

from __future__ import annotations

import logging
import os
import time
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


class AssemblyAIProvider(ASRProvider):
    def __init__(self, api_key: str, model_name: str = "universal"):
        self._api_key = api_key
        self._model_name = model_name

    @property
    def provider_name(self) -> str:
        return "assemblyai"

    def supports_diarization(self) -> bool:
        return True

    def supports_vocabulary(self) -> bool:
        return True  # word_boost

    def supports_translation(self) -> bool:
        return False

    def validate_connection(self) -> tuple[bool, str, float]:
        """Test connectivity with a lightweight list-transcripts call (limit=1)."""
        start = time.time()
        try:
            import assemblyai as aai  # noqa: F401
        except ImportError:
            return False, "assemblyai not installed. Run: pip install 'assemblyai>=0.30.0'", 0.0
        try:
            import requests as _requests

            resp = _requests.get(
                "https://api.assemblyai.com/v2/transcript",
                headers={"authorization": self._api_key},
                params={"limit": 1},
                timeout=10,
            )
            ms = (time.time() - start) * 1000
            if resp.status_code == 401:
                return (
                    False,
                    self._sanitize_error(
                        "Invalid AssemblyAI API key (401 Unauthorized)", self._api_key
                    ),
                    ms,
                )
            return True, "AssemblyAI connection successful", ms
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
            import assemblyai as aai
        except ImportError as err:
            raise RuntimeError(
                "assemblyai not installed. Run: pip install 'assemblyai>=0.30.0'"
            ) from err

        # Validate the file exists before attempting network I/O.
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        filename = os.path.basename(audio_path)
        t_start = time.time()
        logger.info(
            "AssemblyAI transcribe start: file=%s model=%s diarize=%s lang=%s",
            filename,
            self._model_name,
            config.enable_diarization,
            config.language,
        )

        aai.settings.api_key = self._api_key

        # `speech_model` (singular) is deprecated server-side; the API requires
        # `speech_models` — a priority list of model-id strings. Valid ids (AssemblyAI docs,
        # /docs/pre-recorded-audio/select-the-speech-model): universal-3-pro (English+5),
        # universal-2 (99-language), plus slam-1 (English, highest accuracy) and nano (budget).
        _model_map = {
            "universal": "universal-3-pro",
            "universal-multilingual": "universal-2",
            "slam-1": "slam-1",
            "nano": "nano",
        }
        speech_models = [_model_map.get(self._model_name, "universal-3-pro")]
        # universal-2 is the multilingual model → let it auto-detect language.
        detect_language = self._model_name == "universal-multilingual" or config.language == "auto"

        cfg = aai.TranscriptionConfig(
            speech_models=speech_models,
            speaker_labels=config.enable_diarization,
            speakers_expected=config.num_speakers,
            language_detection=detect_language,
            language_code=None if detect_language else config.language,
            punctuate=True,
            format_text=True,
            word_boost=config.vocabulary[:1000] if config.vocabulary else None,
        )

        if progress_callback:
            progress_callback(0.2, "Transcribing with AssemblyAI…")

        try:
            transcript = aai.Transcriber().transcribe(audio_path, config=cfg)
        except Exception as exc:
            sanitized = self._sanitize_error(str(exc), self._api_key)
            logger.error("AssemblyAI transcription failed for file=%s: %s", filename, sanitized)
            raise RuntimeError(f"AssemblyAI transcription failed: {sanitized}") from exc

        if transcript.status == aai.TranscriptStatus.error:
            err_msg = self._sanitize_error(str(transcript.error or "unknown error"), self._api_key)
            logger.error("AssemblyAI job error for file=%s: %s", filename, err_msg)
            raise RuntimeError(f"AssemblyAI error: {err_msg}")

        elapsed_ms = (time.time() - t_start) * 1000
        logger.info(
            "AssemblyAI transcribe complete: file=%s duration_ms=%.0f", filename, elapsed_ms
        )

        if progress_callback:
            progress_callback(0.85, "Parsing AssemblyAI response…")

        segments: list[ASRSegment] = []
        has_speakers = False

        if config.enable_diarization and transcript.utterances:
            has_speakers = True
            for utt in transcript.utterances:
                words = [
                    ASRWord(
                        word=w.text,
                        start=w.start / 1000.0,
                        end=w.end / 1000.0,
                        confidence=w.confidence if w.confidence is not None else 1.0,
                    )
                    for w in (utt.words or [])
                ]
                segments.append(
                    ASRSegment(
                        text=utt.text,
                        start=utt.start / 1000.0,
                        end=utt.end / 1000.0,
                        speaker=self._normalize_speaker_label(utt.speaker),
                        confidence=utt.confidence,
                        words=words,
                    )
                )
        elif transcript.words:
            # Group into segments when diarization is disabled.
            # Split on silence gaps >= _MIN_GAP s OR when segment duration would exceed _MAX_DUR s.
            # New chunk_start is taken from the *next* word's start time so silence gaps
            # are not swallowed into the reported segment duration.
            chunk_words: list = []
            chunk_start = transcript.words[0].start / 1000.0
            words_iter = list(transcript.words)
            for idx, w in enumerate(words_iter):
                chunk_words.append(w)
                end_s = w.end / 1000.0
                is_last = idx == len(words_iter) - 1
                next_start_s = (words_iter[idx + 1].start / 1000.0) if not is_last else end_s
                gap = next_start_s - end_s
                duration = end_s - chunk_start
                if is_last or gap >= _MIN_GAP or duration >= _MAX_DUR:
                    text = " ".join(x.text for x in chunk_words)
                    asr_words = [
                        ASRWord(
                            x.text,
                            x.start / 1000.0,
                            x.end / 1000.0,
                            x.confidence if x.confidence is not None else 1.0,
                        )
                        for x in chunk_words
                    ]
                    avg_conf = sum(aw.confidence for aw in asr_words) / len(asr_words)
                    segments.append(
                        ASRSegment(
                            text=text,
                            start=chunk_start,
                            end=end_s,
                            confidence=avg_conf,
                            words=asr_words,
                        )
                    )
                    chunk_words = []
                    chunk_start = next_start_s
        else:
            # Empty transcript — return a single empty segment so the pipeline can
            # detect the no-speech condition via _validate_transcription_result.
            segments = [ASRSegment(text=transcript.text or "", start=0.0, end=0.0)]

        if progress_callback:
            progress_callback(1.0, "AssemblyAI transcription complete")

        return ASRResult(
            segments=segments,
            language=transcript.language_code or config.language,
            has_speakers=has_speakers,
            provider_name="assemblyai",
            model_name=self._model_name,
        )
