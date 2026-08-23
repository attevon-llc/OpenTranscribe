"""The RAG retrieval pipeline for one chat turn.

Stages, in order:

1. **Cache** — exact-query lookup keyed by user, scope and settings revision.
2. **Retrieve** — hybrid BM25 + vector search over transcript chunks, fetching a
   candidate pool deliberately larger than what the prompt will hold.
3. **Diversity sample** — round-robin across files so one long recording can't
   monopolize the context when several transcripts are selected.
4. **Rerank** — cross-encoder scoring of (question, chunk) pairs for precision.

Over-fetching then narrowing is the point: recall-oriented retrieval finds the
candidates, precision-oriented reranking picks which ones are worth prompt space.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from app.services.chat.settings import ChatSettings
from app.services.chat.trace import Outcome
from app.services.chat.trace import QueryStage
from app.services.chat.trace import TraceRecorder
from app.services.chat.trace import emit
from app.services.search.chunk_retrieval import ChunkHit
from app.services.search.chunk_retrieval import diversity_sample
from app.services.search.chunk_retrieval import retrieve_chunks

logger = logging.getLogger(__name__)


@dataclass
class RetrievalResult:
    """Chunks for the prompt, plus diagnostics surfaced in message metadata."""

    chunks: list[ChunkHit] = field(default_factory=list)
    #: Digest-plane hits, kept in their OWN list. Route, don't fuse: they are a
    #: separate query whose results the prompt layer interleaves, never a second
    #: population merged into one ranking.
    digests: list[ChunkHit] = field(default_factory=list)
    retrieved: int = 0
    reranked: int = 0
    cache_hit: bool = False
    #: True when the chunk-plane search itself failed (no OpenSearch client, or
    #: the query raising) rather than legitimately returning zero hits. Lets a
    #: caller with an empty ``chunks`` list distinguish "your library has
    #: nothing about this" from "search was down" (issue #438's open half).
    retrieval_failed: bool = False
    #: W2.2. How many hits the parallel speaker-focus leg (see
    #: ``retrieve_context``'s ``speaker_focus_names``) contributed that the
    #: main leg had not already found. ``0`` both when the leg was not run and
    #: when it ran and added nothing new — the caller only needs to know a
    #: contribution happened, not to distinguish those two zeros.
    speaker_focus_added: int = 0
    timings_ms: dict[str, int] = field(default_factory=dict)


def _emit_narrowing_skipped(
    recorder: TraceRecorder | None, parent: str | None, *, reason: str
) -> None:
    """Mark rerank and diversity sampling as never having run.

    Rendering them as SKIPPED rather than simply omitting them is the honesty
    rule this trace exists for: an absent node reads as "not part of this
    pipeline", while a skipped one says "we did not need to, and here is why".
    """
    for stage, node_id in ((QueryStage.RERANKED, "rerank"), (QueryStage.SAMPLED, "sample")):
        emit(recorder, stage, Outcome.SKIPPED, parent=parent, node_id=node_id, reason=reason)


def _emit_search_skipped(
    recorder: TraceRecorder | None, parent: str | None, *, reason: str
) -> None:
    """Mark the search itself, and everything downstream of it, as never run.

    Only correct where the search genuinely did not happen — a cache hit. Do NOT
    use it when the search ran and returned nothing: that leg's ``FOUND`` is
    ``EMPTY``, and relabelling it ``SKIPPED`` would erase the exact distinction
    ("we looked and found nothing" vs "we never looked") this trace is for.
    """
    emit(
        recorder,
        QueryStage.FANNED_VECTOR,
        Outcome.SKIPPED,
        parent=parent,
        node_id="main",
        plane="chunk",
        reason=reason,
    )
    _emit_narrowing_skipped(recorder, parent, reason=reason)


def retrieve_context(
    *,
    query: str,
    user_id: int,
    organization_id: int | None,
    file_uuids: list[str] | None,
    speakers: list[str] | None = None,
    settings: ChatSettings,
    search_mode: str = "hybrid",
    wants_digest: bool = False,
    digest_size: int = 6,
    speaker_focus_names: list[str] | None = None,
    recorder: TraceRecorder | None = None,
    parent: str | None = None,
) -> RetrievalResult:
    """Run the retrieval pipeline for one question.

    Args:
        query: The (possibly rewritten) question.
        user_id: Caller — enforced inside the OpenSearch filter.
        organization_id: Active tenant, or None for personal scope.
        file_uuids: Resolved scope; None means all accessible transcripts.
        speakers: The EXPLICIT, hard scope (``ChatScope.speakers`` / a
            checkbox pick) — restricts the main leg to these speakers' turns
            (None/empty = anyone). Untouched by ``speaker_focus_names`` below.
        settings: Admin-tuned RAG knobs.
        search_mode: ``hybrid`` | ``semantic`` | ``keyword``.
        wants_digest: Run the digest leg as well (the router's summarize tier).
        digest_size: How many digest sections to fetch.
        speaker_focus_names: W2.2. Names ``chat.speaker_resolver`` resolved
            from a MENTION in the question text (behind
            ``chat.speaker_resolver_enabled``, off by default) — NOT the
            explicit ``speakers`` scope above, and never merged with it.
            When set, a PARALLEL second chunk leg
            (``retrieve_chunks(speakers=speaker_focus_names)``) is unioned
            into the candidate pool, deduped by ``(file_uuid, chunk_index)``,
            before reranking — so the cross-encoder scores the combined pool
            on one scale rather than two legs reranked separately. This can
            only WIDEN what the main leg already returns: nothing is dropped
            to make room for it, and an explicit ``speakers`` scope is never
            narrowed or replaced by it. Skipped on a cache hit — a hit already
            reflects whatever leg mix produced it the first time this exact
            query/scope/settings combination ran, matching how the exact-cache
            tier treats retrieval as one atomic unit (unlike the digest leg,
            which runs unconditionally because its cost/staleness profile
            differs).

    Returns:
        A :class:`RetrievalResult`; empty chunks when nothing matched or
        retrieval was unavailable (chat then answers without context).
    """
    from app.services.chat import retrieval_cache

    result = RetrievalResult()
    started = time.monotonic()

    scope_digest = retrieval_cache.scope_hash(file_uuids, speakers)
    key = retrieval_cache.cache_key(
        user_id=user_id,
        organization_id=organization_id,
        query=query,
        scope_digest=scope_digest,
        settings_rev=settings.revision,
        search_mode=search_mode,
    )

    # The digest leg is deliberately OUTSIDE the chunk cache. The cache key is
    # built for the chunk plane, and widening it would invalidate every cached
    # entry in every deployment on upgrade for the sake of one extra query on
    # summarize turns only. Running it unconditionally also means a cache HIT on
    # the chunk leg still produces digests, rather than a summarize answer that
    # silently loses its summary tier for the cache TTL.
    if wants_digest:
        from app.services.search.chunk_retrieval import retrieve_digests

        digest_started = time.monotonic()
        emit(
            recorder,
            QueryStage.FANNED_VECTOR,
            parent=parent,
            node_id="digest",
            plane="digest",
            source="opensearch",
        )
        result.digests = retrieve_digests(
            query,
            user_id=user_id,
            organization_id=organization_id,
            file_uuids=file_uuids,
            size=digest_size,
            search_mode=search_mode,
        )
        result.timings_ms["digest"] = int((time.monotonic() - digest_started) * 1000)
        emit(
            recorder,
            QueryStage.FOUND,
            Outcome.OK if result.digests else Outcome.EMPTY,
            parent=parent,
            node_id="digest",
            plane="digest",
            count=len(result.digests),
            ms=result.timings_ms["digest"],
        )
    else:
        emit(
            recorder,
            QueryStage.FANNED_VECTOR,
            Outcome.SKIPPED,
            parent=parent,
            node_id="digest",
            plane="digest",
            reason="not_applicable",
        )

    cached = retrieval_cache.get_cached(key)
    if cached is not None:
        result.chunks = cached[: settings.final_chunks]
        result.retrieved = len(cached)
        result.cache_hit = True
        result.timings_ms["total"] = int((time.monotonic() - started) * 1000)
        logger.info("Chat retrieval cache hit (%d chunks)", len(result.chunks))
        emit(
            recorder,
            QueryStage.CACHE_LOOKUP,
            Outcome.CACHED,
            parent=parent,
            node_id="cache",
            source="cache",
            count=len(cached),
            ms=result.timings_ms["total"],
        )
        _emit_search_skipped(recorder, parent, reason="cached")
        return result

    # Tier 2: a rephrasing of a recent question retrieves the same passages.
    # Reused here so we don't re-embed on the write path below.
    query_embedding: list[float] | None = None
    if settings.semantic_cache_enabled:
        semantic = retrieval_cache.find_semantic_match(
            user_id=user_id,
            organization_id=organization_id,
            query=query,
            scope_digest=scope_digest,
            settings_rev=settings.revision,
            threshold=settings.semantic_cache_threshold,
        )
        if semantic is not None:
            hits, query_embedding = semantic
            result.chunks = hits[: settings.final_chunks]
            result.retrieved = len(hits)
            result.cache_hit = True
            result.timings_ms["total"] = int((time.monotonic() - started) * 1000)
            emit(
                recorder,
                QueryStage.CACHE_LOOKUP,
                Outcome.CACHED,
                parent=parent,
                node_id="cache",
                source="cache",
                count=len(hits),
                reason="semantic",
                ms=result.timings_ms["total"],
            )
            _emit_search_skipped(recorder, parent, reason="cached")
            return result

    # Neither cache tier answered. A MISS is worth its own node: it is invisible
    # today, and "we looked in the cache and it was not there" is a different
    # fact from "this deployment has no cache".
    emit(
        recorder,
        QueryStage.CACHE_LOOKUP,
        Outcome.EMPTY,
        parent=parent,
        node_id="cache",
        source="cache",
        count=0,
    )

    # Over-fetch: the pool feeds diversity sampling and reranking, not the prompt.
    retrieve_started = time.monotonic()
    chunk_diagnostics: dict[str, Any] = {}
    # ⚠️ ONE node, not two. `retrieve_chunks` runs a single chunk-plane query,
    # so there is no second leg to report — and inventing a sibling node for one
    # would misdescribe what actually ran.
    emit(
        recorder,
        QueryStage.FANNED_VECTOR,
        parent=parent,
        node_id="main",
        plane="chunk",
        source="opensearch",
        limit=settings.candidate_pool,
    )
    hits = retrieve_chunks(
        query,
        user_id=user_id,
        organization_id=organization_id,
        file_uuids=file_uuids,
        speakers=speakers,
        size=settings.candidate_pool,
        search_mode=search_mode,
        diagnostics=chunk_diagnostics,
    )
    result.retrieved = len(hits)
    result.retrieval_failed = bool(chunk_diagnostics.get("retrieval_failed"))
    result.timings_ms["retrieve"] = int((time.monotonic() - retrieve_started) * 1000)
    # `retrieve_chunks` degrades to [] on ANY failure, so an empty list alone
    # cannot tell "found nothing" from "the search broke" — which is the whole
    # reason `diagnostics` exists (issue #438). The trace must not collapse them.
    if result.retrieval_failed:
        found_outcome = Outcome.FAILED
    elif hits:
        found_outcome = Outcome.OK
    else:
        found_outcome = Outcome.EMPTY
    emit(
        recorder,
        QueryStage.FOUND,
        found_outcome,
        parent=parent,
        node_id="main",
        plane="chunk",
        count=len(hits),
        ms=result.timings_ms["retrieve"],
        **({"reason": "search_failed"} if result.retrieval_failed else {}),
    )

    # W2.2: the parallel speaker-focus leg. Additive only — see the
    # `speaker_focus_names` docstring above for why this never narrows the
    # main leg and never touches the explicit `speakers` scope.
    if speaker_focus_names:
        focus_started = time.monotonic()
        emit(
            recorder,
            QueryStage.FANNED_VECTOR,
            parent=parent,
            node_id="speaker_focus",
            plane="chunk",
            source="opensearch",
        )
        focus_hits = retrieve_chunks(
            query,
            user_id=user_id,
            organization_id=organization_id,
            file_uuids=file_uuids,
            speakers=speaker_focus_names,
            size=settings.candidate_pool,
            search_mode=search_mode,
        )
        result.timings_ms["speaker_focus"] = int((time.monotonic() - focus_started) * 1000)
        if focus_hits:
            seen = {(h.file_uuid, h.chunk_index) for h in hits}
            added = [h for h in focus_hits if (h.file_uuid, h.chunk_index) not in seen]
            if added:
                hits = hits + added
                result.retrieved += len(added)
                result.speaker_focus_added = len(added)
                logger.info(
                    "Chat speaker-focus leg (%s): +%d chunks merged into the main pool",
                    ", ".join(speaker_focus_names),
                    len(added),
                )
        # `count` is what this leg CONTRIBUTED after dedup, not what it matched:
        # a leg that returned 20 chunks the main leg already had added nothing,
        # and reporting 20 would overstate the evidence it brought.
        emit(
            recorder,
            QueryStage.FOUND,
            Outcome.OK if result.speaker_focus_added else Outcome.EMPTY,
            parent=parent,
            node_id="speaker_focus",
            plane="chunk",
            count=result.speaker_focus_added,
            ms=result.timings_ms["speaker_focus"],
        )

    if not hits:
        result.timings_ms["total"] = int((time.monotonic() - started) * 1000)
        # The search RAN and returned nothing — its FOUND is already EMPTY above.
        # Only the narrowing stages never happened.
        _emit_narrowing_skipped(recorder, parent, reason="no_candidates")
        return result

    # Rerank BEFORE narrowing: the cross-encoder is what decides relevance, so it
    # must see the whole pool. Diversity is then applied to the reranked order.
    if settings.rerank_enabled:
        from app.services.chat.reranker import rerank

        rerank_started = time.monotonic()
        hits = rerank(query, hits, max_pairs=settings.rerank_max_pairs)
        result.reranked = min(len(hits), settings.rerank_max_pairs)
        result.timings_ms["rerank"] = int((time.monotonic() - rerank_started) * 1000)
        emit(
            recorder,
            QueryStage.RERANKED,
            parent=parent,
            node_id="rerank",
            count=result.reranked,
            limit=settings.rerank_max_pairs,
            ms=result.timings_ms["rerank"],
        )
    else:
        emit(
            recorder,
            QueryStage.RERANKED,
            Outcome.SKIPPED,
            parent=parent,
            node_id="rerank",
            reason="disabled",
        )

    pool_size = len(hits)
    selected = diversity_sample(
        hits,
        max_per_file=settings.max_chunks_per_file,
        cap=settings.final_chunks,
    )
    result.chunks = selected
    result.timings_ms["total"] = int((time.monotonic() - started) * 1000)
    # The "48 -> 12, max 4 per file" node. This is where most of a turn's
    # candidate evidence is discarded, and until now nothing reported it —
    # the answer simply arrived grounded in a quarter of what was found.
    emit(
        recorder,
        QueryStage.SAMPLED,
        Outcome.OK if selected else Outcome.EMPTY,
        parent=parent,
        node_id="sample",
        kept=len(selected),
        dropped=max(0, pool_size - len(selected)),
        limit=settings.max_chunks_per_file,
    )

    retrieval_cache.set_cached(key, selected, settings.cache_ttl_seconds)
    if settings.semantic_cache_enabled:
        retrieval_cache.remember_semantic(
            user_id=user_id,
            organization_id=organization_id,
            query=query,
            cache_key_value=key,
            scope_digest=scope_digest,
            settings_rev=settings.revision,
            ttl_seconds=settings.cache_ttl_seconds,
            embedding=query_embedding,
        )

    logger.info(
        "Chat retrieval: %d candidates -> %d selected across %d files (%dms)",
        result.retrieved,
        len(selected),
        len({c.file_uuid for c in selected}),
        result.timings_ms["total"],
    )
    return result
