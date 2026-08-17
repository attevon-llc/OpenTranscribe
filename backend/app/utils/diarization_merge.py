"""Merge cloud ASR transcription with independent diarization results.

Assigns speaker labels from diarization segments onto ASR words/segments
using a fast midpoint-overlap algorithm — no pandas/numpy required.
Used when cloud ASR and cloud diarization run in parallel.
"""

from __future__ import annotations

import bisect
import logging

from app.services.asr.types import ASRResult
from app.services.asr.types import ASRSegment
from app.services.asr.types import ASRWord
from app.services.diarization.types import DiarizeResult

logger = logging.getLogger(__name__)


def merge_cloud_diarization(
    asr_result: ASRResult,
    diarize_result: DiarizeResult,
) -> ASRResult:
    """Merge independent diarization results onto an ASR transcript.

    Uses a fast bisect-based lookup to assign each ASR word to the diarization
    segment that covers its midpoint. Then determines segment-level speakers
    by majority vote of word speakers.

    Args:
        asr_result: Transcription result with word timestamps but no speakers.
        diarize_result: Diarization result with speaker segments.

    Returns:
        Updated ASRResult with speaker labels assigned to segments and words.
    """
    if not diarize_result.segments:
        logger.warning("Empty diarization result — returning ASR result without speakers")
        return asr_result

    # Build sorted lookup arrays for fast bisect search
    # Each diarization segment: (start, end, speaker)
    d_segs = sorted(diarize_result.segments, key=lambda s: s.start)
    d_starts = [s.start for s in d_segs]

    def _find_speaker(midpoint: float) -> str | None:
        """Find which diarization segment covers a given time midpoint."""
        idx = bisect.bisect_right(d_starts, midpoint) - 1
        if idx >= 0 and d_segs[idx].start <= midpoint <= d_segs[idx].end:
            return d_segs[idx].speaker
        return None

    new_segments: list[ASRSegment] = []
    for seg in asr_result.segments:
        word_speakers: list[str | None] = []
        new_words: list[ASRWord] = []

        for w in seg.words or []:
            mid = (w.start + w.end) / 2.0
            spk = _find_speaker(mid)
            word_speakers.append(spk)
            new_words.append(
                ASRWord(
                    word=w.word,
                    start=w.start,
                    end=w.end,
                    confidence=w.confidence,
                )
            )

        # Segment speaker = majority vote of word speakers (or midpoint fallback)
        if word_speakers:
            counts: dict[str, int] = {}
            for spk in word_speakers:
                if spk:
                    counts[spk] = counts.get(spk, 0) + 1
            seg_speaker = max(counts, key=counts.get) if counts else None  # type: ignore[arg-type]
        else:
            # No words — use segment midpoint
            seg_speaker = _find_speaker((seg.start + seg.end) / 2.0)

        new_segments.append(
            ASRSegment(
                text=seg.text,
                start=seg.start,
                end=seg.end,
                speaker=seg_speaker,
                confidence=seg.confidence,
                words=new_words,
            )
        )

    unique_speakers = {s.speaker for s in new_segments if s.speaker}
    logger.info(
        "Merged diarization: %d segments, %d speakers from %s",
        len(new_segments),
        len(unique_speakers),
        diarize_result.provider_name,
    )

    return ASRResult(
        segments=new_segments,
        language=asr_result.language,
        has_speakers=bool(unique_speakers),
        provider_name=asr_result.provider_name,
        model_name=asr_result.model_name,
        metadata={
            **asr_result.metadata,
            "diarization_provider": diarize_result.provider_name,
            "diarization_model": diarize_result.model_name,
        },
    )
