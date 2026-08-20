"""Deterministic, Postgres-only speaker-mention resolution (W2.2, issue #403 family).

Matches a name typed in the question text ("what did Dana say about pricing?")
against the caller's *accessible* speaker roster, purely with SQL + string
matching — no embedding, no LLM call, no OpenSearch round trip — so a no-LLM
deployment (#403 D6) keeps this working exactly like every other flag-gated
retrieval knob.

**Soft, never a silent hard filter.** A resolved mention is evidence for a
PARALLEL second retrieval leg (see ``chat/retrieval.py``'s
``speaker_focus_names``), never a replacement for or a narrowing of the main
leg. An **explicit** checkbox scope (``ChatScope.speakers``, threaded as the
``speakers`` argument elsewhere in this package) is a different, HARD axis and
is untouched by anything in this module. Ambiguity resolves to no filter at
all — the caller surfaces ``ChatWarningCode.AMBIGUOUS_SPEAKER`` with the
candidate names instead of guessing.

**Script-aware, not English-only.** Candidate extraction has two tracks. Latin
capitalization conventions (``extract_candidates``) still drive the original
track. A second track (``extract_script_aware_candidates``) covers scripts
where capitalization carries no signal at all: scriptio-continua scripts
(CJK ideographs, kana, Thai/Lao/Myanmar/Khmer — no word boundaries, so ``\\b``
never matches inside them) extract as maximal same-script RUNS instead of
words, and spaced-but-caseless scripts (Hangul, Arabic, Hebrew, Devanagari)
extract as ordinary whitespace tokens with no common-word/sentence-initial
filter, because that filter exists to use a capitalization signal these
scripts do not have. :func:`match_candidate` carries the matching side: a
grapheme-level prefix rung for scriptio-continua names, a suffix-tolerant
prefix rung for Korean (particles attach to names), and a fuzzy-match floor
that scales with name length rather than one flat constant — the flat Latin
floor never lets a plausible one-character typo in a 2-3 character name
through. What no track adds is a CJK/Thai *segmenter* — deliberately out of
scope (no new dependency); a scriptio-continua candidate is a whole run, and
recovering the name from it is the prefix rung's job, not the extractor's.
"""

from __future__ import annotations

import difflib
import logging
import re
import unicodedata
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.models.media import MediaFile
from app.models.media import Speaker
from app.services.search.chunking_service import _NO_SPACE_CHAR_RE
from app.utils.speaker_labels import UNKNOWN_SPEAKER_LABELS
from app.utils.speaker_labels import canonical_speaker_label

logger = logging.getLogger(__name__)

#: Roster size guard. Above this many DISTINCT canonical names, matching
#: degrades from "occasionally wrong" to "expensive and still occasionally
#: wrong" (every candidate must be scored against the whole roster three
#: ways), so a caller this large gets a clean decline instead.
ROSTER_DISTINCT_CAP = 500

#: Upper bound on raw Speaker rows read while building a roster, independent
#: of the distinct-name cap above — a pathological library with many rows per
#: name (re-diarized recordings, repeated relabeling) must not turn one chat
#: turn into an unbounded scan.
_ROSTER_ROW_CAP = 20_000

#: How many matched / ambiguous / rejected entries `SpeakerMentionResolution`
#: carries. This is diagnostic payload persisted on every turn's
#: `msg_metadata.speaker_resolution` — unbounded growth there is the same
#: class of defect the digest/plan diagnostics elsewhere in this package
#: already cap.
MAX_RESOLUTION_ITEMS = 10

#: `difflib.SequenceMatcher.ratio()` floor for the fuzzy rung of the ladder.
#: 0.85 tolerates a short typo ("Alise" -> "Alice") without conflating
#: genuinely different short names ("Ann" vs "Anna" scores 0.857 — right on
#: the edge on purpose; two people who choose names that close is rare enough
#: that a possible false positive here is a better trade than losing every
#: legitimate typo below it).
FUZZY_MATCH_THRESHOLD = 0.85

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


@dataclass(frozen=True)
class RosterEntry:
    """One person a chat turn could plausibly be asked about."""

    name: str
    profile_id: int | None
    file_count: int


@dataclass(frozen=True)
class Roster:
    """The caller's accessible, non-quarantined speaker roster.

    ``declined`` means the roster is too large to match against safely
    (:data:`ROSTER_DISTINCT_CAP`) — ``entries`` is empty in that case, and the
    caller should resolve no mentions at all rather than degrade to a partial
    or slow match.
    """

    entries: tuple[RosterEntry, ...] = ()
    declined: bool = False


def build_roster(db: Session, user_id: int, *, organization_id: OrgScope = UNSCOPED) -> Roster:
    """Build the roster by joining ``Speaker -> MediaFile -> accessible files``.

    ⚠️ **Never `Speaker.user_id`.** That is the file *owner's* id, and scoping
    the roster to it would silently drop every speaker on a recording shared
    WITH this user — the same class of bug #385 fixed for tags. Access is
    resolved through
    :meth:`PermissionService.get_accessible_file_ids_subquery`, the single
    sharing authority this package already routes every other axis through
    (see ``context_resolver.py``), and quarantined files are excluded
    explicitly — the accessible-files subquery alone does not filter them,
    matching `_get_unique_speakers_for_filter`'s bug fixed alongside this
    module (`api/endpoints/speakers.py`).

    Canonical labels come from :func:`canonical_speaker_label`, the single
    home for speaker display-name resolution, so this roster names people
    exactly the way the chunk index, the digest plane and every other reader
    do. Rows resolving to :data:`UNKNOWN_SPEAKER_LABELS` are excluded — "who
    said X" about an unlabeled diarization slot is not a mention anyone could
    type.

    Returns:
        A :class:`Roster`. ``declined=True`` (empty ``entries``) when the
        caller's distinct-name count exceeds :data:`ROSTER_DISTINCT_CAP`.
    """
    from app.services.permission_service import PermissionService

    accessible_sq = PermissionService.get_accessible_file_ids_subquery(
        db, user_id, organization_id=organization_id
    )
    rows = (
        db.query(
            Speaker.name,
            Speaker.display_name,
            Speaker.suggested_name,
            Speaker.confidence,
            Speaker.profile_id,
            Speaker.media_file_id,
        )
        .join(MediaFile, MediaFile.id == Speaker.media_file_id)
        .filter(
            Speaker.media_file_id.in_(select(accessible_sq)),
            MediaFile.is_quarantined.is_(False),
        )
        .limit(_ROSTER_ROW_CAP)
        .all()
    )

    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        label = canonical_speaker_label(
            row.name,
            display_name=row.display_name,
            suggested_name=row.suggested_name,
            confidence=row.confidence,
        )
        if label in UNKNOWN_SPEAKER_LABELS:
            continue
        bucket = grouped.setdefault(label, {"profile_id": None, "files": set()})
        bucket["files"].add(row.media_file_id)
        if row.profile_id is not None and bucket["profile_id"] is None:
            bucket["profile_id"] = row.profile_id

    if len(grouped) > ROSTER_DISTINCT_CAP:
        logger.info(
            "Speaker roster declined for user %s: %d distinct names > cap %d",
            user_id,
            len(grouped),
            ROSTER_DISTINCT_CAP,
        )
        return Roster(entries=(), declined=True)

    entries = tuple(
        sorted(
            (
                RosterEntry(name=name, profile_id=data["profile_id"], file_count=len(data["files"]))
                for name, data in grouped.items()
            ),
            key=lambda e: (-e.file_count, e.name.lower()),
        )
    )
    return Roster(entries=entries)


# ---------------------------------------------------------------------------
# Candidate extraction — English-first capitalization heuristics.
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")

#: Common English words that are ALSO real first names ("Will", "Grace",
#: "Hope", "May", ...). A capitalized instance of one of these at the very
#: start of a sentence is orthographic convention, not evidence of a proper
#: noun — English capitalizes the first word of every sentence regardless.
#: Mid-sentence capitalization of the same word ("Did Grace present the
#: report?") IS evidence, because nothing else would capitalize it there.
_COMMON_WORD_NAMES: frozenset[str] = frozenset(
    {
        "will",
        "grace",
        "hope",
        "faith",
        "may",
        "june",
        "art",
        "bill",
        "rose",
        "jean",
        "victor",
        "hunter",
        "chase",
        "summer",
        "autumn",
        "dawn",
        "sky",
        "pat",
        "chris",
        "drew",
        "jordan",
        "robin",
        "dale",
        "mark",
        "gene",
        "frank",
        "sandy",
        "joy",
        "rich",
        "max",
        "ray",
        "gus",
        "trip",
    }
)

#: Question words, auxiliaries and pronouns that open the overwhelming
#: majority of chat questions and are for practical purposes NEVER a
#: person's display name. Unlike :data:`_COMMON_WORD_NAMES`, these are
#: excluded UNCONDITIONALLY — not just at sentence start — because a word
#: here is never plausible evidence either way, so there is no mid-sentence
#: exception to make. This is what stops "Did Grace present the report?"
#: from merging into the single multi-word candidate "Did Grace": without
#: it, the run-building loop below has no reason not to join two adjacent
#: capitalized words regardless of whether the first one is a name at all.
_NEVER_NAME_WORDS: frozenset[str] = frozenset(
    {
        "i",
        "we",
        "you",
        "they",
        "he",
        "she",
        "it",
        "did",
        "do",
        "does",
        "what",
        "who",
        "how",
        "when",
        "where",
        "why",
        "which",
        "is",
        "are",
        "was",
        "were",
        "has",
        "have",
        "had",
        "can",
        "could",
        "should",
        "would",
        "the",
        "this",
        "that",
        "these",
        "those",
        "please",
        "tell",
        "give",
        "describe",
        "explain",
        "list",
        "show",
    }
)


def _is_sentence_initial(text: str, pos: int) -> bool:
    """Whether the word starting at ``pos`` opens ``text`` or follows ``. ! ?``.

    Deliberately simple: strip trailing whitespace from everything before
    ``pos`` and check whether what remains ends in sentence-ending
    punctuation (or is empty, i.e. this is the very first word). A capitalized
    common-word candidate ("Grace, can you help?") that opens its own sentence
    is therefore NOT treated as a name — English capitalizes sentence-initial
    words regardless of part of speech, so that position carries no signal by
    itself. This is a known, accepted false negative: resolving it needs
    context this deterministic pass does not have.
    """
    prefix = text[:pos]
    tail = prefix.rstrip()
    if not tail:
        return True
    return tail[-1] in ".!?"


def extract_candidates(text: str) -> list[str]:
    """Capitalized-word candidate phrases, in the order they appear.

    Multi-word runs of consecutive capitalized tokens (only whitespace
    between them) are extracted as ONE phrase — "Alice Chen" is a single
    candidate, never two — because multi-word names are first-class and
    splitting them would only ever produce a worse match. A single-token
    candidate that is also a common English word (:data:`_COMMON_WORD_NAMES`)
    is dropped unless it is capitalized **mid-sentence** (see
    :func:`_is_sentence_initial`).

    Returns:
        Candidate phrases, duplicates included (the caller dedupes by
        normalized form so repeated mentions cost one match, not N).
    """
    matches = list(_WORD_RE.finditer(text))
    candidates: list[str] = []
    i = 0
    n = len(matches)
    while i < n:
        m = matches[i]
        word = m.group(0)
        if not word[0].isupper():
            i += 1
            continue
        if word.lower() in _NEVER_NAME_WORDS:
            # Never a plausible name, at any position — and critically, never
            # a valid START of a multi-word run either, or "Did Grace" would
            # merge into one candidate before the mid-sentence check for
            # "Grace" alone ever gets a chance to run.
            i += 1
            continue

        run_words = [word]
        run_end = m.end()
        j = i + 1
        while j < n:
            gap = text[run_end : matches[j].start()]
            nxt = matches[j].group(0)
            if gap.strip() == "" and nxt[0].isupper() and nxt.lower() not in _NEVER_NAME_WORDS:
                run_words.append(nxt)
                run_end = matches[j].end()
                j += 1
            else:
                break

        if len(run_words) > 1:
            candidates.append(" ".join(run_words))
            i = j
            continue

        if word.lower() in _COMMON_WORD_NAMES and _is_sentence_initial(text, m.start()):
            i += 1
            continue

        candidates.append(word)
        i += 1

    return candidates


def _no_space_runs(text: str) -> list[str]:
    """Maximal runs of scriptio-continua characters in *text*.

    Stands in for word-boundary extraction, which these scripts do not have
    (``\\b`` never matches inside a run of them). A run is typically the
    closest thing to a "word" available without a segmenter — often longer
    than the name it contains, since nothing marks where the name ends and
    the next word begins. :func:`match_candidate`'s grapheme-prefix rung is
    what recovers the name from a run like "达娜说了什么" ("what did Dana say").
    """
    return [m.group(0) for m in _NO_SPACE_RUN_RE.finditer(text)]


def extract_script_aware_candidates(text: str) -> list[str]:
    """Non-Latin candidate phrases that :func:`extract_candidates` cannot see at all.

    ``extract_candidates``'s ``_WORD_RE`` only matches ``[A-Za-z]``, so a
    Chinese, Japanese, Korean, Thai, Arabic or Hindi name in the question was
    previously never even attempted against the roster — independent of
    anything the matching ladder does, because no candidate reached it.

    Two extraction strategies, chosen by script:

    - **Scriptio continua** (CJK ideographs, kana, Thai/Lao/Myanmar/Khmer):
      candidates are maximal runs (:func:`_no_space_runs`), broken at the
      first character outside that script.
    - **Spaced, caseless scripts** (Hangul, Arabic, Hebrew, Devanagari): the
      text already has whitespace-delimited tokens, so ordinary tokenization
      applies. Unlike the Latin pass there is no common-word/sentence-initial
      filter here — that filter exists specifically to use a capitalization
      signal these scripts do not carry. Every token becomes a candidate, and
      the matching ladder's ambiguity rule (a tie declines rather than
      guesses) is what stands in for it.

    Args:
        text: The user's question, as typed.

    Returns:
        Candidate phrases, duplicates included — the caller
        (:func:`resolve_speaker_mentions`) dedupes by normalized form, the
        same as :func:`extract_candidates`'s output.
    """
    candidates: list[str] = list(_no_space_runs(text))
    for token in _TOKEN_RE.findall(text):
        if _HANGUL_RE.search(token) or _CASELESS_SPACED_RE.search(token):
            candidates.append(token)
    return candidates


# ---------------------------------------------------------------------------
# Matching ladder: NFKC + casefold, then exact -> unique token-subset -> fuzzy.
# ---------------------------------------------------------------------------


def _normalize(s: str) -> str:
    """NFKC-normalize and casefold, the one comparison form every rung uses."""
    return unicodedata.normalize("NFKC", s).casefold()


def _tokens(s: str) -> frozenset[str]:
    return frozenset(_normalize(s).split())


@dataclass(frozen=True)
class _MatchOutcome:
    matched: str | None
    ambiguous_with: tuple[str, ...]
    reason: str


def _prefix_rung(
    norm_candidate: str,
    roster: Roster,
    script: str,
    *,
    max_extra_chars: int | None,
    anchored: bool,
) -> _MatchOutcome | None:
    """One prefix-based rung: roster entries of *script* whose normalized name
    is a PREFIX of *norm_candidate* — or, when ``anchored`` is False, a prefix
    starting at ANY position (equivalently, a substring). Ties break on the
    LONGEST matching name — a longer specific match accounts for more of the
    candidate than a coincidentally-matching shorter one — and only a genuine
    tie at the max length is reported as ambiguous.

    ``anchored=False`` is what the scriptio-continua rung needs: a run has no
    internal boundaries at all, so a name is as likely to sit mid-run ("那次
    会议上达娜说了什么" — "达娜" starts at position 5, not 0) as at its start,
    and requiring position 0 missed the common case outright. ``anchored=True``
    is what the Korean rung needs instead: its candidate is already ONE
    whitespace-isolated token, so the only thing that can follow the name
    within it is a particle, never a second word — matching from any
    position there would let the rung swallow a name mid-word inside an
    unrelated token, with a much weaker structural reason to expect one.
    ``max_extra_chars`` bounds the leftover after the match (``None`` =
    unbounded, since a scriptio-continua run's leftover is unrelated
    following text with no boundary to stop at, not a particle).

    Returns ``None`` (not an outcome) when no entry of this script is even a
    candidate, so the caller can fall through to the next rung instead of
    reporting a hard miss prematurely.
    """
    hits = []
    for e in roster.entries:
        if _entry_script(e.name) != script:
            continue
        name_norm = _normalize(e.name)
        if not name_norm or norm_candidate == name_norm:  # exact rung owns equality
            continue
        if anchored:
            if not norm_candidate.startswith(name_norm):
                continue
            extra = len(norm_candidate) - len(name_norm)
        else:
            if name_norm not in norm_candidate:
                continue
            extra = 0  # position, not trailing length, is what matters unanchored
        if max_extra_chars is not None and extra > max_extra_chars:
            continue
        hits.append(e)
    if not hits:
        return None
    max_len = max(len(_normalize(e.name)) for e in hits)
    longest = [e for e in hits if len(_normalize(e.name)) == max_len]
    if len(longest) == 1:
        return _MatchOutcome(longest[0].name, (), "")
    return _MatchOutcome(None, tuple(e.name for e in longest), "")


def match_candidate(candidate: str, roster: Roster) -> _MatchOutcome:
    """Run one candidate through the ladder: exact -> token-subset ->
    grapheme-prefix (scriptio continua) -> suffix-tolerant prefix (Korean) -> fuzzy.

    Every rung requires a UNIQUE hit to resolve; two or more roster entries
    tying at any rung is ambiguity, not a pick between them — per the design
    constraint, ambiguity means no filter, ever, never a best-effort guess.

    The two prefix rungs are script-scoped (only scriptio-continua roster
    entries compete on the first, only Korean entries on the second), so a
    mixed-script roster never lets one script's rung swallow a candidate that
    was headed for another. The fuzzy rung's threshold is chosen PER ENTRY
    by :func:`_fuzzy_threshold` — a mixed roster can therefore hold, say, a
    2-character Chinese name and an 8-character English one, each scored
    against a floor appropriate to its own length and script.

    Returns:
        A :class:`_MatchOutcome`. ``matched`` is set only on a unique hit;
        ``ambiguous_with`` lists the tied roster names when the candidate hit
        more than one; ``reason`` explains a total miss (empty string on a
        match or an ambiguity, since those are self-explanatory).
    """
    norm_candidate = _normalize(candidate)
    if not norm_candidate:
        return _MatchOutcome(None, (), "empty")

    exact = [e for e in roster.entries if _normalize(e.name) == norm_candidate]
    if len(exact) == 1:
        return _MatchOutcome(exact[0].name, (), "")
    if len(exact) > 1:
        return _MatchOutcome(None, tuple(e.name for e in exact), "")

    cand_tokens = _tokens(candidate)
    if cand_tokens:
        subset = [e for e in roster.entries if cand_tokens <= _tokens(e.name)]
        if len(subset) == 1:
            return _MatchOutcome(subset[0].name, (), "")
        if len(subset) > 1:
            return _MatchOutcome(None, tuple(e.name for e in subset), "")

    no_space_outcome = _prefix_rung(
        norm_candidate, roster, "no_space", max_extra_chars=None, anchored=False
    )
    if no_space_outcome is not None:
        return no_space_outcome

    korean_outcome = _prefix_rung(
        norm_candidate,
        roster,
        "korean",
        max_extra_chars=_KOREAN_PARTICLE_MAX_CHARS,
        anchored=True,
    )
    if korean_outcome is not None:
        return korean_outcome

    scored: list[tuple[float, str]] = []
    for entry in roster.entries:
        entry_norm = _normalize(entry.name)
        threshold = _fuzzy_threshold(entry_norm, _entry_script(entry.name))
        if threshold is None:
            continue
        if abs(len(norm_candidate) - len(entry_norm)) > _FUZZY_MAX_LENGTH_DELTA:
            continue
        ratio = difflib.SequenceMatcher(None, norm_candidate, entry_norm).ratio()
        if ratio >= threshold:
            scored.append((ratio, entry.name))
    if scored:
        best = max(ratio for ratio, _ in scored)
        best_names = tuple(name for ratio, name in scored if ratio == best)
        if len(best_names) == 1:
            return _MatchOutcome(best_names[0], (), "")
        return _MatchOutcome(None, best_names, "")

    return _MatchOutcome(None, (), "no_roster_match")


# ---------------------------------------------------------------------------
# Speaker-verb frame — the signal that a mention is being asked "about what
# they SAID", not merely named in passing ("the meeting with Dana").
# ---------------------------------------------------------------------------

_SPEAKER_VERB_RE = re.compile(
    r"\b("
    r"said|say|says|saying|"
    r"mention(?:ed|s|ing)?|"
    r"talk(?:ed|s|ing)?(?:\s+about)?|"
    r"discuss(?:ed|es|ing)?|"
    r"ask(?:ed|s|ing)?|"
    r"answer(?:ed|s|ing)?|"
    r"explain(?:ed|s|ing)?|"
    r"argu(?:ed|es|ing)|"
    r"not(?:ed|es|ing)|"
    r"stat(?:ed|es|ing)|"
    r"claim(?:ed|s|ing)?|"
    r"bring(?:s)?\s+up|brought\s+up|"
    r"commit(?:ted|s|ting)?|"
    r"respond(?:ed|s|ing)?|"
    r"repl(?:y|ies|ied|ying)|"
    r"suggest(?:ed|s|ing)?|"
    r"propos(?:ed|es|ing)|"
    r"report(?:ed|s|ing)?|"
    # #523: "what did the Marketing role CONTRIBUTE" — the probe that found
    # this lexicon gap. A contribution frame is "what did X say/add/bring to
    # this", the same shape `bring(s)? up` already covers for a narrower verb.
    r"contribut(?:e|es|ed|ing)|"
    r"think|thinks|thought|"
    r"believ(?:ed|es|ing)|"
    r"feel[s]?|felt|"
    r"opinion|thoughts?\s+on|(?:take|view|perspective)\s+on|"
    r"promis(?:ed|es|ing)|"
    r"agree[ds]?|agreeing|"
    r"object(?:ed|s|ing)|"
    r"recommend(?:ed|s|ing)?"
    r")\b",
    re.IGNORECASE,
)


def has_speaker_verb_frame(text: str) -> bool:
    """Whether ``text`` contains a verb frame consistent with "what did X say".

    Deliberately a flat lexicon match over the whole question rather than a
    proximity/dependency check against the matched name — this module has no
    parser, and requiring the verb to sit next to the name would miss the
    common "Did Dana mention pricing in last week's call?" shape where the
    subject and the verb are adjacent but many other phrasings are not.
    """
    return bool(_SPEAKER_VERB_RE.search(text))


# ---------------------------------------------------------------------------
# Top-level resolution.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SpeakerMentionResolution:
    """What one question's speaker mentions resolved to.

    ``speaker_focus`` is True exactly when there is at least one uniquely
    matched name AND the question carries a speaker-verb frame — the
    condition the caller uses to decide whether to add the parallel
    speaker-scoped retrieval leg (never to narrow or replace the main one).
    """

    matched: tuple[str, ...] = field(default_factory=tuple)
    ambiguous: tuple[str, ...] = field(default_factory=tuple)
    rejected: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    speaker_focus: bool = False
    declined: bool = False

    def as_meta(self) -> dict[str, Any]:
        """Shape for ``msg_metadata.speaker_resolution`` (size-capped already)."""
        payload: dict[str, Any] = {}
        if self.matched:
            payload["matched"] = list(self.matched)
        if self.ambiguous:
            payload["ambiguous"] = list(self.ambiguous)
        if self.declined:
            payload["declined"] = True
        return payload


def resolve_speaker_mentions(
    db: Session,
    question: str,
    *,
    user_id: int,
    organization_id: OrgScope = UNSCOPED,
    roster: Roster | None = None,
) -> SpeakerMentionResolution:
    """Resolve every name candidate in ``question`` against the roster.

    Candidates come from both extraction tracks — :func:`extract_candidates`
    (Latin capitalization) and :func:`extract_script_aware_candidates`
    (scriptio-continua runs plus Hangul/Arabic/Hebrew/Devanagari tokens) — so
    a roster mixing, say, an English and a Chinese name is matched against
    equally regardless of which script the question itself is written in.

    Args:
        db: A short-lived session — this function issues exactly the roster
            query (unless ``roster`` is already supplied) and nothing else;
            it does not itself manage session lifetime beyond that one call.
        question: The user's message, as typed (NOT the rewritten query — a
            rewrite can lose or paraphrase a name the original carried).
        user_id: The caller. Roster access is resolved with the same sharing
            rule every other axis in this package uses.
        organization_id: Active tenant scope, or ``UNSCOPED`` for the legacy
            (community, no-org) caller.
        roster: Precomputed roster, so a caller resolving several turns (or a
            test) is not forced to re-run the roster query each time.

    Returns:
        A :class:`SpeakerMentionResolution`. Every list is capped at
        :data:`MAX_RESOLUTION_ITEMS`.
    """
    if roster is None:
        roster = build_roster(db, user_id, organization_id=organization_id)
    if roster.declined:
        return SpeakerMentionResolution(declined=True)
    if not roster.entries:
        return SpeakerMentionResolution()

    candidates = extract_candidates(question) + extract_script_aware_candidates(question)
    if not candidates:
        return SpeakerMentionResolution()

    matched: list[str] = []
    ambiguous: list[str] = []
    rejected: list[tuple[str, str]] = []
    seen: set[str] = set()

    for candidate in candidates:
        norm = _normalize(candidate)
        if not norm or norm in seen:
            continue
        seen.add(norm)

        outcome = match_candidate(candidate, roster)
        if outcome.matched is not None:
            if outcome.matched not in matched:
                matched.append(outcome.matched)
        elif outcome.ambiguous_with:
            if candidate not in ambiguous:
                ambiguous.append(candidate)
        elif len(rejected) < MAX_RESOLUTION_ITEMS:
            rejected.append((candidate, outcome.reason))

    matched = matched[:MAX_RESOLUTION_ITEMS]
    ambiguous = ambiguous[:MAX_RESOLUTION_ITEMS]

    speaker_focus = bool(matched) and has_speaker_verb_frame(question)

    return SpeakerMentionResolution(
        matched=tuple(matched),
        ambiguous=tuple(ambiguous),
        rejected=tuple(rejected),
        speaker_focus=speaker_focus,
    )
