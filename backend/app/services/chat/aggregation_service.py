"""Running the four aggregation shapes (#403 Stage 4, Phase 5).

The pure half — subject extraction, shape choice, filter construction — lives in
:mod:`app.services.chat.aggregation` and is testable with no services. This is
the half that talks to OpenSearch and Postgres.

Worst case for any question, at any scope: **one** ``size: 0`` search plus at
most **three** Postgres statements — one to resolve a date filter to file uuids,
one to count the in-scope files that have no recorded date, and one for the
occurrence count. It was one until the date filter moved off the index; see
:func:`_files_in_period` for why that move was correctness and not preference.
The scope itself is capped at
:data:`app.core.constants.CHAT_MAX_SCOPE_FILES` upstream, and that cap is reused
here rather than a second one being invented.

**Those statements each get their own short session**, which is why this module
takes a ``session_factory`` and not a ``Session``. Holding the caller's
transaction open across the ``size: 0`` search would put a chat turn — a
high-concurrency path — ``idle in transaction`` for the length of an OpenSearch
round trip, queueing every ``ALTER TABLE`` behind it. See
``scripts/audit-session-lifetime.py``.

Every shape **declines** (returns ``None``) rather than guessing. A number from
the wrong mechanism is indistinguishable from a number from the right one, so
the turn falling back to ranked excerpts is strictly better than a confident
wrong count.
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Callable
from collections.abc import Iterator
from typing import Any

from app.core.enums import RecordedDateSource
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


#: A callable returning a session context manager — ``db.session_utils.session_scope``
#: in production, the test's own session in the suites. ``None`` means "this caller has
#: no Postgres", which every Postgres-backed shape treats as a decline.
SessionFactory = Callable[[], contextlib.AbstractContextManager[Any]]


@contextlib.contextmanager
def _short_session(session_factory: SessionFactory | None) -> Iterator[Any]:
    """A session for one group of statements, closed before anything slow runs.

    Yields ``None`` when there is no factory, so the Postgres-backed shapes keep
    their existing "no session → decline" branch rather than growing a second
    way to say the same thing.
    """
    if session_factory is None:
        yield None
        return
    with session_factory() as db:
        yield db


def _temporal_bounds(hint: TemporalHint | None) -> tuple[str, str] | None:
    """``(start, end)`` ISO dates for an absolute month or year, half-open.

    Relative hints ("most recent", "last quarter") return ``None``: they need a
    reference clock and a definition of "recent" that nobody has agreed, and
    guessing one produces a filter the user cannot see.
    """
    if hint is None or hint.year is None:
        return None
    if hint.month is None:
        return f"{hint.year:04d}-01-01", f"{hint.year + 1:04d}-01-01"
    year, month = hint.year, hint.month
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return f"{year:04d}-{month:02d}-01", f"{next_year:04d}-{next_month:02d}-01"


def _files_in_period(
    db,
    bounds: tuple[str, str],
    user_id: int,
    organization_id: int | None,
    file_uuids: list[str] | None,
) -> tuple[list[str], dict[str, int], int] | None:
    """Accessible files whose **recorded date** falls in the period, resolved in Postgres.

    ⚠️ **Not an OpenSearch ``range``, and the reason is correctness rather than
    cost.** Two of them:

    1. The index lags the truth. A user correcting a wrong date is the whole
       point of ``recorded_date_locked``, and an index-side filter would keep
       answering with the old date until the next reindex — the correction would
       silently not apply, which is worse than not offering one.
    2. It is the package's existing rule. Scope resolves relationally
       (``context_resolver``) precisely so a stale document cannot reach a
       prompt; a date is scope.

    The result narrows the caller's ``file_uuids``, so it composes with an
    explicit scope instead of replacing it.

    **Costs two statements when a date filter applies**, not one, and the second
    is not optional: it counts the in-scope files that have *no* recorded date.
    Filtering on ``recorded_date`` silently excludes every undated file, so on a
    library the resolver has not reached the honest answer is "3, and 40 more I
    could not date" — never a bare 3. Reporting the count is what keeps this
    change from replacing one silent wrong answer with another.

    Returns:
        ``(uuids, source_tally, undated)``, or ``None`` to **decline** — no
        session, or more matches than
        :data:`~app.core.constants.CHAT_MAX_SCOPE_FILES`. Declining past the cap
        is the same rule every other shape here follows: a truncated answer is a
        wrong answer that looks like a right one, and the cap is the one the
        scope already uses rather than a second invented number.
    """
    if db is None:
        return None
    from sqlalchemy import func
    from sqlalchemy import select

    from app.core.constants import CHAT_MAX_SCOPE_FILES
    from app.models.media import MediaFile
    from app.services.permission_service import PermissionService

    start, end = bounds
    accessible = PermissionService.get_accessible_file_ids_subquery(db, user_id)

    def _scoped(statement):
        statement = statement.where(MediaFile.id.in_(select(accessible.c[0])))
        if organization_id is not None:
            statement = statement.where(MediaFile.organization_id == organization_id)
        if file_uuids is not None:
            statement = statement.where(MediaFile.uuid.in_(list(file_uuids)))
        return statement

    matched = _scoped(
        select(MediaFile.uuid, MediaFile.recorded_date_source)
        .where(MediaFile.recorded_date.is_not(None))
        .where(MediaFile.recorded_date >= start)
        .where(MediaFile.recorded_date < end)
    )
    # One over the cap, so "too many" is distinguishable from "exactly the cap"
    # without counting the whole table.
    rows = db.execute(matched.limit(CHAT_MAX_SCOPE_FILES + 1)).all()
    if len(rows) > CHAT_MAX_SCOPE_FILES:
        logger.info(
            "Declining a date-filtered aggregation: %s..%s matches more than %d files",
            start,
            end,
            CHAT_MAX_SCOPE_FILES,
        )
        return None

    undated = int(
        db.execute(
            _scoped(select(func.count()).select_from(MediaFile)).where(
                MediaFile.recorded_date.is_(None)
            )
        ).scalar()
        or 0
    )
    tally: dict[str, int] = {}
    for _uuid, source in rows:
        key = str(source or RecordedDateSource.NONE.value)
        tally[key] = tally.get(key, 0) + 1
    return [str(uuid) for uuid, _source in rows], tally, undated


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
    session_factory: SessionFactory | None,
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
        session_factory: Opens a session for the shapes that count in Postgres.
            A **factory**, not a session: each group of statements gets its own
            short transaction, so the ``size: 0`` search below never runs with
            one open. ``None`` = no Postgres, and those shapes decline.
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
    coverage: dict[str, Any] = {
        "scope_files": "all accessible" if file_uuids is None else len(file_uuids),
        "subject_source": "phrase" if subject else "content words",
        "date_filter": None,
    }

    # The date filter NARROWS THE SCOPE rather than adding an index clause, so it
    # applies identically to every shape — including the Postgres occurrence count,
    # which used to ignore it while ``coverage`` reported it applied. Base rule 10
    # tells the model to report a counted block exactly, so that over-claim became a
    # confident wrong sentence in the answer rather than a stray dict key.
    bounds = _temporal_bounds(route.temporal)
    if bounds is not None:
        # Its own transaction, closed here — the searches below must not inherit it.
        with _short_session(session_factory) as db:
            in_period = _files_in_period(db, bounds, user_id, organization_id, file_uuids)
        if in_period is None:
            # Declined — too many matches, or no session. Answering without the
            # filter the user asked for would be a different question's answer.
            return None
        file_uuids, date_sources, undated = in_period
        coverage["date_filter"] = "recorded_date"
        coverage["date_filter_period"] = f"{bounds[0]}..{bounds[1]}"
        # WHICH source dated each file. "3 meetings in March — dates from filenames"
        # is a checkable claim; a bare 3 is not, and a derived date the user cannot
        # trace is worse than no date at all.
        coverage["date_sources"] = date_sources
        if undated:
            # The honesty property the old ``upload_time`` disclosure carried, kept.
            # Filtering on a recorded date silently drops every file that has none, so
            # the count is a floor until this number is zero — and on a library the
            # resolver has not swept, it is most of them.
            coverage["undated_files_excluded"] = undated
    elif route.temporal is not None and not route.temporal.is_empty:
        # Said out loud: the user asked for a period and did not get one.
        coverage["date_filter_skipped"] = route.temporal.relative or "unresolvable"

    filters = base_filters(user_id=user_id, organization_id=organization_id, file_uuids=file_uuids)

    try:
        if shape == SHAPE_SPEAKER_FACET:
            return _run_speaker_facet(client, index, filters, question, subject, coverage)
        if shape == SHAPE_COUNT_EVENTS:
            with _short_session(session_factory) as db:
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
