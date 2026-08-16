"""Resolve and persist ``media_file.recorded_date``. Postgres in, Postgres out.

The I/O half of :mod:`.recorded_date`. Like the rest of this package it touches **no LLM,
no model and no OpenSearch**, and it **flushes, never commits** — the caller owns the
transaction so a reindex batch can do many files at once.

Cheap enough to run unconditionally: three regex passes over a filename and at most
:data:`~.date_sources.OPENING_SEGMENTS` turns. It deliberately has **no
``source_fingerprint`` short-circuit** of its own, unlike ``generate_file_artifacts``.
That is what makes it a backfill: the filename and the transcript are already in the
database for every existing row, so the first reindex after this ships resolves the whole
back-catalogue with no re-ingest and no media re-read.

⚠️ **Nothing here may read ``metadata_important['rag_eval']``.** That block is the
evaluation harness's gold source; a product path reading it would score the corpus by
consulting the answer key, and the resulting number would measure nothing. The eval
corpus's dates reach this column the same way a real file's do — written at ingest by the
injector, as ``container``.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.core.enums import RecordedDateSource
from app.models.media import MediaFile
from app.models.media import TranscriptSegment

from .date_sources import OPENING_SEGMENTS
from .date_sources import from_container
from .date_sources import from_filename
from .date_sources import from_transcript
from .recorded_date import Resolution
from .recorded_date import resolve

logger = logging.getLogger(__name__)


def _opening_segments(db: Session, file_id: int) -> list[dict[str, Any]]:
    """Just the opening turns' text, in the package's total order.

    Two columns and a ``LIMIT``, not ``load_ordered_segments``: the transcript source reads
    only ``text`` from the first few turns, and pulling whole ORM segment rows for a
    three-hour recording to look at twelve of them is the shape that has wedged this
    database before (`app/tasks/CLAUDE.md`, the session-lifetime rule).

    The order is ``(start_time, end_time, id)`` — the same total order the rest of the
    package uses. ``start_time`` alone is not one, and "the opening turns" is a statement
    about adjacency, so a partial order would make the answer depend on physical row order.
    """
    rows = (
        db.query(TranscriptSegment.text)
        .filter(TranscriptSegment.media_file_id == file_id)
        .order_by(
            TranscriptSegment.start_time,
            TranscriptSegment.end_time,
            TranscriptSegment.id,
        )
        .limit(OPENING_SEGMENTS)
        .all()
    )
    return [{"text": row[0]} for row in rows]


def resolve_for_file(db: Session, file_id: int, *, force: bool = False) -> Resolution | None:
    """Resolve one file's recorded date and write it, provenance and all.

    A **locked** row is left exactly as it is and its stored state is returned unchanged.
    That is requirement (c) of this change made real: a user's correction outranks every
    derived source *permanently*, not until the next reindex happens to run. ``force``
    overrides the lock and exists only for an explicit "re-derive this" action — no
    automatic path passes it.

    Args:
        db: Open session. Flushed, never committed.
        file_id: ``media_file.id``.
        force: Re-derive even when the user has locked the value.

    Returns:
        The :class:`~.recorded_date.Resolution` written, or ``None`` when the file is gone.
    """
    media_file = db.query(MediaFile).filter(MediaFile.id == file_id).one_or_none()
    if media_file is None:
        logger.debug("recorded_date: file %s no longer exists", file_id)
        return None

    if media_file.recorded_date_locked and not force:
        return _stored_resolution(media_file)

    resolution = resolve(
        [
            from_container(media_file.creation_date),
            from_filename(media_file.filename),
            from_transcript(_opening_segments(db, file_id)),
        ]
    )
    apply_resolution(media_file, resolution)
    db.flush()
    if resolution.conflict:
        # Logged at info, not warning: disagreement is normal and expected, and a warning
        # per file would train operators to ignore the channel. The user-facing surfacing
        # is the point — this line only makes it greppable.
        logger.info(
            "recorded_date: file %s has %d disagreeing sources; resolved to %s via %s",
            file_id,
            len(resolution.candidates),
            resolution.date,
            resolution.source.value,
        )
    return resolution


def apply_resolution(media_file: MediaFile, resolution: Resolution) -> None:
    """Write a resolution onto a row. Separate so a caller with the row can reuse it.

    ``recorded_date_locked`` is **not** touched. Locking is a user action; a derivation
    setting it would make the next derivation refuse to run, and the CHECK
    ``ck_media_file_recorded_date_locked_is_manual`` would reject the row outright the
    moment a non-``manual`` source won.
    """
    media_file.recorded_date = resolution.date
    media_file.recorded_date_source = resolution.source.value
    media_file.recorded_date_confidence = resolution.confidence
    media_file.recorded_date_candidates = resolution.candidates_json()


def set_manual_date(media_file: MediaFile, when: Any, *, evidence: str = "set by the user") -> None:
    """Record a human's own value, and lock it against every later derivation.

    Passing ``None`` clears the correction and returns the row to "not yet resolved", so
    the next derivation runs normally — a user who set a date by mistake must be able to
    take it back, and leaving it locked-at-NULL would silently disable the resolver for
    that file forever.
    """
    from .recorded_date import DateCandidate

    if when is None:
        media_file.recorded_date = None
        media_file.recorded_date_source = None
        media_file.recorded_date_confidence = None
        media_file.recorded_date_candidates = None
        media_file.recorded_date_locked = False
        return
    media_file.recorded_date = when
    media_file.recorded_date_source = RecordedDateSource.MANUAL.value
    # 1.0 without apology: the other confidences estimate how often a *source* is right
    # about a recording's date. A person telling us the date of their own recording is not
    # an estimate of anything, and shading it below 1.0 would invent a doubt we do not have.
    media_file.recorded_date_confidence = 1.0
    media_file.recorded_date_candidates = [
        DateCandidate(
            source=RecordedDateSource.MANUAL,
            date=when,
            confidence=1.0,
            evidence=evidence,
        ).as_dict()
    ]
    media_file.recorded_date_locked = True


def provenance_from_columns(
    *,
    source: str | None,
    confidence: float | None,
    locked: bool | None,
    candidates: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """The wire shape of a recorded-date provenance, or ``None``.

    Takes the four columns rather than a row so the serialisation layer can call it
    without importing a model, and so it is testable without a database.

    ``None`` means the resolver has never run on this row — genuinely unknown, and
    distinct from ``source='none'``, which means every source was consulted and none
    answered. Collapsing the two would make an un-swept library indistinguishable from a
    library of undatable recordings, and only one of those is fixable.

    Shaped as :class:`~app.schemas.media.DerivedFieldProvenance`, which is deliberately
    field-agnostic: participants, topics and titles are the same problem and reuse it.
    """
    if not source:
        return None
    stored = list(candidates or [])
    return {
        "source": source,
        "confidence": confidence,
        "locked": bool(locked),
        # Recomputed from the stored candidates rather than kept as its own column: a
        # conflict is a function of the candidates, and a second copy is a second thing
        # to keep in sync with them. Compared on the calendar DAY (the ISO prefix), the
        # same unit the resolver uses — comparing instants would flag nearly every file.
        "conflict": len({str(c.get("date"))[:10] for c in stored if c.get("date")}) > 1,
        "candidates": stored,
    }


def provenance_payload(media_file: MediaFile) -> dict[str, Any] | None:
    """:func:`provenance_from_columns` for a row. Convenience for model-side callers."""
    return provenance_from_columns(
        source=media_file.recorded_date_source,
        confidence=media_file.recorded_date_confidence,
        locked=media_file.recorded_date_locked,
        candidates=media_file.recorded_date_candidates,
    )


def _stored_resolution(media_file: MediaFile) -> Resolution:
    """Rebuild a :class:`Resolution` from the row, for the locked short-circuit."""
    from .recorded_date import DateCandidate

    stored = media_file.recorded_date_candidates or []
    recorded = media_file.recorded_date
    # A candidate cannot carry a date the row does not have. In practice a locked
    # row always has one — clearing the date unlocks, precisely so a lock at NULL
    # cannot disable the resolver for that file forever — but `recorded_date` is
    # nullable, and reconstructing a candidate around None would produce exactly
    # the bare date that ck_media_file_recorded_date_provenance exists to forbid,
    # one layer above the database.
    # An early return rather than a conditional expression wrapping the generator: a
    # generator is lazy, so narrowing `recorded` outside it does not carry into the body
    # and the guard reads as if it protects something it does not. Returning up front
    # makes the "locked but undated" case explicit — and it IS reachable, because the
    # database permits it: ck_media_file_recorded_date_locked_is_manual constrains the
    # source, not the date. The application never writes that state (clearing the date
    # unlocks, precisely so a lock at NULL cannot disable the resolver for a file
    # forever), but a hand-edited row can hold it, and the honest answer for such a row
    # is "manual, no date" rather than a candidate fabricated around None — which would
    # be the bare date ck_media_file_recorded_date_provenance exists to forbid,
    # reintroduced one layer above the database.
    source = RecordedDateSource(media_file.recorded_date_source or RecordedDateSource.NONE.value)
    if recorded is None:
        return Resolution(
            date=None,
            source=source,
            confidence=media_file.recorded_date_confidence,
            candidates=(),
            conflict=False,
        )

    candidates = tuple(
        DateCandidate(
            source=RecordedDateSource(entry["source"]),
            date=recorded,
            confidence=float(entry.get("confidence") or 0.0),
            evidence=str(entry.get("evidence") or ""),
        )
        for entry in stored
        if entry.get("source") == RecordedDateSource.MANUAL.value
    )
    return Resolution(
        date=recorded,
        source=source,
        confidence=media_file.recorded_date_confidence,
        candidates=candidates,
        conflict=False,
    )
