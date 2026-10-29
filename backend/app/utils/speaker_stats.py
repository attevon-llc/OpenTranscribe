"""Per-speaker talk time, segment count and word count: the one implementation.

Two planes report these numbers. ``services/analytics_service.py`` labels speakers with the
raw diarizer label (``SPEAKER_00``; the SPA maps it to a name) and
``utils/transcript_builders.compute_speaker_stats`` with the canonical display label (LLM
prompts, and ``file_facts`` -> chat). Each used to carry its own arithmetic, and they
disagreed on a segment with a missing time (one raised ``TypeError``, the other counted the
absolute end time as the duration). The tally below is keyed by the speaker, and the label
is applied at the edge, so the planes now differ in labels only.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass
class SpeakerTally:
    """One speaker's (or one label's) share of a transcript."""

    total_time: float = 0.0
    segment_count: int = 0
    word_count: int = 0


def segment_duration(segment: Any) -> float:
    """Seconds a segment spans; a missing start or end time contributes none.

    Both columns are NOT NULL in the database, so only an in-memory caller can hand in a
    missing time. An inverted segment (end before start) is reported as it is, negative.
    """
    if segment.start_time is None or segment.end_time is None:
        return 0.0
    return float(segment.end_time - segment.start_time)


def count_words(text: object) -> int:
    """Words in a segment's text: runs of non-whitespace, Unicode whitespace as separator."""
    return len(str(text or "").split())


def tally_by_speaker(
    segments: Iterable[Any], label: Callable[[Any], str]
) -> dict[str, SpeakerTally]:
    """Tally segments per speaker, then key the result by ``label``.

    A speaker is ``segment.speaker_id``; a segment without one (no linked speaker, or a
    stand-in that has no row) is keyed by its own label. ``label`` is called once per speaker.
    Speakers that share a label (two diarized speakers a human gave the same name) are summed
    into one entry, because a label-keyed mapping cannot hold both. Labels keep the order in
    which their first speaker appears.
    """
    by_speaker: dict[int | str, SpeakerTally] = {}
    labels: dict[int | str, str] = {}
    for segment in segments:
        speaker_id = getattr(segment, "speaker_id", None)
        key: int | str
        if speaker_id is None:
            key = label(segment)
            labels[key] = key
        else:
            key = speaker_id
            if key not in labels:
                labels[key] = label(segment)
        tally = by_speaker.setdefault(key, SpeakerTally())
        tally.total_time += segment_duration(segment)
        tally.segment_count += 1
        tally.word_count += count_words(segment.text)

    merged: dict[str, SpeakerTally] = {}
    for key, tally in by_speaker.items():
        into = merged.setdefault(labels[key], SpeakerTally())
        into.total_time += tally.total_time
        into.segment_count += tally.segment_count
        into.word_count += tally.word_count
    return merged
