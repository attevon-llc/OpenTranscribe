"""The two speaker-keyed aggregation shapes: the facet (OpenSearch) and talk-time stats (Postgres)."""

from __future__ import annotations

import logging
from typing import Any

from app.core import chat_defaults
from app.services.chat.aggregation import SHAPE_SPEAKER_FACET
from app.services.chat.aggregation import SHAPE_SPEAKER_STATS
from app.services.chat.aggregation import AggregationResult
from app.services.chat.aggregation import subject_clause
from app.services.chat.aggregation_queries import _MENTION_FIELDS
from app.services.chat.aggregation_queries import _TITLE_FIELDS
from app.services.chat.aggregation_queries import SessionFactory
from app.services.chat.aggregation_queries import _accessible_scoped_files
from app.services.chat.aggregation_queries import _flag_enabled
from app.services.chat.aggregation_queries import _quarantined_among
from app.services.chat.aggregation_queries import _speaker_tally
from app.utils.speaker_labels import UNKNOWN_SPEAKER_LABELS
from app.utils.speaker_labels import canonical_speaker_label

logger = logging.getLogger(__name__)

#: Flag-gated setting keys. Read via :func:`_flag_enabled`, never through
#: :mod:`app.services.chat.settings` — that module resolves all thirteen (now
#: fifteen) RAG knobs in one query, and pulling it in here for two booleans
#: would couple a module that already budgets its own Postgres statement count
#: to every other admin knob's schema.
_SETTING_SPEAKER_FACET_CONTENT_SCOPE = "chat.aggregate.speaker_facet_content_scope"


_SETTING_SPEAKER_STATS_ENABLED = "chat.aggregate.speaker_stats_enabled"


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
        chat_defaults.DEFAULT_CHAT_AGGREGATE_SPEAKER_FACET_CONTENT_SCOPE,
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
