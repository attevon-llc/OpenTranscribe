"""Scoped I/O primitives for the aggregation shapes: sessions, access predicate, searches.

The tenant and quarantine chokepoints live here: :func:`_accessible_scoped_files` (the Postgres
predicate every Postgres-backed shape builds through) and :func:`_quarantined_among` (the
post-filter for the OpenSearch-backed shapes, whose index carries no quarantine field). Both are
shared by :mod:`app.services.chat.aggregation_service`,
:mod:`app.services.chat.aggregation_speakers` and :mod:`app.services.chat.recurrence_service`.
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Iterator
from typing import Any

from app.core.enums import RecordedDateSource
from app.services.chat.aggregation import MAX_BUCKETS
from app.services.chat.aggregation import buckets
from app.services.chat.router import TemporalHint
from app.services.takedown_predicates import quarantined_among

logger = logging.getLogger(__name__)

#: Fields the "mentions X" predicate reads. ``content.exact`` is the standard
#: analyzer — no stemming, no shingles — which is what makes a phrase match mean
#: the phrase. ``content`` itself is the shingled/stemmed transcript analyzer and
#: would match paraphrases, which is right for ranking and wrong for counting.
_MENTION_FIELDS = ("content.exact",)


#: The speaker facet scopes by the recording's title, which is app metadata the
#: indexer writes, not something a participant said. This is the original
#: default and stays the mechanism when ``chat.aggregate.speaker_facet_content_
#: scope`` is off — "who discussed X" then answers "who attended a recording
#: whose TITLE matches X", which is attendance, not participation.
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


def _flag_enabled(session_factory: SessionFactory | None, key: str, default: bool) -> bool:
    """One admin-tunable ``chat.aggregate.*`` boolean, read fresh per call.

    Its own short session (``_short_session``) rather than a Postgres statement
    the caller already has open — the module's phase discipline is "gather,
    close, then run the slow thing", and a flag read is exactly the kind of
    incidental lookup that turns into an ``idle in transaction`` hold if it
    rides along on someone else's session instead of opening (and closing) its
    own.

    Returns:
        ``default`` when there is no session, the row is unset, or the stored
        value is unparseable — never raises.
    """
    with _short_session(session_factory) as db:
        if db is None:
            return default
        from app.services.system_settings_service import get_setting_bool

        return get_setting_bool(db, key, default)


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


def _accessible_scoped_files(
    db,
    user_id: int,
    organization_id: int | None,
    file_uuids: list[str] | None,
):
    """Filter predicate every Postgres aggregation shape must build through.

    Wraps ``PermissionService.get_accessible_file_ids_subquery`` with
    ``organization_id`` threaded through explicitly, and folds in the caller's
    resolved-scope ``file_uuids`` (``None`` = every accessible file). Omitting
    ``organization_id`` from that call defaults it to the ``UNSCOPED``
    sentinel — no tenant gate at all — which is different from, and wider
    than, "personal scope": two shapes here (``_files_in_period`` and
    ``_occurrence_count``) used to call it bare, so a *personal*-scope
    aggregation counted org-stamped files that belong to a different tenant
    the user happens to also be a member of. Threading it through applies
    ``organization_id.is_(None)`` for personal scope, matching every other
    Postgres read in this package.

    Also excludes quarantined files, unconditionally — the Postgres-side
    counterpart of `_quarantined_among` below, which does the same job for the
    shapes that read OpenSearch instead. Chat has no admin bypass on quarantine
    anywhere else (`context_resolver._visible_files_query`,
    `service._drop_quarantined_hits`), so this predicate would otherwise be the
    one path in the package where a taken-down file's segments still counted —
    exactly the gap `_occurrence_count` shipped with.

    Returns:
        A SQLAlchemy boolean predicate for use in ``MediaFile``-rooted
        ``.where()`` clauses.
    """
    from sqlalchemy import select

    from app.models.media import MediaFile
    from app.services.permission_service import PermissionService

    accessible = PermissionService.get_accessible_file_ids_subquery(
        db, user_id, organization_id=organization_id
    )
    predicate = MediaFile.id.in_(select(accessible.c[0])) & MediaFile.is_quarantined.is_(False)
    if file_uuids is not None:
        predicate = predicate & MediaFile.uuid.in_(list(file_uuids))
    return predicate


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
        :data:`~app.core.chat_defaults.CHAT_MAX_SCOPE_FILES`. Declining past the cap
        is the same rule every other shape here follows: a truncated answer is a
        wrong answer that looks like a right one, and the cap is the one the
        scope already uses rather than a second invented number.
    """
    if db is None:
        return None
    from sqlalchemy import func
    from sqlalchemy import select

    from app.core.chat_defaults import CHAT_MAX_SCOPE_FILES
    from app.models.media import MediaFile

    start, end = bounds
    scoped_predicate = _accessible_scoped_files(db, user_id, organization_id, file_uuids)

    def _scoped(statement):
        return statement.where(scoped_predicate)

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


def _quarantined_among(
    session_factory: SessionFactory | None, file_uuids: Iterable[str]
) -> frozenset[str]:
    """Which of ``file_uuids`` belong to a quarantined file — the aggregation
    tier's post-filter, mirroring ``api/endpoints/search_helpers.py::
    _drop_quarantined_search_hits`` and ``service._drop_quarantined_hits``.

    Quarantine is not an OpenSearch filter field (see ``context_resolver``'s
    module docstring), so the three OpenSearch-backed shapes here
    (``count_files`` / ``list_files`` / ``speaker_facet``) cannot exclude a
    taken-down file at query time; they run the search first and this filters
    the bucket keys the response actually contained, in Postgres, in ONE query
    over exactly those ids (never the whole quarantined table).

    Declines to enforce — returns an empty set, not ``None`` — when there is no
    session. That is a narrower "no session" contract than the rest of this
    module's Postgres shapes (which decline the WHOLE answer): these three
    shapes have always been answerable with no Postgres access at all (they are
    OpenSearch-only), and every real caller (``service.py``'s
    ``_prepare_context``) always passes a working factory. Chat has no other
    path that reaches these shapes with ``session_factory=None``.
    """
    ids = {str(uuid) for uuid in file_uuids if uuid}
    if not ids:
        return frozenset()
    with _short_session(session_factory) as db:
        if db is None:
            return frozenset()
        return frozenset(quarantined_among(db, ids))


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

    Callers must post-filter the result through ``_quarantined_among`` — this
    function is index-only and the index carries no quarantine field at all.
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
) -> list[tuple[str, tuple[str, ...]]]:
    """``(speaker, distinct file uuids)`` per speaker — index-only, unfiltered.

    Returns the raw file-uuid set per speaker rather than a count so the caller
    can drop quarantined files (via ``_quarantined_among``) BEFORE counting —
    counting first and subtracting after would be right for the common case but
    wrong the moment a subtraction pushed two speakers into a tie the pre-filter
    count did not have.
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
    return [
        (str(bucket["key"]), tuple(str(fb["key"]) for fb in buckets(bucket, "files")))
        for bucket in buckets(response, "people")
    ]


def _occurrence_count(
    db,
    subject: str,
    user_id: int,
    organization_id: int | None,
    file_uuids: list[str] | None,
) -> int | None:
    """Total occurrences of ``subject`` across the user's transcript segments.

    Postgres, not OpenSearch, and not by preference: chunking overlaps a long
    turn's tail into the next chunk, so counting *occurrences* over chunk
    documents double-counts every overlap. Segments are the unsplit turns.

    Access is enforced by ``_accessible_scoped_files`` — the same
    accessible-files subquery, with the tenant gate threaded through, the rest
    of the app uses — never by trusting the denormalised ids on an index
    document.
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

    scoped_predicate = _accessible_scoped_files(db, user_id, organization_id, file_uuids)
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
        .where(scoped_predicate)
        .where(TranscriptSegment.text.ilike(f"%{_like_escape(subject)}%", escape="\\"))
    )
    return int(db.execute(statement).scalar() or 0)


def _posix_escape(value: str) -> str:
    """Escape a literal for a POSIX ERE, which is not Python's regex dialect."""
    return re.sub(r"([\\.^$*+?()\[\]{}|])", r"\\\1", value)


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
