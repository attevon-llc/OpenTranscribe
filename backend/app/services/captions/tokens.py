"""Turn one segment's display text into timed, breakable tokens.

A token is the smallest unit a cue or a line may be broken around: a word in a
space-separated script, a single character in Chinese/Japanese (which have no spaces),
a whole redaction placeholder such as ``[EMAIL]`` (so masking never produces a broken
``[EMA`` / ``IL]``), with attached punctuation glued on.

Timing comes from one monotone map from a character offset to a time:

- With **word timestamps**, each word's start/end become anchors at its character span
  in the ORIGINAL segment text, so a token's time is the time its word was spoken.
- Without them (older transcripts, edited text the words no longer match, providers
  that return segment timing only) the only anchors are the segment's own start and end,
  and token times are interpolated **in proportion to character position**. The caller
  is told (``SegmentTokens.interpolated``) so it can say so in the output.

Redaction runs before segmentation and changes the text (``john@x.com`` becomes
``[EMAIL]``), so word timestamps no longer line up with the text being captioned. The
display text is mapped back onto the original with a character diff, and every display
offset is timed at the original offset it came from: the placeholder is shown exactly
while the masked words were spoken, and the original words themselves are never read.
"""

from __future__ import annotations

import bisect
import difflib
import itertools
import re
from collections.abc import Callable
from dataclasses import dataclass

from app.services.captions.scripts import CJK as _CJK
from app.services.captions.scripts import CLOSE as _CLOSE
from app.services.captions.scripts import HANGUL as _HANGUL
from app.services.captions.scripts import NO_LINE_START_LETTERS as _NO_START
from app.services.captions.scripts import OPEN as _OPEN
from app.services.captions.scripts import unspaced_cuts
from app.services.redaction.spans import build_word_offsets

_TOKEN_RE = re.compile(
    rf"\[[^\[\]\n]{{1,80}}\]"
    rf"|[{_OPEN}]*[{_CJK}][{_CLOSE}{_NO_START}]*"
    rf"|[^\s{_CJK}{_OPEN}{_CLOSE}]+[{_CLOSE}]*"
    rf"|[{_OPEN}{_CLOSE}]+"
)
_CJK_CHAR_RE = re.compile(rf"[{_CJK}{_HANGUL}]")
_CJK_LEADING_RE = re.compile(rf"^[{_OPEN}]*[{_CJK}]")
_CJK_TRAILING_RE = re.compile(rf"[{_CJK}][{_CLOSE}]*$")


@dataclass(frozen=True)
class Token:
    """One breakable unit of caption text.

    Attributes:
        text: The characters shown.
        space_before: Whether a space separates it from the previous token when both
            sit on one line (False for CJK characters and glued punctuation).
        breakable_before: Whether a line or cue may start with this token.
        start: When it starts being spoken, seconds.
        end: When it stops being spoken, seconds.
        starts_segment: The first token of a transcript segment (the ASR's own phrase
            boundary, a good place to break when the text has no punctuation).
        cost: Extra penalty for breaking before this token: a cut inside a space-less
            run (Thai, ...) is never as good as a break at a space (``scripts.py``).
    """

    text: str
    space_before: bool
    breakable_before: bool
    start: float
    end: float
    starts_segment: bool = False
    cost: float = 0.0


@dataclass(frozen=True)
class SegmentTokens:
    """A segment's tokens and how their times were obtained."""

    tokens: list[Token]
    cjk: bool
    interpolated: bool


def is_cjk_text(text: str) -> bool:
    """Whether CJK characters dominate ``text`` enough to use the CJK line limits."""
    visible = [ch for ch in text if not ch.isspace()]
    if not visible:
        return False
    cjk = sum(1 for ch in visible if _CJK_CHAR_RE.match(ch))
    return cjk >= 0.3 * len(visible)


def _offset_map(source: str, display: str) -> Callable[[int], float]:
    """Map an offset in ``display`` to the offset in ``source`` it was derived from.

    Unchanged runs map one to one; a replaced run (a redaction placeholder) is spread
    proportionally over the original characters it replaced.
    """
    if source == display:
        return float
    blocks: list[tuple[int, int, int, int]] = []
    matcher = difflib.SequenceMatcher(None, source, display, autojunk=False)
    for _tag, i1, i2, j1, j2 in matcher.get_opcodes():
        blocks.append((j1, j2, i1, i2))
    starts = [b[0] for b in blocks]

    def mapped(offset: int) -> float:
        idx = max(0, bisect.bisect_right(starts, offset) - 1)
        j1, j2, i1, i2 = blocks[idx]
        if j2 <= j1:
            return float(i1)
        fraction = min(1.0, max(0.0, (offset - j1) / (j2 - j1)))
        return i1 + fraction * (i2 - i1)

    return mapped


def _time_map(
    source: str, words: list[dict] | None, seg_start: float, seg_end: float
) -> tuple[Callable[[float], float], bool]:
    """Build the character-offset to time map. Returns ``(map, interpolated)``."""
    seg_end = max(seg_end, seg_start)
    xs: list[float] = [0.0]
    ts: list[float] = [seg_start]
    timed_words = 0
    offsets = build_word_offsets(source, words)  # one entry per word, [] without words
    for (char_start, char_end), word in zip(offsets, words or [], strict=True):
        w_start, w_end = word.get("start"), word.get("end")
        if char_end <= char_start or not isinstance(w_start, (int, float)):
            continue
        if not isinstance(w_end, (int, float)) or w_end < w_start:
            w_end = w_start
        for x, t in ((float(char_start), float(w_start)), (float(char_end), float(w_end))):
            t = min(max(t, seg_start), seg_end)
            if x < xs[-1] or t < ts[-1]:
                continue  # a word out of order would make time run backwards
            xs.append(x)
            ts.append(t)
        timed_words += 1
    length = float(max(len(source), 1))
    if xs[-1] < length:
        xs.append(length)
        ts.append(max(seg_end, ts[-1]))

    def at(offset: float) -> float:
        idx = bisect.bisect_right(xs, offset)
        if idx <= 0:
            return ts[0]
        if idx >= len(xs):
            return ts[-1]
        x0, x1, t0, t1 = xs[idx - 1], xs[idx], ts[idx - 1], ts[idx]
        if x1 <= x0:
            return t1
        return t0 + (offset - x0) / (x1 - x0) * (t1 - t0)

    return at, timed_words == 0


def tokenize_segment(
    display_text: str,
    *,
    start: float,
    end: float,
    words: list[dict] | None = None,
    source_text: str | None = None,
) -> SegmentTokens:
    """Split a segment into timed tokens.

    Args:
        display_text: The text that will be shown (already masked).
        start: Segment start, seconds.
        end: Segment end, seconds.
        words: The segment's word timestamps (``[{"word", "start", "end"}, ...]``),
            aligned to ``source_text``.
        source_text: The original text the words belong to; defaults to
            ``display_text`` (nothing was masked).

    Returns:
        The tokens, whether CJK limits apply, and whether timing was interpolated.
    """
    source = display_text if source_text is None else source_text
    to_source = _offset_map(source, display_text)
    time_at, interpolated = _time_map(source, words, start, end)

    word_starts: list[int] = []
    if display_text == source:
        word_starts = [a for a, b in build_word_offsets(source, words) if b > a]

    tokens: list[Token] = []
    prev_end = 0
    prev_text = ""
    for match in _TOKEN_RE.finditer(display_text):
        text = match.group(0)
        spaced = match.start() > prev_end and display_text[prev_end : match.start()].isspace()
        cjk_boundary = bool(_CJK_LEADING_RE.match(text) or _CJK_TRAILING_RE.search(prev_text))
        starts_with_close = text[0] in _CLOSE or text[0] in _NO_START
        breakable = bool(tokens) and not starts_with_close and (spaced or cjk_boundary)
        inner = [s - match.start() for s in word_starts if match.start() < s < match.end()]
        cuts = unspaced_cuts(text, inner)
        bounds = [(0, 0.0), *cuts, (len(text), 0.0)]
        for index, ((a, cost), (b, _)) in enumerate(itertools.pairwise(bounds)):
            t0 = time_at(to_source(match.start() + a))
            t1 = max(t0, time_at(to_source(match.start() + b)))
            if index == 0:
                tokens.append(
                    Token(text[a:b], spaced and bool(tokens), breakable or not tokens, t0, t1)
                )
            else:
                tokens.append(Token(text[a:b], False, True, t0, t1, cost=cost))
        prev_end = match.end()
        prev_text = tokens[-1].text
    return SegmentTokens(tokens, is_cjk_text(display_text), interpolated)
