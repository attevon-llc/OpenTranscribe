"""Per-script caption rules: what a line may not start with, what must not be split, and
how a script written without spaces is cut.

Everything here is dictionary-free on purpose (no new dependency, nothing to load on a CPU
worker), so each rule is a conservative structural one:

- **Kinsoku (Japanese/Chinese).** A line never starts with a closing mark, a small kana
  (the small ya/yu/yo/tsu in either kana), the long-vowel mark or an iteration mark, and
  never ends with an opening bracket. Those characters are glued to the character before
  them as one token (``tokens.py``), so no break can fall between them.
- **Words made of one script.** Katakana loanwords are never split; a hiragana particle
  or okurigana stays with the kanji it follows; a number stays with the counter that
  follows it (2027 + year mark, 450 + ten-thousand). Between two kanji (Chinese text)
  nothing is known, so nothing is preferred.
- **Scripts with no spaces at all (Thai, Lao, Khmer, Myanmar).** A run is cut only at, in
  order of preference, the recognizer's own word boundaries (word timestamps; when there
  are any they are the only cuts), then a change of script (a Thai word next to digits),
  and, for Thai only and only when the run is longer than
  ``UNSPACED_MIN_RUN``, a syllable boundary a vowel sign or a leading vowel guarantees. A
  cut inside a word is a last resort that costs more than any space or punctuation break;
  a run with none of these (Lao/Khmer/Myanmar without word timestamps) stays whole, exactly
  like a long URL. There is no Thai/Lao/Khmer dictionary here, so a cut can still fall
  inside a word when nothing better exists on the line.
- **Display width.** Combining marks (Thai above/below vowels and tone marks, Arabic
  diacritics, accents) take no column, so a line's length is counted without them.
"""

from __future__ import annotations

import re
import unicodedata

# Han, kana, CJK compatibility ideographs, iteration marks, full-width alphanumerics,
# half-width katakana.
CJK = (
    "\u3005-\u3007\u3040-\u30ff\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    "\uff10-\uff19\uff21-\uff3a\uff41-\uff5a\uff66-\uff9f"
)
HANGUL = "\u1100-\u11ff\u3130-\u318f\uac00-\ud7af"
# A line may not start with a closing mark nor end with an opening one (kinsoku shori).
OPEN = "\u300c\u300e\uff08\u3010\u3008\u300a\u201c\u2018\u3014\uff3b\uff5b"
CLOSE = (
    "\u3002\u3001\uff0c\uff0e\uff01\uff1f\uff1a\uff1b"
    "\u300d\u300f\uff09\u3011\u3009\u300b\u201d\u2019\u2026\u3015\uff3d\uff5d"
    "\u203c\u2047\u2048\u2049"
)
# Characters that may not start a line but are letters, so they are glued to the previous
# character rather than treated as punctuation: small kana, the long-vowel mark, kana and
# ideographic iteration marks, the katakana middle dot.
NO_LINE_START_LETTERS = (
    "\u3041\u3043\u3045\u3047\u3049\u3063\u3083\u3085\u3087\u308e\u3095\u3096"
    "\u30a1\u30a3\u30a5\u30a7\u30a9\u30c3\u30e3\u30e5\u30e7\u30ee\u30f5\u30f6"
    "\u30fc\u309d\u309e\u30fd\u30fe\u3005\u30fb"
)

# Thai, Lao, Myanmar, Khmer and New Tai Lue: scripts written without spaces between words.
UNSPACED = "\u0e00-\u0eff\u1000-\u109f\u1780-\u17ff\u19e0-\u19ff"
_UNSPACED_RE = re.compile(f"[{UNSPACED}]")
UNSPACED_MIN_RUN = 21

# The five vowels written BEFORE their consonant: they always open a syllable.
_THAI_LEADING = frozenset("\u0e40\u0e41\u0e42\u0e43\u0e44")
_THAI_VOWEL_SIGN = frozenset(
    {chr(c) for c in (0x0E30, 0x0E32, 0x0E33, *range(0x0E34, 0x0E3B), 0x0E45, 0x0E46)}
)
_THAI_TONE = frozenset({chr(c) for c in range(0x0E47, 0x0E4F)})
_THAI_RANGE = ("\u0e01", "\u0e5b")

#: A junction costing at least this splits a word (a loanword, a number from its counter);
#: a layout that needs one is rejected while any other layout is possible.
WORD_INTEGRITY = 1.0

#: Added to the break score before a token, per kind of cut (lower is better).
COST_ASR_WORD = 0.1
COST_SCRIPT_CHANGE = 0.3
COST_SYLLABLE = 1.0


def display_width(text: str) -> int:
    """Columns ``text`` occupies: its characters minus the zero-width combining marks."""
    return sum(1 for ch in text if unicodedata.category(ch) != "Mn")


def _kind(ch: str) -> str:
    code = ord(ch)
    if 0x3040 <= code <= 0x309F:
        return "hira"
    if 0x30A0 <= code <= 0x30FF or 0x31F0 <= code <= 0x31FF or 0xFF66 <= code <= 0xFF9F:
        return "kata"
    if 0x3005 <= code <= 0x3007 or 0x3400 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF:
        return "han"
    if ch.isdigit():
        return "digit"
    if ch.isalpha():
        return "latin"
    return "other"


def junction_cost(prev_text: str, next_text: str) -> float:
    """Extra cost of breaking between two adjacent CJK tokens, from the scripts they meet.

    Only called where no space separates the tokens. Punctuation on either side is its own
    reason to break (or not) and is scored elsewhere, so it returns 0 here.
    """
    if not prev_text or not next_text or prev_text[-1] in CLOSE or next_text[0] in OPEN:
        return 0.0
    before, after = _kind(prev_text[-1]), _kind(next_text[0])
    if before == "digit" and after in ("han", "hira", "kata"):
        return 1.0  # a number and the counter it counts (2027 + year, 450 + ten-thousand)
    if before == "kata" and after == "kata":
        return 1.5  # one loanword
    if before == "hira" and after == "hira":
        return 0.35
    if before in ("han", "kata") and after == "hira":
        return 0.35  # a particle or okurigana belongs to the word before it
    if before == "hira" and after in ("han", "kata", "latin", "digit"):
        return -0.25  # the usual phrase boundary: a particle, then the next content word
    return 0.0


def _thai_syllable_cuts(text: str) -> list[int]:
    cuts: list[int] = []
    for i in range(1, len(text)):
        ch, prev = text[i], text[i - 1]
        if not (_THAI_RANGE[0] <= ch <= _THAI_RANGE[1]) or prev in _THAI_LEADING:
            continue
        if ch in _THAI_LEADING:
            cuts.append(i)  # a leading vowel always opens a syllable
        elif "\u0e01" <= ch <= "\u0e2e" and (
            prev in _THAI_VOWEL_SIGN
            or (prev in _THAI_TONE and i > 1 and text[i - 2] in _THAI_VOWEL_SIGN)
        ):
            cuts.append(i)  # a consonant right after a vowel sign starts the next syllable
    return cuts


def unspaced_cuts(text: str, word_starts: list[int]) -> list[tuple[int, float]]:
    """Where a space-less run of Thai/Lao/Khmer/Myanmar text may be cut, with the cost.

    Args:
        text: One token as the tokenizer matched it (no whitespace inside).
        word_starts: Offsets inside ``text`` where the recognizer's word timestamps say a
            word begins (empty when there are none or the text was masked).

    Returns:
        ``[(offset, cost), ...]`` sorted by offset, offsets strictly inside the token; empty
        when the token is not such a script or is short enough to stay whole.
    """
    if not _UNSPACED_RE.search(text) or display_width(text) <= UNSPACED_MIN_RUN:
        return []
    found: dict[int, float] = {}
    for offset in word_starts:
        if 0 < offset < len(text):
            found[offset] = COST_ASR_WORD
    if found:
        return sorted(found.items())  # the recognizer knows where its words are; trust it
    for i in range(1, len(text)):
        if bool(_UNSPACED_RE.match(text[i])) != bool(_UNSPACED_RE.match(text[i - 1])):
            found[i] = COST_SCRIPT_CHANGE
    if "\u0e00" <= text[0] <= "\u0e7f":
        for i in _thai_syllable_cuts(text):
            found.setdefault(i, COST_SYLLABLE)
    return sorted(found.items())
