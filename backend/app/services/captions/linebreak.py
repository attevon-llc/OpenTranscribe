"""Where a caption may be broken, and how one cue is laid out on its (at most two) lines.

The same scoring decides both cue boundaries and line breaks, following the BBC and
Netflix guidance (see ``rules.py``): prefer to break after a sentence or clause, or
before a conjunction; never leave an article or preposition hanging at the end of a
line; never separate a number from the number or unit that follows it, nor two
capitalised words that are probably one name; prefer a pause in the speech over a
word gap with none. Lines are balanced, with a slight preference for a shorter top
line (the "pyramid" shape both guides recommend), and a single-word line is avoided
when the cue has more words to share.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field

from app.services.captions.rules import CaptionRules
from app.services.captions.tokens import Token

_SENTENCE_END_RE = re.compile(
    "[.!?\u2026\u3002\uff01\uff1f]['\"\u201d\u2019)\\]\u300d\u300f\uff09]*$"
)
_CLAUSE_END_RE = re.compile("(?:[,;:\u2014\u2013\uff0c\u3001\uff1b\uff1a]|--)['\"\u201d\u2019)]*$")
_INITIALS_RE = re.compile(r"^(?:[A-Za-z]\.)+$")
_NUMBER_RE = re.compile(r"^[$\u20ac\u00a3\u00a5#~+\-\u2212]?\d[\d.,:/]*(?:%|st|nd|rd|th|s)?$")
_PUNCT_STRIP = "\"'\u201c\u201d\u2018\u2019()[]{}.,;:!?\u2026"

_ABBREVIATIONS = frozenset(
    {"mr.", "mrs.", "ms.", "dr.", "prof.", "sr.", "jr.", "st.", "vs.", "mt.", "no.", "approx."}
)
_CONJUNCTIONS = frozenset(
    {
        "and", "but", "or", "nor", "so", "because", "which", "that", "when", "while", "if",
        "then", "who", "whom", "whose", "where", "although", "though", "since", "unless",
        "until", "whereas", "however",
    }
)  # fmt: skip
_LINE_END_FORBIDDEN = frozenset(
    {
        "a", "an", "the", "of", "to", "in", "on", "at", "for", "with", "from", "by", "my",
        "your", "his", "her", "its", "our", "their", "this", "these", "those", "as", "into",
        "about", "mr.", "mrs.", "ms.", "dr.", "prof.", "st.",
    }
)  # fmt: skip
_UNITS = frozenset(
    {
        "percent", "per", "million", "millions", "billion", "billions", "thousand",
        "hundred", "trillion", "dollars", "euros", "pounds", "yen", "am", "pm", "a.m.",
        "p.m.", "kg", "km", "mph", "kph", "%", "years", "year", "minutes", "minute",
        "seconds", "second", "hours", "hour", "days", "day", "weeks", "months", "degrees",
    }
)  # fmt: skip

UNBREAKABLE = float("inf")


def _bare(text: str) -> str:
    return text.strip(_PUNCT_STRIP).lower()


def ends_sentence(text: str) -> bool:
    """Whether a token closes a sentence (``Mr.``, ``U.S.`` and initials do not)."""
    if not _SENTENCE_END_RE.search(text):
        return False
    lowered = text.lower().rstrip("\"'\u201d\u2019)")
    return lowered not in _ABBREVIATIONS and not _INITIALS_RE.match(lowered)


def _is_capitalised_word(text: str) -> bool:
    return len(text) > 1 and text[0].isupper() and text[1:2].isalpha()


@dataclass
class TokenRun:
    """A sequence of tokens with prefix sums, so any span's laid-out length is O(1)."""

    tokens: list[Token]
    _chars: list[int] = field(init=False)
    _spaces: list[int] = field(init=False)

    def __post_init__(self) -> None:
        self._chars = [0]
        self._spaces = [0]
        for tok in self.tokens:
            self._chars.append(self._chars[-1] + len(tok.text))
            self._spaces.append(self._spaces[-1] + int(tok.space_before))

    def length(self, a: int, b: int) -> int:
        """Characters ``tokens[a:b]`` occupy on one line."""
        if b <= a:
            return 0
        return self._chars[b] - self._chars[a] + self._spaces[b] - self._spaces[a + 1]

    def text(self, a: int, b: int) -> str:
        """``tokens[a:b]`` joined as they would be displayed."""
        parts: list[str] = []
        for i in range(a, b):
            tok = self.tokens[i]
            if i > a and tok.space_before:
                parts.append(" ")
            parts.append(tok.text)
        return "".join(parts)

    def duration(self, a: int, b: int) -> float:
        """Seconds from the first token's start to the latest end in ``tokens[a:b]``."""
        return max(t.end for t in self.tokens[a:b]) - self.tokens[a].start

    def break_penalty(self, i: int) -> float:
        """Cost of breaking (a cue or a line) before ``tokens[i]``; lower is better."""
        if i <= 0 or i >= len(self.tokens):
            return 0.0
        tok, prev = self.tokens[i], self.tokens[i - 1]
        if not tok.breakable_before:
            return UNBREAKABLE
        penalty = 0.0
        if ends_sentence(prev.text):
            penalty -= 1.0
        elif _CLAUSE_END_RE.search(prev.text):
            penalty -= 0.6
        if tok.starts_segment:
            penalty -= 0.6
        prev_bare, next_bare = _bare(prev.text), _bare(tok.text)
        if next_bare in _CONJUNCTIONS:
            penalty -= 0.3
        if prev.text.lower() in _LINE_END_FORBIDDEN or prev_bare in _LINE_END_FORBIDDEN:
            penalty += 0.6
        if _NUMBER_RE.match(prev.text) and (_NUMBER_RE.match(tok.text) or next_bare in _UNITS):
            penalty += 0.8
        if (
            _is_capitalised_word(prev.text)
            and _is_capitalised_word(tok.text)
            and not _CLAUSE_END_RE.search(prev.text)
            and not ends_sentence(prev.text)
        ):
            penalty += 0.5
        pause = max(0.0, tok.start - prev.end)
        penalty -= min(pause, 1.0) * 0.8
        return penalty

    def break_lines(
        self, a: int, b: int, rules: CaptionRules, label_len: int = 0, *, force: bool = False
    ) -> list[tuple[int, int]] | None:
        """Lay ``tokens[a:b]`` out on at most ``rules.max_lines`` (two) lines.

        Args:
            a: First token index.
            b: One past the last token index.
            rules: The limits to honour.
            label_len: Characters a speaker label takes at the start of line one.
            force: Return the least-overflowing layout instead of ``None`` when no
                layout fits — used only for a cue that cannot be split any further
                (a single token wider than a line, such as a long URL).

        Returns:
            ``[(start, end), ...]`` token ranges per line, or ``None`` when the span
            does not fit and ``force`` is False.
        """
        width = rules.max_line_chars
        first_width = max(width - label_len, width // 2)
        if self.length(a, b) <= first_width:
            return [(a, b)]
        best: tuple[float, int] | None = None
        fallback: tuple[float, float, int] | None = None
        n = b - a
        for k in range(a + 1, b):
            penalty = self.break_penalty(k)
            if penalty == UNBREAKABLE:
                continue
            top, bottom = self.length(a, k), self.length(k, b)
            overflow = max(0, top - first_width) + max(0, bottom - width)
            score = abs(top + label_len - bottom) / width + 0.5 * penalty
            if top + label_len > bottom:
                score += 0.1
            if n > 3 and (k - a == 1 or b - k == 1):
                score += 0.6  # a lone word on a line, with others to share it
            if rules.max_lines >= 2 and overflow == 0:
                if best is None or score < best[0]:
                    best = (score, k)
            elif fallback is None or (overflow, score) < fallback[:2]:
                fallback = (float(overflow), score, k)
        if best is not None:
            return [(a, best[1]), (best[1], b)]
        if not force:
            return None
        if fallback is not None and rules.max_lines >= 2:
            return [(a, fallback[2]), (fallback[2], b)]
        return [(a, b)]

    def fits(self, a: int, b: int, rules: CaptionRules, label_len: int = 0) -> bool:
        """Whether ``tokens[a:b]`` is a legal cue: within the line and duration limits."""
        if b <= a:
            return False
        if self.length(a, b) + label_len > rules.max_cue_chars + rules.max_lines - 1:
            return False
        if b - a > 1 and self.duration(a, b) > rules.max_duration:
            return False
        return self.break_lines(a, b, rules, label_len) is not None
