"""Corpus-agnostic data model for non-ASR transcript injection (issue #403).

The adapter layer parses a third-party corpus into these structures; the
injection core turns them into ``MediaFile`` / ``Speaker`` / ``TranscriptSegment``
rows and hands off to the production search-indexing task.

Nothing here knows about SQLAlchemy or OpenSearch on purpose: an adapter can be
unit-tested against files on disk with no stack running.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Any

# Timing provenance values. Recorded on the MediaFile row, in the manifest, and
# checked by ``timings.assert_real_timings`` before any latency/timing metric is
# allowed to read a file's segment times.
TIMING_REAL = "real"
TIMING_SYNTHETIC = "synthetic"


@dataclass(slots=True)
class Word:
    """One timed word. Only ever populated from a real timed source."""

    word: str
    start: float
    end: float

    def as_dict(self) -> dict[str, Any]:
        return {"word": self.word, "start": round(self.start, 3), "end": round(self.end, 3)}


@dataclass(slots=True)
class Turn:
    """One speaker turn as the source corpus records it.

    ``start``/``end`` are ``None`` until timings are resolved — either aligned
    against a timed reference corpus or generated synthetically. ``turn_index``
    is the index into the *source* corpus's turn list and is what gold
    relevance spans (e.g. QMSum ``relevant_text_span``) address, so it is
    preserved verbatim into the manifest even when segments end up reordered by
    time.
    """

    turn_index: int
    speaker: str
    text: str
    start: float | None = None
    end: float | None = None
    words: list[Word] | None = None


@dataclass(slots=True)
class TimingInfo:
    """Where a meeting's segment times came from.

    ``source`` is ``TIMING_REAL`` only when every reported time was aligned to a
    timed reference recording. Anything else is ``TIMING_SYNTHETIC`` and must
    never feed a duration, latency, WER-by-time or diarization metric.
    """

    source: str = TIMING_SYNTHETIC
    reference: str | None = None  # e.g. "icsi:Bdb001" / "ami:ES2004a"
    aligned_turns: int = 0
    total_turns: int = 0
    params: dict[str, Any] | None = None  # synthetic generator parameters

    @property
    def alignment_rate(self) -> float:
        return (self.aligned_turns / self.total_turns) if self.total_turns else 0.0

    @property
    def is_real(self) -> bool:
        return self.source == TIMING_REAL


@dataclass(slots=True)
class MeetingDoc:
    """A single meeting, parsed and ready to inject."""

    corpus: str
    meeting_id: str
    title: str
    turns: list[Turn]
    language: str = "en"
    timing: TimingInfo = field(default_factory=TimingInfo)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def word_count(self) -> int:
        return sum(len(t.text.split()) for t in self.turns)

    @property
    def speakers(self) -> list[str]:
        seen: dict[str, None] = {}
        for t in self.turns:
            seen.setdefault(t.speaker, None)
        return list(seen)

    @property
    def duration(self) -> float:
        ends = [t.end for t in self.turns if t.end is not None]
        return max(ends) if ends else 0.0


@dataclass(slots=True)
class CorpusInfo:
    """Static description of a corpus, recorded in the run manifest."""

    key: str
    name: str
    version: str  # pinned commit / release id — whatever identifies THIS copy
    license_tier: str  # "A" publishable, "B" internal-only (see NAS README)
    root: str
    citation: str = ""


@dataclass(slots=True)
class InjectionRecord:
    """One row of the run manifest — what actually landed in the database."""

    corpus: str
    meeting_id: str
    file_uuid: str
    media_file_id: int
    title: str
    turn_count: int
    segment_count: int
    word_count: int
    speaker_count: int
    duration_seconds: float
    timing_source: str
    timing_reference: str | None
    timing_aligned_turns: int
    timing_alignment_rate: float
    synthetic_timing_params: dict[str, Any] | None
    content_sha256: str
    language: str
    action: str  # "created" | "updated" | "skipped" | "dry-run"
    index_task_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
