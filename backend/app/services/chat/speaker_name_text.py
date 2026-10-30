"""Name normalization and script classification for speaker-mention matching.

The comparison form (:func:`_normalize`), the script classes that drive both
candidate extraction and the matching ladder, and the per-script fuzzy floor.
Shared by :mod:`speaker_roster`, :mod:`speaker_mention_extract` and
:mod:`speaker_name_match`; pure string logic, no database and no model.
"""

from __future__ import annotations

import re
import unicodedata

from app.core.constants import FUZZY_MATCH_THRESHOLD
from app.services.search.sentence_splitter import _NO_SPACE_CHAR_RE

# ``FUZZY_MATCH_THRESHOLD`` (``app.core.constants``) is the `difflib.SequenceMatcher.ratio()`
# floor for the fuzzy rung of the ladder. 0.85 tolerates a short typo ("Alise" -> "Alice")
# without conflating genuinely different short names ("Ann" vs "Anna" scores 0.857 — right on
# the edge on purpose; two people who choose names that close is rare enough that a possible
# false positive here is a better trade than losing every legitimate typo below it).

#: A typo is a small EDIT — a substitution, or an insertion/deletion of a
#: diacritic or doubled letter — not a truncation or extension by several
#: characters; that shape (a name plus unrelated trailing text) belongs to
#: the dedicated prefix rungs, which bound it explicitly (unbounded but
#: ANCHORED-or-substring for scriptio continua, capped by
#: :data:`_KOREAN_PARTICLE_MAX_CHARS` for Korean). Without this cap, a short
#: name's necessarily low :func:`_fuzzy_threshold` floor also accepted
#: candidates the prefix rungs deliberately declined — measured: "다나였습니다"
#: (Dana + a 4-syllable copula the Korean rung's particle cap correctly
#: rejects) still scored 0.5 against "다나" by the fuzzy formula alone,
#: clearing the 0.45 floor a genuine short typo needs. ``2`` does not affect
#: any existing Latin case: "Alicce"/"Alice" and "Ann"/"Anna" both differ by
#: exactly 1 character.
_FUZZY_MAX_LENGTH_DELTA = 2

# ---------------------------------------------------------------------------
# Script classification — drives both extraction and matching below.
# ---------------------------------------------------------------------------

#: Hangul syllable block. Korean is written WITH spaces (unlike the scripts in
#: ``_NO_SPACE_CHAR_RE``), but has no case, so it needs its own extraction path
#: (ordinary whitespace tokens) and its own matching rung (particles attach to
#: names, so a token is often the name PLUS a trailing grammatical particle).
_HANGUL_RE = re.compile("[가-힣]")

#: Scripts that are written WITH spaces but carry no case distinction at all —
#: Arabic, Hebrew, Devanagari. These tokenize like Latin text but cannot use
#: the capitalization-based candidate filter, so every token becomes a
#: candidate and the matching ladder's ambiguity rule (a tie declines) is what
#: guards against a false positive, in place of that filter.
_CASELESS_SPACED_RE = re.compile(
    "[֐-׿"  # Hebrew
    "؀-ۿ"  # Arabic
    "ݐ-ݿ"  # Arabic supplement
    "ऀ-ॿ"  # Devanagari
    "]"
)

#: One or more consecutive scriptio-continua characters — derived from
#: ``_NO_SPACE_CHAR_RE`` (imported, not re-derived) by repeating its pattern.
#: This is the closest analogue to a "word" these scripts have without a
#: segmenter: a run typically contains a name PLUS trailing text with no
#: boundary between them, which is exactly what the grapheme-prefix rung in
#: :func:`match_candidate` is for.
_NO_SPACE_RUN_RE = re.compile(_NO_SPACE_CHAR_RE.pattern + "+")

#: A run of Unicode letters, for tokenizing spaced scripts (Korean, Arabic,
#: Hebrew, Devanagari) without a language-specific tokenizer. Matches Latin
#: text too; callers filter to the scripts they care about so this pass never
#: reprocesses what :func:`extract_candidates` already covers.
#:
#: ``[^\W\d_]`` alone (plain "word characters") is NOT enough: Devanagari,
#: Hebrew and Arabic spell a syllable as a base consonant plus a COMBINING
#: mark (a matra/vowel sign, Unicode category Mn) that ``\w`` does not treat
#: as a word character. Measured before this union: "दाना" ("Dana") split
#: into two single-consonant fragments ("द", "न") with the combining vowel
#: signs silently dropped as boundaries — every Devanagari/Hebrew/Arabic
#: candidate came out shredded to isolated consonants. The alternation
#: below re-derives the caseless-spaced ranges from :data:`_CASELESS_SPACED_RE`
#: (not a second hand-written class) so a combining mark in THOSE scripts
#: stays attached to its base letter.
_TOKEN_RE = re.compile(r"(?:[^\W\d_]|[" + _CASELESS_SPACED_RE.pattern[1:-1] + r"])+", re.UNICODE)

#: Korean particles (조사) are almost always one or two syllable blocks —
#: 이/가/은/는/을/를/의/와/과/도/만 (1), 에서/으로/한테/에게 (2). A token that
#: extends a roster name by more than this is more likely a different word
#: entirely than a name plus a longer, rarer particle.
_KOREAN_PARTICLE_MAX_CHARS = 2


def _entry_script(name: str) -> str:
    """Classify a roster entry's script for matching purposes.

    Checked in this order because the ranges are disjoint by construction
    (no character in ``_NO_SPACE_CHAR_RE`` is also Hangul or in the
    caseless-spaced set), so order does not affect correctness — it is
    written narrowest-first only for readability.
    """
    norm = _normalize(name)
    if _NO_SPACE_CHAR_RE.search(norm):
        return "no_space"
    if _HANGUL_RE.search(norm):
        return "korean"
    if _CASELESS_SPACED_RE.search(norm):
        return "caseless_spaced"
    return "cased"


def _fuzzy_threshold(entry_name_norm: str, script: str) -> float | None:
    """The fuzzy-match floor for one roster entry, or ``None`` to skip fuzzy entirely.

    ``difflib.SequenceMatcher.ratio()`` is edit-distance-shaped: a single
    one-character substitution in a name of normalized length ``n`` scores
    ``(n-1)/n`` — measured 0.500 for a 2-character CJK/Japanese/Korean/Thai
    name, 0.667 for 3, 0.750-0.857 for a 4-character Arabic/Devanagari name.
    :data:`FUZZY_MATCH_THRESHOLD` (0.85) was calibrated against 4-8 character
    Western given names and refuses every one of those outright — a real typo
    on a short name never reaches it, so the fuzzy rung was silently a no-op
    for these scripts.

    The floor here is the computed single-substitution ratio minus a small
    margin (0.05), floored at 0.35 and capped at the Latin constant so no
    script gets a MORE permissive floor than English. A lower floor for a
    short name is safe, not merely convenient: the ladder declines on any
    TIE rather than guessing (the module's existing rule), so a coincidental
    match against an unrelated short name costs a decline, never a wrong
    match, unless it happens to be the unique closest entry — the same
    residual risk the flat 0.85 constant already accepts for "Ann"/"Anna".
    Names of normalized length 0-1 skip fuzzy matching altogether: a single
    character's ratio against anything is either 1.0 (already caught by the
    exact rung) or 0.0, so there is nothing a threshold could usefully gate.

    Args:
        entry_name_norm: The roster entry's name, already NFKC+casefolded.
        script: One of ``_entry_script``'s return values.

    Returns:
        The ratio floor to require, or ``None`` when fuzzy matching this
        entry is not meaningful.
    """
    if script == "cased":
        return FUZZY_MATCH_THRESHOLD
    n = len(entry_name_norm)
    if n <= 1:
        return None
    computed = (n - 1) / n - 0.05
    return max(0.35, min(computed, FUZZY_MATCH_THRESHOLD))


def _normalize(s: str) -> str:
    """NFKC-normalize and casefold, the one comparison form every rung uses."""
    return unicodedata.normalize("NFKC", s).casefold()


def _tokens(s: str) -> frozenset[str]:
    return frozenset(_normalize(s).split())
