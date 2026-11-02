"""What a turn's evidence passes through after retrieval, and what is recorded about it.

The quarantine drop for the ranked tiers, the read-time expansion count, the merge
(digests lead) and the fail-closed filter that drops whatever masking emptied, plus
the grounding diagnostics (languages, retrieval counters) written to ``meta``.

``_drop_quarantined_hits`` is called through ``service.py``'s module globals (from
``service._drop_quarantined``), which is where tests patch it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.services.chat.language import METADATA_KEY as LANGUAGE_METADATA_KEY
from app.services.chat.language import WARNING_CODE as LANGUAGE_WARNING_CODE
from app.services.chat.language import describe_context_languages
from app.services.chat.redactor import MaskedChunk
from app.services.chat.retrieval import RetrievalResult
from app.services.chat.trace import Outcome as TraceOutcome
from app.services.chat.trace import QueryStage
from app.services.chat.trace import TraceRecorder
from app.services.chat.trace import emit as emit_trace
from app.services.search.chunk_retrieval import ChunkHit
from app.services.takedown_predicates import quarantined_among


def _drop_quarantined_hits(db: Session, hits: list[ChunkHit]) -> list[ChunkHit]:
    """Remove hits belonging to quarantined files, chunk **or** digest.

    Quarantine status is not an OpenSearch filter field — it can lag a takedown
    by a reindex — so scope resolution filters it in Postgres
    (``context_resolver._visible_files_query``) whenever a turn is explicitly
    scoped. An **unscoped** turn (``file_uuids is None``) never routes through
    that resolver at all: retrieval goes straight to the index, which still
    returns the quarantined file's chunks and digest sections. This is the
    chat-plane analogue of ``api/endpoints/search_helpers.py::_drop_quarantined_search_hits``
    and must run over both planes, since a summarize-routed turn's digest hits
    never pass through the chunk leg.

    ⚠️ **This is NOT the only place a quarantine flag is enforced in the chat
    pipeline, and an earlier revision of this docstring claiming that was
    itself the bug** (found in an adversarial review). This function runs at
    ``_prepare_context`` phase 3.5 and only ever sees ``result.chunks`` /
    ``result.digests`` — the RANKED tiers. The counted/aggregation tier
    (``aggregation_service.answer_aggregation``) runs a whole phase EARLIER, at
    phase 2, and is a separate code path this function never touches:
    ``aggregation_queries._accessible_scoped_files`` and
    ``aggregation_queries._quarantined_among`` are the enforcement points for
    that tier, on the Postgres and OpenSearch shapes respectively. The
    map-reduce overview's bounded-scope leg (``mapreduce.scope_digest_hits``)
    is a third, independent enforcement point for the same reason — it re-reads
    ``file_facts`` directly rather than routing through this function's output.
    """
    if not hits:
        return hits
    quarantined = quarantined_among(db, (hit.file_uuid for hit in hits))
    if not quarantined:
        return hits
    return [hit for hit in hits if hit.file_uuid not in quarantined]


def _emit_expansion(recorder, masked: list, *, enabled: bool) -> None:
    """Report read-time context expansion (#523) on the trace.

    It runs INSIDE ``mask_chunks``, so the count is only knowable after masking
    returns — hence a helper called from there rather than an emit beside the
    other narrowing stages.

    ``chat.context_expansion_enabled`` is admin-editable (default ON as of
    2026-09-21), and a stage that did not run still reports ``SKIPPED`` rather
    than vanishing: an absent row reads as
    "not part of this pipeline", which is precisely the ambiguity the panel
    exists to remove. ``chunk.expanded`` counts chunks whose own time range was
    widened; the rest were already long enough to keep as they were.
    """
    if not enabled:
        emit_trace(
            recorder,
            QueryStage.EXPANDED,
            TraceOutcome.SKIPPED,
            node_id="expansion",
            reason="disabled",
        )
        return

    widened = sum(1 for chunk in masked if chunk.expanded)
    emit_trace(
        recorder,
        QueryStage.EXPANDED,
        TraceOutcome.OK if widened else TraceOutcome.EMPTY,
        node_id="expansion",
        count=widened,
        kept=len(masked),
    )


def _merge_evidence(
    masked: list[MaskedChunk],
    digest_masked: list[MaskedChunk],
    result: RetrievalResult,
    *,
    meta: dict[str, Any],
    recorder: TraceRecorder | None,
) -> list[MaskedChunk]:
    """Phase 5 of ``_prepare_context``: digests first, then drop what masking emptied."""
    if digest_masked:
        # Digests lead: they are the recording-level answer a summarize turn
        # asked for, and the chunk excerpts under them are the evidence. This
        # is independent of whether the OVERVIEW came from the scope map or
        # the ranked leg — the ranked digest hits are still evidence chunks
        # either way.
        masked = digest_masked + masked
    if result.digests:
        meta["digests_retrieved"] = len(result.digests)

    kept = [chunk for chunk in masked if chunk.content.strip()]
    # Masking fails CLOSED: an unmaskable chunk becomes "" and contributes
    # nothing. Without this counter that is indistinguishable from retrieval
    # returning less, which is a different defect with a different fix.
    meta["chunks_dropped_empty_after_masking"] = len(masked) - len(kept)
    # The headline case this panel exists for: retrieval worked, masking failed
    # closed on EVERY chunk, and the model answered anyway. `EMPTY` rather than
    # `OK` when nothing survived, so it cannot read as a filter that passed.
    emit_trace(
        recorder,
        QueryStage.FILTERED,
        TraceOutcome.OK if kept else TraceOutcome.EMPTY,
        node_id="masking",
        kept=len(kept),
        dropped=len(masked) - len(kept),
    )
    masked = kept
    return masked


def _record_languages(
    session_scope, file_uuids: list[str] | None, masked: list[MaskedChunk], meta: dict[str, Any]
) -> None:
    """Phase 5 of ``_prepare_context``: the languages this turn could draw on."""
    # RAG is English-only (see services/chat/language.py). Recording what the turn
    # could draw on is what makes that limit visible instead of silent.
    #
    # Its own short session, and it must stay one: this read used to be handed the
    # phase-4 session AFTER that `with` block had closed it, which SQLAlchemy
    # answers by silently opening a fresh transaction on a connection nothing ever
    # returns — an `idle in transaction` backend left behind by every turn, freed
    # only when the garbage collector reaches the connection.
    with session_scope() as db:
        languages = describe_context_languages(
            db,
            scope_file_uuids=file_uuids,
            grounded_file_uuids=[chunk.source.file_uuid for chunk in masked],
        )
    if languages.total_files:
        meta[LANGUAGE_METADATA_KEY] = languages.as_metadata()
    if languages.has_unsupported:
        meta[LANGUAGE_WARNING_CODE] = True


def _record_retrieval_diagnostics(
    result: RetrievalResult,
    file_uuids: list[str] | None,
    speakers: list[str] | None,
    meta: dict[str, Any],
) -> None:
    """Phase 5 of ``_prepare_context``: what retrieval did, as counts and timings."""
    meta["retrieved"] = result.retrieved
    meta["reranked"] = result.reranked
    meta["cache_hit"] = result.cache_hit
    # Only when true: a key most turns never carry keeps `msg_metadata` from
    # growing a permanent "search worked fine" marker on every ordinary turn.
    if result.retrieval_failed:
        meta["retrieval_failed"] = True
    meta["files_searched"] = "all" if file_uuids is None else len(file_uuids)
    if speakers:
        meta["speakers_filtered"] = list(speakers)
    timings = meta.setdefault("timings_ms", {})
    timings.update(result.timings_ms)
