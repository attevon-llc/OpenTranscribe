"""The speaker-name matching ladder (no database, no model).

NFKC + casefold, then exact -> unique token-subset -> script-scoped prefix
rungs -> fuzzy, run against an already-built :class:`Roster`. Every rung
requires a UNIQUE hit; a tie is ambiguity, never a guess.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

from app.services.chat.speaker_name_text import _FUZZY_MAX_LENGTH_DELTA
from app.services.chat.speaker_name_text import _KOREAN_PARTICLE_MAX_CHARS
from app.services.chat.speaker_name_text import _entry_script
from app.services.chat.speaker_name_text import _fuzzy_threshold
from app.services.chat.speaker_name_text import _normalize
from app.services.chat.speaker_name_text import _tokens
from app.services.chat.speaker_roster import Roster


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
