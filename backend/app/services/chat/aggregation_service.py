"""Running the five aggregation shapes (#403 Stage 4, Phase 5; the fifth shape is the speaker-stats one).

Siblings: ``aggregation_queries`` (scoped I/O primitives and the tenant/quarantine chokepoints),
``aggregation_speakers`` (the facet and talk-time-stats shapes), ``recurrence_service``.

The pure half — subject extraction, shape choice, filter construction — lives in
:mod:`app.services.chat.aggregation` and is testable with no services. This is
the half that talks to OpenSearch and Postgres.

Worst case for any question, at any scope: **one** ``size: 0`` search plus at
most **three** Postgres statements — one to resolve a date filter to file uuids,
one to count the in-scope files that have no recorded date, and one for the
occurrence count. It was one until the date filter moved off the index; see
:func:`_files_in_period` for why that move was correctness and not preference.
The scope itself is capped at
:data:`app.core.chat_defaults.CHAT_MAX_SCOPE_FILES` upstream, and that cap is reused
here rather than a second one being invented. Two exceptions to the "three
statements" ceiling, both the speaker facet's content-scope choice and
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

import logging
from typing import Any

from app.core import chat_defaults
from app.services.chat.aggregation import SHAPE_COUNT_EVENTS
from app.services.chat.aggregation import SHAPE_SPEAKER_FACET
from app.services.chat.aggregation import SHAPE_SPEAKER_STATS
from app.services.chat.aggregation import AggregationResult
from app.services.chat.aggregation import base_filters
from app.services.chat.aggregation import choose_shape
from app.services.chat.aggregation import extract_subject
from app.services.chat.aggregation import subject_clause
from app.services.chat.aggregation_queries import _MENTION_FIELDS
from app.services.chat.aggregation_queries import SessionFactory
from app.services.chat.aggregation_queries import _files_in_period
from app.services.chat.aggregation_queries import _files_matching
from app.services.chat.aggregation_queries import _flag_enabled
from app.services.chat.aggregation_queries import _occurrence_count
from app.services.chat.aggregation_queries import _quarantined_among
from app.services.chat.aggregation_queries import _short_session
from app.services.chat.aggregation_queries import _temporal_bounds
from app.services.chat.aggregation_speakers import _SETTING_SPEAKER_STATS_ENABLED
from app.services.chat.aggregation_speakers import _run_speaker_facet
from app.services.chat.aggregation_speakers import _run_speaker_stats
from app.services.chat.router import Route

logger = logging.getLogger(__name__)


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
                chat_defaults.DEFAULT_CHAT_AGGREGATE_SPEAKER_STATS_ENABLED,
            ):
                # Flag off: fall back to whatever `choose_shape` would have
                # returned with the `who-talked-most` signal removed — the
                # same shape this question would have hit before this feature
                # existed (see router.py's `_AGGREGATE_HEADS_STANDALONE`,
                # where "who talked the most" already satisfies the older
                # `who-most` pattern). This is what keeps the flag OFF path
                # byte-identical to the behaviour before the speaker-facet flags existed.
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
