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

**English-first.** Candidate extraction leans on Latin-script capitalization
conventions; a later, script-aware pass is tracked separately (see the RAG
multilingual notes in ``backend/app/services/chat/CLAUDE.md``).
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


def match_candidate(candidate: str, roster: Roster) -> _MatchOutcome:
    """Run one candidate through the ladder: exact -> token-subset -> fuzzy.

    Every rung requires a UNIQUE hit to resolve; two or more roster entries
    tying at any rung is ambiguity, not a pick between them — per the design
    constraint, ambiguity means no filter, ever, never a best-effort guess.

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

    scored: list[tuple[float, str]] = []
    for entry in roster.entries:
        ratio = difflib.SequenceMatcher(None, norm_candidate, _normalize(entry.name)).ratio()
        if ratio >= FUZZY_MATCH_THRESHOLD:
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
    """Resolve every capitalized-name candidate in ``question`` against the roster.

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

    candidates = extract_candidates(question)
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
