"""Metadata filter predicates for the summary-search leg (issue #831 items 1-2).

``GET /api/search`` answers ONE request with up to two legs — the transcript leg
(OpenSearch, ``hybrid_search_service._build_filters``) and the summary leg
(Postgres, ``summary_search.search_summaries``). Every filter the caller sends
belongs to the *request*, not to a leg, and the SPA sends all of them on every
tab (``frontend/src/routes/search/+page.svelte`` builds one ``apiParams`` object
regardless of ``result_type``). A filter honoured by one leg and dropped by the
other therefore makes the two halves of a single response disagree: before this
module, a user on the Summaries tab got hits their date / tag / collection
filters should have excluded.

**These predicates mirror the TRANSCRIPT leg's semantics, not the gallery's**
(``api/endpoints/files/filtering.py``). The two planes genuinely differ in three
places, and reusing a gallery helper for any of them would reproduce the very
defect this module closes, in a new place:

===========  =============================  ====================================
filter       gallery (``filtering.py``)     search plane (this module)
===========  =============================  ====================================
tags         ALL of — ``HAVING COUNT(...)`` ANY of — the index filters with a
             ``== len(tag)``                ``terms`` clause, which is OR
file size    MB, multiplied to bytes here   BYTES already: ``/api/search``
                                            documents the params as bytes and
                                            the SPA sends ``MB * 1024 * 1024``
date range   ``media_file.upload_time``     ``COALESCE(creation_date,
                                            upload_time)`` — what
                                            ``search_indexing_task`` actually
                                            writes into the index's
                                            ``upload_time`` field
===========  =============================  ====================================

Nothing here imports ``api.endpoints.files.filtering`` for a second reason
besides the semantics: a service importing the API layer pulls the whole files
router in at import time. ``FILE_TYPE_MIME_PREFIXES`` **is** shared —
``core/constants`` has been the single home both planes read it from since
issue #871.

⚠️ Every predicate this module returns must be applied as a **pre-filter inside
the same query** that computes ``total`` and takes the page offset. That is
issue #818's rule: a filter applied after paging leaves ``total`` describing a
different set than the page does, which is how a taken-down file's existence
leaked through a count.
"""

from __future__ import annotations

import uuid as uuid_pkg
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy import ColumnElement
from sqlalchemy import select

from app.core.constants import FILE_TYPE_MIME_PREFIXES
from app.models.media import CollectionMember
from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import Tag

#: The language the indexer substitutes for a file that never got one
#: (``search_indexing_task._file_index_metadata``: ``media_file.language or "en"``).
#: A ``?language=en`` request therefore matches un-detected files on the
#: transcript leg, and must here too.
_INDEXED_LANGUAGE_DEFAULT = "en"


@dataclass(frozen=True)
class SummarySearchFilters:
    """The ``/api/search`` metadata filters, as the summary leg needs them.

    One object rather than thirteen keyword arguments on ``search_summaries``,
    which already carries paging, tenancy, redaction and the quarantine bypass.

    ``date_from`` / ``date_to`` are **parsed datetimes**, not the ISO strings the
    endpoint receives — use :func:`parse_date_bound` so the rounding rule below
    is applied exactly once, at the edge.
    """

    speakers: list[str] | None = None
    tags: list[str] | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    file_type: list[str] | None = None
    collection_id: int | None = None
    min_duration: float | None = None
    max_duration: float | None = None
    min_file_size: int | None = None
    max_file_size: int | None = None
    language: str | None = None
    title_filter: str | None = None
    file_uuid: str | None = None


def parse_date_bound(value: str | None, *, upper: bool) -> datetime | None:
    """Parse one ``date_from``/``date_to`` query value into a comparable datetime.

    ``/api/search`` takes these as **strings** and hands them straight to
    OpenSearch, so the transcript leg gets OpenSearch's date-range semantics for
    free. Postgres has no equivalent, so the two rules that matter are applied
    here instead:

    - **A date-only upper bound covers the whole day.** OpenSearch rounds a
      range bound to its granularity — ``gte``/``lt`` down, ``gt``/``lte`` up —
      so ``date_to=2026-01-15`` includes everything recorded that day on the
      transcript leg. Read as a bare midnight here it would have excluded all of
      it, and the two legs of one request would disagree about an entire day.
    - **A naive value is UTC.** The SPA's date inputs send ``YYYY-MM-DD`` with no
      offset; OpenSearch reads that as UTC. Both date columns are
      ``timestamptz``, so leaving the value naive would instead have it read in
      whatever the database session's ``TimeZone`` happens to be.

    Args:
        value: An ISO 8601 date or datetime, or None/empty for "no bound".
        upper: True for ``date_to`` (the rounding-up bound), False for
            ``date_from``.

    Returns:
        An aware datetime, or None when no bound was given.

    Raises:
        ValueError: The value is not ISO 8601. Callers turn this into a 400 —
            a bound that cannot be parsed must never be silently dropped, which
            would leave the caller believing they had filtered.
    """
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    date_only = len(value.strip()) == len("YYYY-MM-DD") and "T" not in value
    if upper and date_only:
        # The last microsecond of that day — Postgres timestamp resolution, so
        # this is exactly "the whole day" and stays a single ``<=`` comparison.
        parsed = parsed + timedelta(days=1) - timedelta(microseconds=1)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _title_expression() -> ColumnElement[str]:
    """What ``title_filter`` matches against, mirroring the indexed ``title``.

    ``search_indexing_task`` writes ``title or filename or "File {id}"``. The
    synthetic ``File {id}`` placeholder is deliberately NOT reproduced: it is an
    indexer artifact, not something a user named, and matching it would let a
    title filter of "file" select every untitled, unnamed row.
    """
    return sa.func.coalesce(
        sa.func.nullif(MediaFile.title, ""), sa.func.nullif(MediaFile.filename, ""), ""
    )


def _like_escape(value: str) -> str:
    """Escape LIKE metacharacters so user input is matched literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _related_row_predicates(filters: SummarySearchFilters) -> list[ColumnElement[bool]]:
    """Predicates that reach another table: identity, speakers, tags, collections.

    Split from the plain-column half below only so each stays legible — they are
    ANDed together and neither is meaningful on its own.
    """
    preds: list[ColumnElement[bool]] = []

    if filters.file_uuid:
        try:
            preds.append(MediaFile.uuid == uuid_pkg.UUID(filters.file_uuid))
        except ValueError:
            # The transcript leg sends an unparseable value to OpenSearch as a
            # term that simply matches nothing. Binding it to a UUID column
            # would raise instead, so match nothing explicitly — never fall
            # through unfiltered, which would widen the scope the caller asked
            # to narrow.
            preds.append(sa.literal(False))

    if filters.speakers:
        # Display name OR original diarization label, the same pair the gallery
        # matches: the index's ``speaker`` keyword holds whichever name was
        # resolved when the chunk was written, so matching both is the shape
        # that cannot miss a rename.
        preds.append(
            select(sa.literal(1))
            .select_from(Speaker)
            .where(
                Speaker.media_file_id == MediaFile.id,
                sa.or_(
                    Speaker.display_name.in_(filters.speakers),
                    Speaker.name.in_(filters.speakers),
                ),
            )
            .exists()
        )

    if filters.tags:
        # ANY of, not all of — see the table in the module docstring.
        preds.append(
            MediaFile.id.in_(
                select(FileTag.media_file_id)
                .join(Tag, Tag.id == FileTag.tag_id)
                .where(Tag.name.in_(filters.tags))
            )
        )

    if filters.collection_id is not None:
        # No permission check on the collection itself, deliberately: access
        # control is already the accessible-file subquery in the caller, so
        # naming somebody else's collection yields the (possibly empty)
        # intersection — exactly what the transcript leg's ``collection_ids``
        # term ANDed with ``accessible_user_ids`` produces.
        preds.append(
            MediaFile.id.in_(
                select(CollectionMember.media_file_id).where(
                    CollectionMember.collection_id == filters.collection_id
                )
            )
        )

    return preds


def _column_predicates(filters: SummarySearchFilters) -> list[ColumnElement[bool]]:
    """Predicates over ``media_file``'s own columns: type, language, title, ranges."""
    preds: list[ColumnElement[bool]] = []

    if filters.file_type:
        # Mirrors ``hybrid_search_service._file_type_filter_clause``: a coarse
        # bucket becomes a MIME-family prefix, anything else is compared
        # literally so a caller holding a full ``audio/mpeg`` still matches.
        # An unrecognized value narrows to nothing rather than being dropped
        # (issue #871), which is why there is no "if type_conditions" guard.
        type_conditions: list[ColumnElement[bool]] = []
        for value in filters.file_type:
            prefix = FILE_TYPE_MIME_PREFIXES.get(value.lower())
            if prefix:
                type_conditions.append(MediaFile.content_type.like(f"{prefix}%"))
            else:
                type_conditions.append(MediaFile.content_type == value)
        preds.append(sa.or_(*type_conditions))

    if filters.language:
        preds.append(
            sa.func.coalesce(sa.func.nullif(MediaFile.language, ""), _INDEXED_LANGUAGE_DEFAULT)
            == filters.language
        )

    if filters.title_filter:
        escaped = _like_escape(filters.title_filter)
        preds.append(_title_expression().ilike(f"%{escaped}%", escape="\\"))

    if filters.date_from is not None or filters.date_to is not None:
        recorded = sa.func.coalesce(MediaFile.creation_date, MediaFile.upload_time)
        if filters.date_from is not None:
            preds.append(recorded >= filters.date_from)
        if filters.date_to is not None:
            preds.append(recorded <= filters.date_to)

    if filters.min_duration is not None:
        preds.append(MediaFile.duration >= filters.min_duration)
    if filters.max_duration is not None:
        preds.append(MediaFile.duration <= filters.max_duration)

    # Bytes, not megabytes — see the table in the module docstring.
    if filters.min_file_size is not None:
        preds.append(MediaFile.file_size >= filters.min_file_size)
    if filters.max_file_size is not None:
        preds.append(MediaFile.file_size <= filters.max_file_size)

    return preds


def summary_filter_predicates(
    filters: SummarySearchFilters,
) -> list[ColumnElement[bool]]:
    """Translate ``filters`` into SQL predicates over ``media_file``.

    Args:
        filters: The request's metadata filters.

    Returns:
        Predicates to AND into the summary query — the SAME query that computes
        ``total`` and applies the page offset (see the module docstring).
    """
    return _related_row_predicates(filters) + _column_predicates(filters)
