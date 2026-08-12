"""Synthetic timestamp generation and the guard that keeps them out of metrics.

QMSum's ``Committee`` meetings — and any corpus that ships plain speaker-labelled
text — have **no timestamps at all**. OpenTranscribe chunks and cites by time, so
something has to be there. Generating it is fine. Letting a later reader mistake
a generated number for a measurement is not: a "mean answer latency" or "citation
timestamp error" computed over synthetic times would be a fabricated result in a
paper.

Four layers keep that from happening by accident:

1. Generated times are recorded as :data:`~.model.TIMING_SYNTHETIC` on the
   ``MediaFile`` row (``metadata_important.rag_eval``) and in the run manifest.
2. ``TranscriptSegment.words`` is left NULL for a synthetic meeting, so a
   word-level timing metric has literally no rows to read.
3. :func:`assert_real_timings` raises rather than returning a filtered list, so
   the failure mode of forgetting the check is a crash, not a wrong number.
4. The generator parameters are recorded in the manifest, so anyone who *does*
   look at a synthetic duration can see exactly which constant produced it.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.scripts.corpus_injection.model import TIMING_REAL
from app.scripts.corpus_injection.model import MeetingDoc
from app.scripts.corpus_injection.model import TimingInfo
from app.scripts.corpus_injection.model import Turn

# Deterministic synthetic-speech model. Values are conventional, not measured:
# 150 wpm is the middle of the usual 140-160 wpm conversational range. They are
# constants rather than a random draw so the same corpus always injects
# byte-identically.
SYNTHETIC_WORDS_PER_SECOND = 2.5
SYNTHETIC_INTER_TURN_GAP_S = 0.25
SYNTHETIC_MIN_TURN_S = 0.5

# Floor on the width of an interpolated turn. Without it, a run of untimed turns
# sitting in a zero-width gap between two timed neighbours all collapse onto the
# same instant — and the live DDL carries
# ``UNIQUE (media_file_id, start_time, end_time, md5(text))``
# (``uq_transcript_segment_content``), so two identical short backchannels
# ("Yeah.", "Okay.") at the same instant abort the whole insert.
MIN_INTERPOLATED_SLOT_S = 0.01

SYNTHETIC_PARAMS = {
    "generator": "uniform_rate_v1",
    "words_per_second": SYNTHETIC_WORDS_PER_SECOND,
    "inter_turn_gap_s": SYNTHETIC_INTER_TURN_GAP_S,
    "min_turn_s": SYNTHETIC_MIN_TURN_S,
}


class SyntheticTimingError(RuntimeError):
    """Raised when a timing-sensitive computation is handed synthetic times."""


def generate_synthetic_timings(turns: list[Turn]) -> None:
    """Fill ``start``/``end`` on every turn in place, in turn order.

    Turn duration is proportional to word count at a fixed rate, with a fixed
    gap between turns and a floor so a one-word turn still occupies a
    non-degenerate span (a zero-length segment would collapse chunk boundaries).
    Word-level timings are deliberately NOT generated.
    """
    cursor = 0.0
    for turn in turns:
        words = max(1, len(turn.text.split()))
        duration = max(SYNTHETIC_MIN_TURN_S, words / SYNTHETIC_WORDS_PER_SECOND)
        turn.start = round(cursor, 3)
        turn.end = round(cursor + duration, 3)
        turn.words = None
        cursor = turn.end + SYNTHETIC_INTER_TURN_GAP_S


def interpolate_missing(turns: list[Turn]) -> int:
    """Give a time to turns the aligner could not place, between their neighbours.

    Returns the number of turns that had to be interpolated. A turn with no
    aligned words (a single "Mm-hmm" the reference transcribes as a vocal sound,
    say) is squeezed into the gap between the last timed turn before it and the
    first after it; runs of them share that gap evenly. Leading/trailing runs
    are anchored to the corpus start / last known end.
    """
    n = len(turns)
    timed = [i for i, t in enumerate(turns) if t.start is not None and t.end is not None]
    if not timed:
        return 0

    filled = 0
    for i, turn in enumerate(turns):
        if turn.start is not None and turn.end is not None:
            continue
        prev = max((j for j in timed if j < i), default=None)
        nxt = min((j for j in timed if j > i), default=None)
        # prev/nxt are drawn from `timed`, so their times are never None; the
        # `or 0.0` is a type narrowing, not a fallback.
        lo = (turns[prev].end or 0.0) if prev is not None else 0.0
        hi = (turns[nxt].start or 0.0) if nxt is not None else (turns[timed[-1]].end or 0.0)
        if hi < lo:
            hi = lo
        # Share the gap across the whole run of untimed turns this one belongs to.
        run_start = (prev + 1) if prev is not None else 0
        run_end = (nxt - 1) if nxt is not None else n - 1
        run_len = max(1, run_end - run_start + 1)
        slot = max((hi - lo) / run_len, MIN_INTERPOLATED_SLOT_S)
        offset = i - run_start
        turn.start = round(lo + slot * offset, 3)
        turn.end = round(lo + slot * (offset + 1), 3)
        turn.words = None
        filled += 1
    return filled


def resolve_timings(doc: MeetingDoc, min_alignment_rate: float = 0.8) -> None:
    """Settle a meeting's timing provenance, mutating ``doc`` in place.

    An adapter that aligned against a timed reference leaves times on some or
    all turns and sets ``doc.timing``. This decides whether that alignment was
    good enough to call the whole meeting *real*:

    * rate >= ``min_alignment_rate`` — keep the aligned times, interpolate the
      stragglers, provenance stays ``real``. The interpolated minority is
      reported in the manifest as ``timing_aligned_turns`` so the claim stays
      auditable.
    * otherwise — discard the partial alignment entirely and regenerate the
      whole meeting synthetically. Provenance is per-file and all-or-nothing on
      purpose: a file that is 60 % measured and 40 % invented is neither, and
      every downstream consumer would have to carry a per-segment predicate to
      use it safely.
    """
    total = len(doc.turns)
    doc.timing.total_turns = total
    if total == 0:
        doc.timing.source = TIMING_REAL if doc.timing.is_real else doc.timing.source
        return

    aligned = sum(1 for t in doc.turns if t.start is not None and t.end is not None)
    doc.timing.aligned_turns = aligned

    if doc.timing.is_real and (aligned / total) >= min_alignment_rate:
        interpolate_missing(doc.turns)
        doc.timing.params = None
        return

    if aligned == total:
        # A generated corpus that supplied its own pacing. Keep the times —
        # discarding them would flatten deliberate overlap and silence
        # structure — but the provenance stays synthetic, because a generator's
        # output is not a measurement no matter how plausible it looks.
        doc.timing = TimingInfo(
            source="synthetic",
            reference=doc.timing.reference,
            aligned_turns=0,
            total_turns=total,
            params={"generator": "corpus_supplied_v1"},
        )
        for turn in doc.turns:
            turn.words = None
        return

    generate_synthetic_timings(doc.turns)
    doc.timing = TimingInfo(
        source="synthetic",
        reference=doc.timing.reference if doc.timing.reference else None,
        aligned_turns=0,
        total_turns=total,
        params=dict(SYNTHETIC_PARAMS),
    )


def assert_real_timings(records: Iterable, context: str = "timing metric") -> None:  # noqa: ANN001
    """Raise unless every manifest record carries real, measured timings.

    Call this at the top of anything that computes a number from a segment
    ``start``/``end``. It takes manifest records rather than a database handle so
    the check is available to offline analysis too.
    """
    offenders = [
        getattr(r, "meeting_id", None) or r["meeting_id"]
        for r in records
        if (getattr(r, "timing_source", None) or r["timing_source"]) != TIMING_REAL
    ]
    if offenders:
        shown = ", ".join(str(o) for o in offenders[:5])
        more = f" (+{len(offenders) - 5} more)" if len(offenders) > 5 else ""
        raise SyntheticTimingError(
            f"{context} refused: {len(offenders)} injected meeting(s) carry SYNTHETIC "
            f"timestamps generated by {SYNTHETIC_PARAMS['generator']}, not measurements: "
            f"{shown}{more}. Restrict the set to timing_source == 'real' meetings, or "
            f"report a metric that does not read segment times."
        )
