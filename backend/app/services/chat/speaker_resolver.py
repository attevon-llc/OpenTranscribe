"""Deterministic, Postgres-only speaker-mention resolution (issue #403 family).

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

import re
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from sqlalchemy.orm import Session

from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.services.chat.speaker_mention_extract import extract_candidates
from app.services.chat.speaker_mention_extract import extract_script_aware_candidates
from app.services.chat.speaker_name_match import match_candidate
from app.services.chat.speaker_name_text import _normalize
from app.services.chat.speaker_roster import Roster
from app.services.chat.speaker_roster import SpeakerResolutionDeclineReason
from app.services.chat.speaker_roster import build_candidate_roster

#: How many matched / ambiguous / rejected entries `SpeakerMentionResolution`
#: carries. This is diagnostic payload persisted on every turn's
#: `msg_metadata.speaker_resolution` — unbounded growth there is the same
#: class of defect the digest/plan diagnostics elsewhere in this package
#: already cap.
MAX_RESOLUTION_ITEMS = 10


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
    decline_reason: SpeakerResolutionDeclineReason | None = None

    def as_meta(self) -> dict[str, Any]:
        """Shape for ``msg_metadata.speaker_resolution`` (size-capped already).

        ``reason`` (issue #524) is the short, non-identifying category behind
        an empty ``matched`` — never a speaker name — so a turn that resolved
        nothing is diagnosable from the persisted metadata alone instead of
        costing a source read to find out which of several causes it was.
        """
        payload: dict[str, Any] = {}
        if self.matched:
            payload["matched"] = list(self.matched)
        if self.ambiguous:
            payload["ambiguous"] = list(self.ambiguous)
        if self.declined:
            payload["declined"] = True
        if self.decline_reason is not None:
            payload["reason"] = self.decline_reason.value
        return payload


def resolve_speaker_mentions(
    db: Session,
    question: str,
    *,
    user_id: int,
    organization_id: OrgScope = UNSCOPED,
    file_uuids: list[str] | None = None,
    roster: Roster | None = None,
) -> SpeakerMentionResolution:
    """Resolve every name candidate in ``question`` against a roster scoped
    to those SAME candidates (issue #524).

    Candidates come from both extraction tracks — :func:`extract_candidates`
    (Latin capitalization) and :func:`extract_script_aware_candidates`
    (scriptio-continua runs plus Hangul/Arabic/Hebrew/Devanagari tokens) — so
    a candidate mixing, say, an English and a Chinese name is matched against
    equally regardless of which script the question itself is written in.
    Candidates are extracted BEFORE the roster is built (unless one is
    already supplied): a question naming nobody now costs zero Postgres
    reads, where the old shape built a roster first and looked at the
    question second.

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
        file_uuids: The turn's already-resolved scope, or ``None`` for "all
            accessible" — threaded into :func:`build_candidate_roster` so a
            scoped turn resolves a mention against ONLY the files already in
            scope (design direction #3), never the whole library. Ignored
            when ``roster`` is supplied directly.
        roster: Precomputed roster, so a caller resolving several turns (or a
            test) is not forced to re-run the roster query each time.

    Returns:
        A :class:`SpeakerMentionResolution`. Every list is capped at
        :data:`MAX_RESOLUTION_ITEMS`.
    """
    candidates = extract_candidates(question) + extract_script_aware_candidates(question)
    if not candidates:
        return SpeakerMentionResolution(decline_reason=SpeakerResolutionDeclineReason.NO_CANDIDATES)

    if roster is None:
        roster = build_candidate_roster(
            db,
            user_id,
            candidates,
            organization_id=organization_id,
            file_uuids=file_uuids,
        )
    if roster.declined:
        return SpeakerMentionResolution(declined=True, decline_reason=roster.decline_reason)
    if not roster.entries:
        return SpeakerMentionResolution(decline_reason=SpeakerResolutionDeclineReason.NO_MATCH)

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

    decline_reason = None
    if not matched:
        decline_reason = (
            SpeakerResolutionDeclineReason.AMBIGUOUS_TIE
            if ambiguous
            else SpeakerResolutionDeclineReason.NO_MATCH
        )

    return SpeakerMentionResolution(
        matched=tuple(matched),
        ambiguous=tuple(ambiguous),
        rejected=tuple(rejected),
        speaker_focus=speaker_focus,
        decline_reason=decline_reason,
    )
