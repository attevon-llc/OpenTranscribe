"""Running the five aggregation shapes (#403 Stage 4, Phase 5; W2.4 adds the fifth).

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
here rather than a second one being invented. Two exceptions to the "three
statements" ceiling, both W2.4: the speaker facet's content-scope choice and
``SHAPE_SPEAKER_STATS``'s enable check each cost one more short single-row
read (:func:`_flag_enabled`) when that shape is chosen — bounded, and paid
only by the two shapes that need it. ``SHAPE_SPEAKER_STATS`` itself issues
**zero** OpenSearch searches: it reads ``file_facts`` exactly like
``count_events`` reads ``transcript_segment``.

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
from collections.abc import Iterable
from collections.abc import Iterator
from typing import Any

from app.core import constants as C  # noqa: N812
from app.core.enums import RecordedDateSource
from app.services.chat.aggregation import MAX_BUCKETS
from app.services.chat.aggregation import SHAPE_COUNT_EVENTS
from app.services.chat.aggregation import SHAPE_SPEAKER_FACET
from app.services.chat.aggregation import SHAPE_SPEAKER_STATS
from app.services.chat.aggregation import AggregationResult
from app.services.chat.aggregation import base_filters
from app.services.chat.aggregation import buckets
from app.services.chat.aggregation import choose_shape
from app.services.chat.aggregation import extract_subject
from app.services.chat.aggregation import subject_clause
from app.services.chat.router import Route
from app.services.chat.router import TemporalHint
from app.utils.speaker_labels import UNKNOWN_SPEAKER_LABELS
from app.utils.speaker_labels import canonical_speaker_label

logger = logging.getLogger(__name__)

#: Fields the "mentions X" predicate reads. ``content.exact`` is the standard
#: analyzer — no stemming, no shingles — which is what makes a phrase match mean
#: the phrase. ``content`` itself is the shingled/stemmed transcript analyzer and
#: would match paraphrases, which is right for ranking and wrong for counting.
_MENTION_FIELDS = ("content.exact",)

#: The speaker facet scopes by the recording's title, which is app metadata the
#: indexer writes, not something a participant said. This is the pre-W2.4
#: default and stays the mechanism when ``chat.aggregate.speaker_facet_content_
#: scope`` is off — "who discussed X" then answers "who attended a recording
#: whose TITLE matches X", which is attendance, not participation.
_TITLE_FIELDS = ("title",)

#: W2.4 flag-gated setting keys. Read via :func:`_flag_enabled`, never through
#: :mod:`app.services.chat.settings` — that module resolves all thirteen (now
#: fifteen) RAG knobs in one query, and pulling it in here for two booleans
#: would couple a module that already budgets its own Postgres statement count
#: to every other admin knob's schema.
_SETTING_SPEAKER_FACET_CONTENT_SCOPE = "chat.aggregate.speaker_facet_content_scope"
_SETTING_SPEAKER_STATS_ENABLED = "chat.aggregate.speaker_stats_enabled"


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
    tier's post-filter, mirroring ``api/endpoints/search.py::
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
        from app.models.media import MediaFile

        rows = (
            db.query(MediaFile.uuid)
            .filter(MediaFile.uuid.in_(ids), MediaFile.is_quarantined.is_(True))
            .all()
        )
        return frozenset(str(row[0]) for row in rows)


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
        if shape == SHAPE_SPEAKER_STATS:
            if not _flag_enabled(
                session_factory,
                _SETTING_SPEAKER_STATS_ENABLED,
                C.DEFAULT_CHAT_AGGREGATE_SPEAKER_STATS_ENABLED,
            ):
                # Flag off: fall back to whatever `choose_shape` would have
                # returned with the `who-talked-most` signal removed — the
                # same shape this question would have hit before this feature
                # existed (see router.py's `_AGGREGATE_HEADS_STANDALONE`,
                # where "who talked the most" already satisfies the older
                # `who-most` pattern). This is what keeps the flag OFF path
                # byte-identical to the pre-W2.4 behaviour.
                fallback_signals = set(route.signals) - {"who-talked-most"}
                if fallback_signals & {"which-speakers", "who-most"}:
                    return _run_speaker_facet(
                        client, index, filters, question, subject, coverage, session_factory
                    )
                return None
            speaker_focus = route.speakers[0] if len(route.speakers) == 1 else None
            with _short_session(session_factory) as db:
                return _run_speaker_stats(
                    db, user_id, organization_id, file_uuids, speaker_focus, coverage
                )
        if shape == SHAPE_SPEAKER_FACET:
            return _run_speaker_facet(
                client, index, filters, question, subject, coverage, session_factory
            )
        if shape == SHAPE_COUNT_EVENTS:
            with _short_session(session_factory) as db:
                total = _occurrence_count(db, subject, user_id, organization_id, file_uuids)
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
        # Quarantine post-filter: the index carries no quarantine field, so a
        # taken-down file's uuid and TITLE would otherwise reach
        # `format_counted_block`'s `<counted>` block and, per base rule 10, be
        # reported to the user as fact.
        quarantined = _quarantined_among(session_factory, (uuid for uuid, _title in matched))
        if quarantined:
            matched = [(uuid, title) for uuid, title in matched if uuid not in quarantined]
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
    session_factory: SessionFactory | None,
) -> AggregationResult | None:
    """Who, across the matching recordings — tallied by speaker.

    Scoped by the recording's TITLE unless ``chat.aggregate.speaker_facet_
    content_scope`` is on, in which case it is scoped by what was actually
    SAID (``_MENTION_FIELDS`` over the chunk plane). The default (title) is
    attendance: "who discussed X" answers "who was in a recording whose title
    matched X". Content scope answers the question actually asked, at the cost
    of needing a Postgres read for the flag — still zero extra OpenSearch
    round trips, since it only changes which fields ``subject_clause`` reads.
    """
    content_scoped = _flag_enabled(
        session_factory,
        _SETTING_SPEAKER_FACET_CONTENT_SCOPE,
        C.DEFAULT_CHAT_AGGREGATE_SPEAKER_FACET_CONTENT_SCOPE,
    )
    fields = _MENTION_FIELDS if content_scoped else _TITLE_FIELDS
    clause = subject_clause(subject, question, fields)
    if clause is None:
        return None
    raw = _speaker_tally(client, index, filters, clause)
    if not raw:
        return None
    if content_scoped:
        # The title-scoped path has never excluded the unknown bucket (it is
        # not this change's job to alter that default's behaviour — the flag
        # OFF pin must stay byte-identical), but a content-scoped answer to
        # "who discussed X" must not name "Unknown Speaker" as a participant.
        raw = [(speaker, uuids) for speaker, uuids in raw if speaker not in UNKNOWN_SPEAKER_LABELS]
        if not raw:
            return None
    # Quarantine post-filter, BEFORE tallying: a taken-down recording's session
    # must not count toward "who attended the most sessions" for anyone, admin
    # included (chat has no admin bypass on quarantine anywhere else). Filtering
    # the counts after tallying would be right for the common case and wrong the
    # moment a subtraction created a tie the pre-filter numbers did not have.
    all_uuids = {uuid for _speaker, uuids in raw for uuid in uuids}
    quarantined = _quarantined_among(session_factory, all_uuids)
    tally = [
        (speaker, len(uuids) - sum(1 for uuid in uuids if uuid in quarantined))
        for speaker, uuids in raw
    ]
    tally = [(speaker, sessions) for speaker, sessions in tally if sessions > 0]
    if not tally:
        return None
    tally.sort(key=lambda item: (-item[1], item[0]))
    top_name, top_sessions = tally[0]
    tied = sum(1 for _name, sessions in tally if sessions == top_sessions)
    coverage["scoped_by"] = (
        "spoken content" if content_scoped else "recording title (app metadata, not spoken content)"
    )
    coverage["speakers_found"] = len(tally)
    if tied > 1:
        # A tie means there is no "the most". Reporting one name would be a
        # coin flip presented as a fact.
        coverage["tied_at_top"] = tied
    return AggregationResult(
        shape=SHAPE_SPEAKER_FACET,
        mechanism=(
            "opensearch: content phrase filter + terms(speakers) x terms(file_uuid)"
            if content_scoped
            else "opensearch: title filter + terms(speakers) x terms(file_uuid)"
        ),
        subject=subject,
        count=len(tally),
        speaker=None if tied > 1 else top_name,
        speaker_sessions=None if tied > 1 else top_sessions,
        rows=tuple(tally),
        coverage=coverage,
    )


def _run_speaker_stats(
    db,
    user_id: int,
    organization_id: int | None,
    file_uuids: list[str] | None,
    speaker_focus: str | None,
    coverage: dict[str, Any],
) -> AggregationResult | None:
    """ "Who talked the most" — exact per-speaker talk time from ``file_facts``.

    Postgres-only, like ``_occurrence_count``, and for the same class of
    reason: ``file_facts.facts['speakers']`` already holds an exact
    ``total_time`` per speaker per file, computed once at ingest
    (``services.ingest_artifacts.facts.build_facts``) — this is a read, not a
    search, and OpenSearch has no role here at all.

    Three refusals, each returned as a **disclosure** — a non-``None``
    :class:`AggregationResult` with no ``count``/``speaker`` but a ``coverage``
    note explaining why — rather than a silent decline. Base rule 10 tells the
    model "if a ``<counted>`` block reports a limitation, say so", so a
    disclosure reaches the user; a silent ``None`` would just fall back to
    ranked excerpts with no explanation at all, which is the wrong failure mode
    for "I can answer this, but not honestly for what you scoped":

    1. **Unbounded scope** (``file_uuids is None``, "every accessible file").
       Talk-time stats need a bounded set to sum over; summing "everything"
       would either be unboundedly expensive or silently truncate, and this
       tier refuses to guess which files a truncation would have picked.
    2. **Partial ``file_facts`` coverage.** Some files complete before
       ``file_facts`` existed (v390) or the periodic backfill has not reached
       them yet — see ``mapreduce.scope_digest_hits``'s identical outer-join
       gap, whose ``coverage["files_without_artifacts"]`` key name this
       mirrors on purpose. Answering from the files that DO have a row would
       present a partial tally as complete.
    3. **A focused speaker who never talked in scope.** Narrowing to one name
       and finding nothing is still worth telling the user, not just silence.

    Ties at the top are refused outright (``coverage["tied_at_top"]``), same
    rule as :func:`_run_speaker_facet`: a coin flip is not "the most".

    Args:
        db: An open, short-lived session, or ``None`` to decline (no Postgres).
        user_id: Caller, enforced via ``_accessible_scoped_files``.
        organization_id: Active tenant, or ``None`` for personal scope.
        file_uuids: Resolved scope. ``None`` declines (see above); an empty
            list has nothing to tally and declines quietly, matching every
            other shape's "empty explicit scope, nothing to say" behaviour.
        speaker_focus: A single active speaker-scope name, when the turn was
            asked with exactly one (``route.speakers``). Narrows the answer to
            that speaker alone.
        coverage: The shared coverage dict this call annotates in place.

    Returns:
        An :class:`AggregationResult`, or ``None`` when there is no session or
        no in-scope file to tally at all.
    """
    if db is None:
        return None
    if file_uuids is None:
        coverage["declined"] = (
            "talk-time stats need a bounded set of recordings, not the whole library"
        )
        return AggregationResult(
            shape=SHAPE_SPEAKER_STATS,
            mechanism="postgres: file_facts.facts['speakers']",
            subject="",
            coverage=coverage,
        )
    if not file_uuids:
        return None

    from sqlalchemy import select

    from app.models.file_facts import FileFacts
    from app.models.media import MediaFile

    scoped_predicate = _accessible_scoped_files(db, user_id, organization_id, file_uuids)
    rows = db.execute(
        select(MediaFile.uuid, FileFacts.facts)
        .select_from(MediaFile)
        .outerjoin(FileFacts, FileFacts.media_file_id == MediaFile.id)
        .where(scoped_predicate)
    ).all()

    missing = sum(1 for _uuid, facts in rows if facts is None)
    if missing:
        # Declines the WHOLE tally rather than summing the files that do have
        # a row — a partial talk-time tally read as the answer to "who talked
        # most" is the exact silent-wrong-answer shape this tier exists to
        # remove, and it looks identical to a complete one until the user
        # reads the coverage note.
        coverage["files_without_artifacts"] = missing
        return AggregationResult(
            shape=SHAPE_SPEAKER_STATS,
            mechanism="postgres: file_facts.facts['speakers']",
            subject="",
            coverage=coverage,
        )

    totals: dict[str, float] = {}
    undiarized_files = 0
    for _uuid, facts in rows:
        payload = facts or {}
        for entry in payload.get("speakers") or []:
            label = canonical_speaker_label(entry.get("name"))
            if label in UNKNOWN_SPEAKER_LABELS:
                continue
            totals[label] = totals.get(label, 0.0) + float(entry.get("total_time") or 0.0)
        # `facts['coverage']['undiarized_files_excluded']` is already computed
        # once, at ingest, by `build_facts` — 1 when the file had no diarized
        # speaker at all. Summing it here rather than re-deriving our own
        # per-file "is this undiarized" test keeps one definition of the term.
        undiarized_files += int(
            (payload.get("coverage") or {}).get("undiarized_files_excluded", 0) or 0
        )

    if undiarized_files:
        coverage["undiarized_files_excluded"] = undiarized_files

    if not totals:
        return None

    if speaker_focus:
        focus_label = canonical_speaker_label(speaker_focus)
        if focus_label not in totals:
            coverage["speaker_not_found"] = focus_label
            return AggregationResult(
                shape=SHAPE_SPEAKER_STATS,
                mechanism="postgres: file_facts.facts['speakers']",
                subject=focus_label,
                coverage=coverage,
            )
        totals = {focus_label: totals[focus_label]}

    ranked = sorted(totals.items(), key=lambda item: (-item[1], item[0]))
    top_name, top_seconds = ranked[0]
    tied = sum(1 for _name, seconds in ranked if seconds == top_seconds)
    if tied > 1:
        # Same rule as the facet: a tie means there is no "the most", and
        # naming one speaker anyway would be a coin flip presented as fact.
        coverage["tied_at_top"] = tied
        return AggregationResult(
            shape=SHAPE_SPEAKER_STATS,
            mechanism="postgres: file_facts.facts['speakers']",
            subject="",
            count=len(ranked),
            coverage=coverage,
        )

    return AggregationResult(
        shape=SHAPE_SPEAKER_STATS,
        mechanism="postgres: file_facts.facts['speakers']",
        subject="",
        count=len(ranked),
        speaker=top_name,
        speaker_seconds=round(top_seconds, 2),
        coverage=coverage,
    )
