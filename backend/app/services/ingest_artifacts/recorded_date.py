"""Resolving "when was this recorded" from several disagreeing sources.

The pure half: candidate types, the precedence rule, and conflict detection. The half that
reads and writes Postgres is :mod:`.recorded_date_service`; the extractors that produce
candidates are :mod:`.date_sources`.

Three properties this module exists to guarantee, none of which is a detail:

**A date never travels without its source.** :class:`Resolution` cannot express one — the
source field has no ``None``, and "nothing answered" is
:attr:`~app.core.enums.RecordedDateSource.NONE` rather than an absent value. The database
enforces the same rule (``ck_media_file_recorded_date_provenance``), so a bare date is
unrepresentable at both ends rather than being a convention someone can forget.

**Losing candidates are kept, not discarded.** Sources legitimately disagree — a recording
made on the 14th about the 15th's meeting is an ordinary thing — and a disagreement
resolved silently by whichever branch ran first is the confident-wrong-answer shape this
epic keeps finding. Every observation is serialised into ``recorded_date_candidates`` so a
conflict can be shown to the user and corrected, rather than being a decision nobody can
see or audit.

**Conflict is not confidence, and they are not merged into one number.** Confidence answers
"how much does this *source* usually know"; conflict answers "do the sources agree". A
single blended score cannot be read back into either, so a high-confidence container date
contradicted by the filename would look identical to a mediocre uncontested one. They stay
separate fields all the way to the UI.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from app.core.enums import PRECEDENCE
from app.core.enums import RecordedDateSource


@dataclass(frozen=True)
class DateCandidate:
    """One source's observation. Frozen: a candidate is evidence, not working state."""

    source: RecordedDateSource
    date: dt.datetime
    #: Ordinal, not a measured probability — see :mod:`.date_sources`. Used to order forms
    #: within a source and shown as a hint; it never overrides :data:`PRECEDENCE`.
    confidence: float
    #: What the source actually said, in the source's own terms — the matched filename
    #: substring, the spoken phrase, the container field. This is what makes a wrong date
    #: *diagnosable* instead of merely wrong: the user can see why we believed it.
    evidence: str

    def as_dict(self) -> dict[str, Any]:
        """JSONB-safe. ``date`` is ISO-8601 so the column round-trips through any client."""
        return {
            "source": self.source.value,
            "date": self.date.isoformat(),
            "confidence": round(float(self.confidence), 4),
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class Resolution:
    """The answer, its origin, and everything that was considered and lost."""

    date: dt.datetime | None
    #: Never ``None``. ``NONE`` means every source was consulted and none answered, which
    #: is a different statement from "the resolver has not run" (the column is NULL).
    source: RecordedDateSource
    confidence: float | None
    #: All candidates, winner included, ordered by precedence. Serialisable.
    candidates: tuple[DateCandidate, ...]
    #: Two or more candidates name different calendar days.
    conflict: bool

    @property
    def is_resolved(self) -> bool:
        return self.date is not None

    def candidates_json(self) -> list[dict[str, Any]] | None:
        """``None`` rather than ``[]`` when nothing was found — an empty list in JSONB
        reads as "we looked and stored the empty result", which is true here, but the
        column is also NULL on unresolved rows and two spellings of the same state is one
        more thing for a reader to get wrong."""
        return [c.as_dict() for c in self.candidates] or None


def _rank(source: RecordedDateSource) -> int:
    """Position in :data:`PRECEDENCE`; unranked sources sort last rather than crashing.

    ``NONE`` is the only member outside ``PRECEDENCE`` today, and it never reaches here as
    a candidate — but a future source added to the enum and forgotten in ``PRECEDENCE``
    would otherwise raise inside the resolver, at ingest, on a user's file.
    ``test_precedence_ranks_every_derivable_source_exactly_once`` is what actually catches
    that; this is only so the failure is a bad ordering rather than an exception.
    """
    try:
        return PRECEDENCE.index(source)
    except ValueError:
        return len(PRECEDENCE)


def resolve(candidates: list[DateCandidate | None]) -> Resolution:
    """Pick a date by precedence, keep the rest, and say whether they disagreed.

    ``None`` entries are accepted and dropped so callers can pass the extractors' results
    positionally without a filtering dance at every call site.

    Ties within one source are broken by confidence and then by the ISO date, so the result
    is deterministic — two sources of the same rank with the same confidence must not
    resolve differently between runs, or two identical files ingested a month apart would
    disagree.

    Args:
        candidates: Observations, in any order, possibly containing ``None``.

    Returns:
        A :class:`Resolution`. ``source`` is ``NONE`` and ``date`` is ``None`` when every
        candidate was absent — the honest empty answer, not an error.
    """
    found = [c for c in candidates if c is not None]
    if not found:
        return Resolution(
            date=None,
            source=RecordedDateSource.NONE,
            confidence=None,
            candidates=(),
            conflict=False,
        )

    ordered = tuple(
        sorted(found, key=lambda c: (_rank(c.source), -c.confidence, c.date.isoformat()))
    )
    winner = ordered[0]
    # Compared on the calendar day, not the instant: a container stamp at 09:04 and a
    # filename saying the same date are the same answer, and flagging that as a conflict
    # would make the flag fire on almost every file and mean nothing.
    days = {c.date.date() for c in ordered}
    return Resolution(
        date=winner.date,
        source=winner.source,
        confidence=winner.confidence,
        candidates=ordered,
        conflict=len(days) > 1,
    )
