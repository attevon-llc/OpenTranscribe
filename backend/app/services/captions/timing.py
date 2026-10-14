"""Track-wide timing: make cues sequential and hold them long enough to read.

Runs after every speaker turn has been cut into cues (``segmenter``), because both rules
are about NEIGHBOURS, possibly from different speakers:

- **No overlaps.** Two speakers talking at once produce cues that overlap in time. When
  each fits on one line they become one two-line dialogue cue, each line labelled (Ofcom:
  two speakers in one subtitle, each on its own line); otherwise the earlier cue ends where
  the later begins. A cue shorter than ``min_duration`` followed within ``close_gap_below``
  by another speaker's short reply is merged the same way.
- **Readable durations.** Each cue's end is held toward ``chars / max_cps`` and at least
  ``min_duration`` while the timeline has room: never into the next cue (a two-frame gap
  is kept) and never past ``hold_until`` (``max_linger`` after its last word, or its
  segment's end). Gaps shorter than ``close_gap_below`` are closed to the two-frame gap so
  captions do not blink off between consecutive cues.
"""

from __future__ import annotations

from app.services.captions.model import Cue
from app.services.captions.model import CuePart


def _as_dialogue(first: Cue, second: Cue) -> Cue | None:
    """Two single-line cues from different speakers as one two-line dialogue cue."""
    if len(first.parts) != 1 or len(second.parts) != 1 or first.rules != second.rules:
        return None
    p1, p2 = first.parts[0], second.parts[0]
    if p1.speaker_key == p2.speaker_key or len(p1.lines) != 1 or len(p2.lines) != 1:
        return None
    parts = [
        CuePart(p.speaker_key, p.speaker_label, bool(p.speaker_label), list(p.lines))
        for p in (p1, p2)
    ]
    width = first.rules.max_line_chars
    if any(len(line) > width for part in parts for line in part.rendered_lines()):
        return None
    start, end = min(first.start, second.start), max(first.end, second.end)
    if end - start > first.rules.max_duration:
        return None
    return Cue(start, end, parts, first.rules, max(first.hold_until, second.hold_until))


def resolve_overlaps(cues: list[Cue]) -> list[Cue]:
    """Make the track sequential: no cue starts before the previous one has ended."""
    out: list[Cue] = []
    for cue in sorted(cues, key=lambda c: (c.start, c.end)):
        if not out:
            out.append(cue)
            continue
        prev = out[-1]
        rules = prev.rules
        overlapping = cue.start < prev.end
        rapid = (
            cue.start - prev.end < rules.close_gap_below
            and min(prev.end - prev.start, cue.end - cue.start) < rules.min_duration
        )
        if overlapping or rapid:
            dialogue = _as_dialogue(prev, cue)
            if dialogue is not None:
                out[-1] = dialogue
                continue
        if overlapping:
            floor = prev.start + min(rules.min_duration / 2, max(prev.end - prev.start, 0.0))
            if cue.start < floor:
                cue.end = max(cue.end, floor + rules.min_duration / 2)
                cue.start = floor
            prev.end = cue.start
        out.append(cue)
    return out


def settle_timing(cues: list[Cue]) -> None:
    """Hold ends toward reading speed / minimum duration, and close short gaps."""
    for i, cue in enumerate(cues):
        rules = cue.rules
        nxt = cues[i + 1] if i + 1 < len(cues) else None
        ceiling = cue.hold_until
        if nxt is not None:
            ceiling = min(ceiling, nxt.start - rules.frame_gap)
        wanted = min(max(rules.min_duration, cue.chars / rules.max_cps), rules.max_duration)
        if cue.end - cue.start < wanted:
            cue.end = max(cue.end, min(cue.start + wanted, ceiling))
        if nxt is not None and 0 < nxt.start - cue.end < rules.close_gap_below:
            cue.end = max(cue.end, nxt.start - rules.frame_gap)
        if nxt is not None:
            cue.end = min(cue.end, nxt.start)
        cue.end = min(cue.end, cue.start + rules.max_duration)
        if cue.end <= cue.start:
            room = (nxt.start - cue.start) if nxt is not None else rules.min_duration
            cue.end = cue.start + max(min(rules.min_duration, room), 0.001)
