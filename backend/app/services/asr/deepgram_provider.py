"""Deepgram Nova-3 ASR provider.

Targets deepgram-sdk >= 6.0.0 (Fern-generated client with keyword-only APIs).
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

_MAX_SEGMENT_DURATION = 30.0  # seconds — cap for no-diarization grouping
_MIN_PAUSE_SPLIT = 0.5  # seconds — pause gap that forces a new segment


def _group_words_into_segments(words: list, fallback_text: str) -> list:
    """Group a flat word list into segments for the no-diarization path.

    Splits on either a silence gap >= _MIN_PAUSE_SPLIT seconds OR when the
    current segment duration would exceed _MAX_SEGMENT_DURATION seconds.
    Falls back to a single text-only segment when no word timestamps exist.
    """
    from .types import ASRSegment  # local import to avoid circular refs at module level

    if not words:
        return [ASRSegment(text=fallback_text, start=0.0, end=0.0)]

    segments: list = []
    cur: list = [words[0]]

    for w in words[1:]:
        gap = w.start - cur[-1].end
        duration = cur[-1].end - cur[0].start
        if gap >= _MIN_PAUSE_SPLIT or duration >= _MAX_SEGMENT_DURATION:
            text = " ".join(x.word for x in cur)
            avg_conf = sum(x.confidence for x in cur) / len(cur)
            segments.append(
                ASRSegment(
                    text=text,
                    start=cur[0].start,
                    end=cur[-1].end,
                    confidence=avg_conf,
                    words=list(cur),
                )
            )
            cur = [w]
        else:
            cur.append(w)

    if cur:
        text = " ".join(x.word for x in cur)
        avg_conf = sum(x.confidence for x in cur) / len(cur)
        segments.append(
            ASRSegment(
                text=text,
                start=cur[0].start,
                end=cur[-1].end,
                confidence=avg_conf,
                words=list(cur),
            )
        )
    return segments


class DeepgramProvider(ASRProvider):
    def __init__(self, api_key: str, model_name: str = "nova-3"):
        self._api_key = api_key
        self._model_name = model_name

    @property
    def provider_name(self) -> str:
        return "deepgram"

    def supports_diarization(self) -> bool:
        return True

    def supports_vocabulary(self) -> bool:
        return True  # keywords parameter

    def supports_translation(self) -> bool:
        return False

    def validate_connection(self) -> tuple[bool, str, float]:
        """Test connectivity by listing projects. Requires a valid API key."""
        start = time.time()
        try:
            from deepgram import DeepgramClient
        except ImportError:
            return False, "deepgram-sdk not installed. Run: pip install 'deepgram-sdk>=6.0.0'", 0.0
        try:
            client = DeepgramClient(api_key=self._api_key)
            client.manage.v1.projects.list()
            ms = (time.time() - start) * 1000
            return True, "Deepgram connection successful", ms
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
            from deepgram import DeepgramClient
        except ImportError as err:
            raise RuntimeError(
                "deepgram-sdk not installed. Run: pip install 'deepgram-sdk>=6.0.0'"
            ) from err

        # Validate the file exists before attempting network I/O.
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        filename = os.path.basename(audio_path)
        t_start = time.time()
        logger.info(
            "Deepgram transcribe start: file=%s model=%s diarize=%s lang=%s",
            filename,
            self._model_name,
            config.enable_diarization,
            config.language,
        )

        if progress_callback:
            progress_callback(0.1, "Uploading to Deepgram…")

        client = DeepgramClient(api_key=self._api_key)

        # Build keyword args for transcribe_file (v6 uses keyword-only params)
        transcribe_kwargs: dict = {
            "model": self._model_name,
            "smart_format": True,
            "diarize": config.enable_diarization,
            "punctuate": True,
            "utterances": True,
        }
        if config.language and config.language != "auto":
            transcribe_kwargs["language"] = config.language
        if config.vocabulary:
            transcribe_kwargs["keywords"] = config.vocabulary[:100]

        if progress_callback:
            progress_callback(0.3, "Transcribing with Deepgram…")

        # Read file into bytes — the v6 SDK expects bytes or an iterator for request=.
        try:
            with open(audio_path, "rb") as f:
                audio_bytes = f.read()
            response = client.listen.v1.media.transcribe_file(
                request=audio_bytes, **transcribe_kwargs
            )
        except Exception as exc:
            sanitized = self._sanitize_error(str(exc), self._api_key)
            logger.error("Deepgram transcription failed for file=%s: %s", filename, sanitized)
            raise RuntimeError(f"Deepgram transcription failed: {sanitized}") from exc

        elapsed_ms = (time.time() - t_start) * 1000
        logger.info("Deepgram transcribe complete: file=%s duration_ms=%.0f", filename, elapsed_ms)

        if progress_callback:
            progress_callback(0.85, "Parsing Deepgram response…")

        result = response.results
        ch = result.channels[0]
        detected_language = getattr(ch, "detected_language", None) or config.language

        utt_count = len(result.utterances) if result.utterances else 0
        utt_speakers = {u.speaker for u in result.utterances} if result.utterances else set()
        logger.info(
            "Deepgram response: utterances=%d speakers=%s file=%s",
            utt_count,
            sorted(utt_speakers) if utt_speakers else "none",
            filename,
        )

        segments: list[ASRSegment] = []
        has_speakers = False

        if result.utterances:
            # Use utterances for segment boundaries (better semantic grouping)
            # Speaker labels only present when diarize=true
            for utt in result.utterances:
                speaker = self._normalize_speaker_label(utt.speaker)
                if speaker:
                    has_speakers = True
                words = [
                    ASRWord(
                        word=w.word,
                        start=w.start,
                        end=w.end,
                        confidence=getattr(w, "confidence", 1.0),
                    )
                    for w in (utt.words or [])
                ]
                segments.append(
                    ASRSegment(
                        text=utt.transcript,
                        start=utt.start,
                        end=utt.end,
                        speaker=speaker,
                        confidence=getattr(utt, "confidence", None),
                        words=words,
                    )
                )
        else:
            alt = ch.alternatives[0]
            all_words = [
                ASRWord(w.word, w.start, w.end, getattr(w, "confidence", 1.0))
                for w in (alt.words or [])
            ]
            segments = _group_words_into_segments(all_words, alt.transcript)

        if progress_callback:
            progress_callback(1.0, "Deepgram transcription complete")

        return ASRResult(
            segments=segments,
            language=detected_language,
            has_speakers=has_speakers,
            provider_name="deepgram",
            model_name=self._model_name,
        )
