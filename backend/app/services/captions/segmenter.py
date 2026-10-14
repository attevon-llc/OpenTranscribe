"""Cut a transcript into broadcast-style caption cues — the ONE implementation.

A diarized segment is a speaker turn, and a turn can be a minute of speech. Shown as one
cue it covers the frame with a paragraph; that is what the file-detail player used to do.
This module turns segments into cues the way a subtitler would (rules and their sources
are in ``rules.py``):

1. **Speaker turns.** Consecutive segments by the same speaker form one turn, so many
   one-word segments read as one caption instead of a flicker of single words. Segments
   in an overlap group are kept as turns of their own.
2. **Hard breaks** inside a turn at every pause of ``pause_split`` seconds or more: a
   caption never stays up across silence.
3. **Sentences**, then **cues**: a sentence that fits one cue (two lines, ``max_duration``)
   is one cue; a longer one is cut at the best break point (``linebreak``) near where the
   cue fills up, or evenly in two when two cues are enough. Short neighbouring sentences
   are merged back ("Yes. I agree.").
4. **Lines**: each cue is laid out on at most two balanced lines.
5. **Timing** (``timing.py``), across the whole track: overlapping cues from two speakers become one
   two-line dialogue cue when each fits on a line, otherwise the earlier one is cut where
   the later starts; ends are held toward the reading-speed and minimum-duration targets
   while the timeline has room (never into the next cue, never more than ``max_linger``
   past the last word or beyond its segment); gaps shorter than ``close_gap_below`` are
   closed to a two-frame gap.

The speaker's name is shown on the first cue of every turn — and on both lines of a
dialogue cue — and never repeated on the continuation cues of one turn.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from dataclasses import field

from app.services.captions.linebreak import UNBREAKABLE
from app.services.captions.linebreak import TokenRun
from app.services.captions.linebreak import ends_sentence
from app.services.captions.model import CaptionSegment
from app.services.captions.model import CaptionTrack
from app.services.captions.model import Cue
from app.services.captions.model import CuePart
from app.services.captions.rules import CJK_RULES
from app.services.captions.rules import LATIN_RULES
from app.services.captions.rules import CaptionRules
from app.services.captions.timing import resolve_overlaps
from app.services.captions.timing import settle_timing
from app.services.captions.tokens import Token
from app.services.captions.tokens import tokenize_segment


@dataclass
class _Turn:
    speaker_key: object | None
    speaker_label: str | None
    tokens: list[Token] = field(default_factory=list)
    holds: list[float] = field(default_factory=list)  # each token's segment end
    cjk_chars: int = 0
    chars: int = 0
    overlap: bool = False


def _label_len(label: str | None) -> int:
    return len(label) + 2 if label else 0


def _cue_spans(run: TokenRun, rules: CaptionRules, label_len: int) -> list[tuple[int, int]]:
    """Token ranges, one per cue, for one speaker turn."""
    n = len(run.tokens)

    def lab(a: int) -> int:
        return label_len if a == 0 else 0

    def fits(a: int, b: int) -> bool:
        return run.fits(a, b, rules, lab(a))

    blocks: list[tuple[int, int]] = []
    start = 0
    for i in range(1, n):
        gap = run.tokens[i].start - run.tokens[i - 1].end
        if gap >= rules.pause_split and run.tokens[i].breakable_before:
            blocks.append((start, i))
            start = i
    blocks.append((start, n))

    spans: list[tuple[int, int]] = []
    for b_start, b_end in blocks:
        sentences: list[tuple[int, int]] = []
        s = b_start
        for i in range(b_start, b_end - 1):
            if ends_sentence(run.tokens[i].text) and run.tokens[i + 1].breakable_before:
                sentences.append((s, i + 1))
                s = i + 1
        sentences.append((s, b_end))
        pieces: list[tuple[int, int]] = []
        for a, b in sentences:
            pieces.extend(_split_sentence(run, a, b, fits, rules))
        spans.extend(_merge_short(run, pieces, rules, fits))
    return spans


def _split_sentence(
    run: TokenRun, a: int, b: int, fits, rules: CaptionRules
) -> list[tuple[int, int]]:
    """Cut ``tokens[a:b]`` into cue-sized ranges at the best break points."""
    out: list[tuple[int, int]] = []
    s = a
    while s < b:
        if fits(s, b):
            out.append((s, b))
            break
        e = s + 1
        while e < b and fits(s, e + 1):
            e += 1
        if not fits(s, e):
            out.append((s, s + 1))  # one token wider than a cue: shown alone
            s += 1
            continue
        k = _two_cue_split(run, s, b, fits, rules)
        if k is None:
            k = _greedy_break(run, s, e)
        out.append((s, k))
        s = k
    return out


def _two_cue_split(run: TokenRun, s: int, b: int, fits, rules: CaptionRules) -> int | None:
    """When ``tokens[s:b]`` fits in two cues, the most balanced natural split point."""
    total = run.length(s, b)
    if total > 2 * (rules.max_cue_chars + rules.max_lines):
        return None
    best: tuple[float, int] | None = None
    for k in range(s + 1, b):
        penalty = run.break_penalty(k)
        if penalty == UNBREAKABLE or not (fits(s, k) and fits(k, b)):
            continue
        score = abs(run.length(s, k) - run.length(k, b)) / max(total, 1) + 0.5 * penalty
        if best is None or score < best[0]:
            best = (score, k)
    return best[1] if best else None


def _greedy_break(run: TokenRun, s: int, e: int) -> int:
    """Best break in the back half of the fullest cue ``tokens[s:e]`` that fits."""
    lo = s + max(1, math.ceil((e - s) * 0.5))
    best: tuple[float, int] | None = None
    for k in range(lo, e + 1):
        if k >= len(run.tokens):
            break
        penalty = run.break_penalty(k)
        if penalty == UNBREAKABLE:
            continue
        score = penalty + 0.6 * (e - k) / max(e - s, 1)
        if best is None or score < best[0]:
            best = (score, k)
    return best[1] if best else e


def _merge_short(
    run: TokenRun, pieces: list[tuple[int, int]], rules: CaptionRules, fits
) -> list[tuple[int, int]]:
    """Merge neighbouring pieces when one is too short to read alone and both fit."""

    def short(a: int, b: int) -> bool:
        return (
            run.duration(a, b) < 1.5 * rules.min_duration
            or run.length(a, b) < rules.max_line_chars // 2
        )

    merged: list[tuple[int, int]] = []
    for a, b in pieces:
        if merged:
            pa, _ = merged[-1]
            if (short(pa, a) or short(a, b)) and fits(pa, b):
                merged[-1] = (pa, b)
                continue
        merged.append((a, b))
    return merged


def _build_turns(segments: list[CaptionSegment]) -> tuple[list[_Turn], int]:
    turns: list[_Turn] = []
    interpolated = 0
    for seg in sorted(segments, key=lambda s: (s.start, s.end)):
        text = (seg.text or "").strip()
        if not text:
            continue
        seg_tokens = tokenize_segment(
            text,
            start=float(seg.start),
            end=float(seg.end),
            words=seg.words,
            source_text=seg.source_text.strip() if seg.source_text is not None else None,
        )
        if not seg_tokens.tokens:
            continue
        interpolated += int(seg_tokens.interpolated)
        overlap = seg.overlap_group is not None
        current = turns[-1] if turns else None
        if (
            current is None
            or overlap
            or current.overlap
            or current.speaker_key != seg.speaker_key
            or current.speaker_label != seg.speaker_label
        ):
            current = _Turn(seg.speaker_key, seg.speaker_label, overlap=overlap)
            turns.append(current)
        first, *rest = seg_tokens.tokens
        # A new segment always starts a breakable unit, even mid-turn.
        current.tokens.append(
            Token(
                first.text,
                bool(current.tokens) and not seg_tokens.cjk,
                True,
                first.start,
                first.end,
                starts_segment=True,
            )
        )
        current.tokens.extend(rest)
        current.holds.extend([float(seg.end)] * len(seg_tokens.tokens))
        visible = len(text.replace(" ", ""))
        current.chars += visible
        current.cjk_chars += visible if seg_tokens.cjk else 0
    return turns, interpolated


def _turn_cues(turn: _Turn) -> list[Cue]:
    rules = CJK_RULES if turn.cjk_chars * 2 > turn.chars else LATIN_RULES
    run = TokenRun(turn.tokens)
    cues: list[Cue] = []
    for index, (a, b) in enumerate(_cue_spans(run, rules, _label_len(turn.speaker_label))):
        show_label = index == 0 and bool(turn.speaker_label)
        label_len = _label_len(turn.speaker_label) if show_label else 0
        layout = run.break_lines(a, b, rules, label_len, force=True) or [(a, b)]
        last_word_end = max(t.end for t in run.tokens[a:b])
        cues.append(
            Cue(
                start=run.tokens[a].start,
                end=last_word_end,
                parts=[
                    CuePart(
                        turn.speaker_key,
                        turn.speaker_label,
                        show_label,
                        [run.text(x, y) for x, y in layout],
                    )
                ],
                rules=rules,
                hold_until=max(last_word_end + rules.max_linger, max(turn.holds[a:b])),
            )
        )
    return cues


def build_caption_track(segments: list[CaptionSegment]) -> CaptionTrack:
    """Segment a transcript into caption cues.

    Args:
        segments: The transcript's segments, any order.

    Returns:
        The cues in time order, non-overlapping, with how many segments were timed by
        interpolation because they carried no usable word timestamps.
    """
    turns, interpolated = _build_turns(segments)
    cues: list[Cue] = []
    for turn in turns:
        cues.extend(_turn_cues(turn))
    cues = resolve_overlaps(cues)
    settle_timing(cues)
    return CaptionTrack(cues, len(segments), interpolated)
