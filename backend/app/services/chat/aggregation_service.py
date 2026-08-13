"""Running the four aggregation shapes (#403 Stage 4, Phase 5).

The pure half — subject extraction, shape choice, filter construction — lives in
:mod:`app.services.chat.aggregation` and is testable with no services. This is
the half that talks to OpenSearch and Postgres.

Worst case for any question, at any scope: **one** ``size: 0`` search plus at
most **one** Postgres statement. The scope itself is capped at
:data:`app.core.constants.CHAT_MAX_SCOPE_FILES` upstream, and that cap is reused
here rather than a second one being invented.

Every shape **declines** (returns ``None``) rather than guessing. A number from
the wrong mechanism is indistinguishable from a number from the right one, so
the turn falling back to ranked excerpts is strictly better than a confident
wrong count.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.services.chat.aggregation import MAX_BUCKETS
from app.services.chat.aggregation import SHAPE_COUNT_EVENTS
from app.services.chat.aggregation import SHAPE_SPEAKER_FACET
from app.services.chat.aggregation import AggregationResult
from app.services.chat.aggregation import base_filters
from app.services.chat.aggregation import buckets
from app.services.chat.aggregation import choose_shape
from app.services.chat.aggregation import extract_subject
from app.services.chat.aggregation import subject_clause
from app.services.chat.router import Route
from app.services.chat.router import TemporalHint

logger = logging.getLogger(__name__)

#: Fields the "mentions X" predicate reads. ``content.exact`` is the standard
#: analyzer — no stemming, no shingles — which is what makes a phrase match mean
#: the phrase. ``content`` itself is the shingled/stemmed transcript analyzer and
#: would match paraphrases, which is right for ranking and wrong for counting.
_MENTION_FIELDS = ("content.exact",)

#: The speaker facet scopes by the recording's title, which is app metadata the
#: indexer writes, not something a participant said.
_TITLE_FIELDS = ("title",)


def _temporal_range(hint: TemporalHint | None) -> dict[str, Any] | None:
    """A ``range`` on ``upload_time`` for an absolute month or year.

    ⚠️ **``upload_time`` is the only date this application records.** There is no
    "recorded at" column on ``media_file`` (only ``upload_time`` and
    ``completed_at``), so "meetings in March 2025" means *uploaded* in March
    2025. For files ingested long after they were recorded — a back-catalogue
    import, or the eval corpus injector — that is the wrong date, and the answer
    will be confidently wrong rather than absent. Filed rather than papered
    over; a real recorded-date column is the fix, not a heuristic here.

    Relative hints ("most recent", "last quarter") return ``None``: they need a
    reference clock and a definition of "recent" that nobody has agreed, and
    guessing one produces a filter the user cannot see.
    """
    if hint is None or hint.year is None:
        return None
    if hint.month is None:
        start, end = f"{hint.year:04d}-01-01", f"{hint.year + 1:04d}-01-01"
    else:
        year, month = hint.year, hint.month
        next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
        start = f"{year:04d}-{month:02d}-01"
        end = f"{next_year:04d}-{next_month:02d}-01"
    return {"range": {"upload_time": {"gte": start, "lt": end}}}


def _search(client, index: str, body: dict[str, Any]) -> dict[str, Any]:
    """One aggregation search. **No ``search_pipeline``, ever.**

    OpenSearch 3.4 throws ``ArrayIndexOutOfBoundsException`` inside
    ``score-ranker-processor`` when an aggregation meets hybrid + collapse + RRF,
    so the pipeline is not merely unnecessary here — attaching it crashes the
    query. ``params`` is omitted entirely rather than passed empty.
    """
    # Annotated rather than returned directly: the OpenSearch client is untyped,
    # so returning its result straight out of a dict-declared function is Any.
    response: dict[str, Any] = client.search(index=index, body=body)
    return response


def _files_matching(
    client,
    index: str,
    filters: list[dict[str, Any]],
    clause: dict[str, Any],
) -> list[tuple[str, str]]:
    """``(file_uuid, title)`` for every match, sorted by uuid. **One** search.

    The title rides along as a nested ``terms`` bucket rather than being fetched
    afterwards, because "which meetings mention X" is useless as a list of uuids
    and a second query per answer would break the one-search worst case. A file
    whose documents carry no title yields ``""`` — never a fabricated name.
    """
    body = {
        "size": 0,
        "track_total_hits": False,
        "query": {"bool": {"filter": [*filters, clause]}},
        "aggs": {
            "files": {
                "terms": {"field": "file_uuid", "size": MAX_BUCKETS, "order": {"_key": "asc"}},
                "aggs": {
                    "title": {"terms": {"field": "title.keyword", "size": 1}},
                },
            }
        },
    }
    response = _search(client, index, body)
    found: list[tuple[str, str]] = []
    for bucket in buckets(response, "files"):
        titles = (bucket.get("title") or {}).get("buckets") or []
        found.append((str(bucket["key"]), str(titles[0]["key"]) if titles else ""))
    return sorted(found)


def _speaker_tally(
    client,
    index: str,
    filters: list[dict[str, Any]],
    clause: dict[str, Any],
) -> list[tuple[str, int]]:
    """``(speaker, distinct files)`` descending, ties broken lexicographically.

    Counted over **distinct files**, not documents: "who attended the most
    sessions" is a question about sessions, and a speaker who talked twice as
    much in one meeting did not attend two.
    """
    body = {
        "size": 0,
        "track_total_hits": False,
        "query": {"bool": {"filter": [*filters, clause]}},
        "aggs": {
            "people": {
                "terms": {"field": "speakers", "size": MAX_BUCKETS, "order": {"_key": "asc"}},
                "aggs": {
                    "files": {
                        "terms": {
                            "field": "file_uuid",
                            "size": MAX_BUCKETS,
                            "order": {"_key": "asc"},
                        }
                    }
                },
            }
        },
    }
    response = _search(client, index, body)
    tally = [
        (str(bucket["key"]), len(buckets(bucket, "files")))
        for bucket in buckets(response, "people")
    ]
    tally.sort(key=lambda item: (-item[1], item[0]))
    return tally


def _occurrence_count(db, subject: str, user_id: int, file_uuids: list[str] | None) -> int | None:
    """Total occurrences of ``subject`` across the user's transcript segments.

    Postgres, not OpenSearch, and not by preference: chunking overlaps a long
    turn's tail into the next chunk, so counting *occurrences* over chunk
    documents double-counts every overlap. Segments are the unsplit turns.

    Access is enforced by the same accessible-files subquery the rest of the app
    uses — never by trusting the denormalised ids on an index document.
    """
    if db is None or not subject:
        # No session (a no-Postgres caller) or no phrase to count: decline. The
        # alternative — counting over chunk documents instead — double-counts
        # every chunk overlap, which is the reason this shape is in Postgres.
        return None
    from sqlalchemy import func
    from sqlalchemy import select

    from app.models.media import MediaFile
    from app.models.media import TranscriptSegment
    from app.services.permission_service import PermissionService

    accessible = PermissionService.get_accessible_file_ids_subquery(db, user_id)
    # Composed with SQLAlchemy constructs, not string interpolation: the subject
    # is user text on its way into a regular expression, and the earlier draft
    # built the statement by compiling the permission subquery with
    # `literal_binds` and formatting it into an f-string. That worked and was a
    # SQL-injection shape one refactor away from being real.
    statement = (
        select(
            func.coalesce(
                func.sum(func.regexp_count(TranscriptSegment.text, _posix_escape(subject), 1, "i")),
                0,
            )
        )
        .select_from(TranscriptSegment)
        .join(MediaFile, MediaFile.id == TranscriptSegment.media_file_id)
        # `select(...)` explicitly: the helper returns a Subquery, and passing one
        # straight to IN() makes SQLAlchemy coerce it with a warning.
        .where(MediaFile.id.in_(select(accessible.c[0])))
        .where(TranscriptSegment.text.ilike(f"%{_like_escape(subject)}%", escape="\\"))
    )
    if file_uuids is not None:
        statement = statement.where(MediaFile.uuid.in_(list(file_uuids)))
    return int(db.execute(statement).scalar() or 0)


def _posix_escape(value: str) -> str:
    """Escape a literal for a POSIX ERE, which is not Python's regex dialect."""
    return re.sub(r"([\\.^$*+?()\[\]{}|])", r"\\\1", value)


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def answer_aggregation(
    question: str,
    route: Route,
    *,
    db,
    client,
    index: str,
    user_id: int,
    organization_id: int | None = None,
    file_uuids: list[str] | None = None,
) -> AggregationResult | None:
    """Answer one aggregation question exactly, or decline.

    Args:
        question: The user's question, as typed.
        route: The router's decision. Its ``signals`` choose the shape and its
            ``temporal`` hint becomes the date filter.
        db: Session, for the shapes that count in Postgres.
        client: OpenSearch client. ``None`` declines.
        index: Chunk index name.
        user_id: Caller — enforced by ``accessible_user_ids`` and, on the
            Postgres path, by the accessible-files subquery.
        organization_id: Active tenant, or None for personal scope.
        file_uuids: Resolved scope. ``None`` = every accessible file; an empty
            list = match nothing.

    Returns:
        An :class:`AggregationResult`, or ``None`` when no shape applies, the
        subject could not be recovered, or the mechanism failed. Declining is
        always safe: the turn answers from the chunk leg.
    """
    shape = choose_shape(route)
    if shape is None or client is None:
        return None

    subject = extract_subject(question, route.temporal)
    filters = base_filters(user_id=user_id, organization_id=organization_id, file_uuids=file_uuids)
    date_clause = _temporal_range(route.temporal)
    if date_clause is not None:
        filters.append(date_clause)

    coverage: dict[str, Any] = {
        "scope_files": "all accessible" if file_uuids is None else len(file_uuids),
        "subject_source": "phrase" if subject else "content words",
        "date_filter": "upload_time" if date_clause is not None else None,
    }
    if route.temporal is not None and date_clause is None and not route.temporal.is_empty:
        # Said out loud: the user asked for a period and did not get one.
        coverage["date_filter_skipped"] = route.temporal.relative or "unresolvable"

    try:
        if shape == SHAPE_SPEAKER_FACET:
            return _run_speaker_facet(client, index, filters, question, subject, coverage)
        if shape == SHAPE_COUNT_EVENTS:
            total = _occurrence_count(db, subject, user_id, file_uuids)
            if total is None:
                return None
            coverage["counted_over"] = "transcript_segment (chunk overlap would double-count)"
            return AggregationResult(
                shape=shape,
                mechanism="postgres: regexp_count over transcript_segment.text",
                subject=subject,
                count=total,
                coverage=coverage,
            )

        clause = subject_clause(subject, question, _MENTION_FIELDS)
        if clause is None:
            return None
        matched = _files_matching(client, index, filters, clause)
        return AggregationResult(
            shape=shape,
            mechanism="opensearch: size:0 phrase filter + terms(file_uuid) + terms(title)",
            subject=subject,
            count=len(matched),
            file_uuids=tuple(uuid for uuid, _title in matched),
            file_titles=tuple(title for _uuid, title in matched),
            coverage=coverage,
        )
    except Exception:  # noqa: BLE001 — a failed count must not break the turn
        logger.exception("Aggregation shape %s failed; falling back to ranked excerpts", shape)
        return None


def _run_speaker_facet(
    client,
    index: str,
    filters: list[dict[str, Any]],
    question: str,
    subject: str,
    coverage: dict[str, Any],
) -> AggregationResult | None:
    """Who, across the matching recordings — scoped by title, tallied by speaker."""
    clause = subject_clause(subject, question, _TITLE_FIELDS)
    if clause is None:
        return None
    tally = _speaker_tally(client, index, filters, clause)
    if not tally:
        return None
    top_name, top_sessions = tally[0]
    tied = sum(1 for _name, sessions in tally if sessions == top_sessions)
    coverage["scoped_by"] = "recording title (app metadata, not spoken content)"
    coverage["speakers_found"] = len(tally)
    if tied > 1:
        # A tie means there is no "the most". Reporting one name would be a
        # coin flip presented as a fact.
        coverage["tied_at_top"] = tied
    return AggregationResult(
        shape=SHAPE_SPEAKER_FACET,
        mechanism="opensearch: title filter + terms(speakers) x terms(file_uuid)",
        subject=subject,
        count=len(tally),
        speaker=None if tied > 1 else top_name,
        speaker_sessions=None if tied > 1 else top_sessions,
        rows=tuple(tally),
        coverage=coverage,
    )
