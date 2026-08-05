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

from app.services.chat.settings import ChatSettings
from app.services.search.chunk_retrieval import ChunkHit
from app.services.search.chunk_retrieval import diversity_sample
from app.services.search.chunk_retrieval import retrieve_chunks

logger = logging.getLogger(__name__)


@dataclass
class RetrievalResult:
    """Chunks for the prompt, plus diagnostics surfaced in message metadata."""

    chunks: list[ChunkHit] = field(default_factory=list)
    retrieved: int = 0
    reranked: int = 0
    cache_hit: bool = False
    timings_ms: dict[str, int] = field(default_factory=dict)


def retrieve_context(
    *,
    query: str,
    user_id: int,
    organization_id: int | None,
    file_uuids: list[str] | None,
    settings: ChatSettings,
    search_mode: str = "hybrid",
) -> RetrievalResult:
    """Run the retrieval pipeline for one question.

    Args:
        query: The (possibly rewritten) question.
        user_id: Caller — enforced inside the OpenSearch filter.
        organization_id: Active tenant, or None for personal scope.
        file_uuids: Resolved scope; None means all accessible transcripts.
        settings: Admin-tuned RAG knobs.
        search_mode: ``hybrid`` | ``semantic`` | ``keyword``.

    Returns:
        A :class:`RetrievalResult`; empty chunks when nothing matched or
        retrieval was unavailable (chat then answers without context).
    """
    from app.services.chat import retrieval_cache

    result = RetrievalResult()
    started = time.monotonic()

    scope_digest = retrieval_cache.scope_hash(file_uuids)
    key = retrieval_cache.cache_key(
        user_id=user_id,
        organization_id=organization_id,
        query=query,
        scope_digest=scope_digest,
        settings_rev=settings.revision,
        search_mode=search_mode,
    )

    cached = retrieval_cache.get_cached(key)
    if cached is not None:
        result.chunks = cached[: settings.final_chunks]
        result.retrieved = len(cached)
        result.cache_hit = True
        result.timings_ms["total"] = int((time.monotonic() - started) * 1000)
        logger.info("Chat retrieval cache hit (%d chunks)", len(result.chunks))
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
            return result

    # Over-fetch: the pool feeds diversity sampling and reranking, not the prompt.
    retrieve_started = time.monotonic()
    hits = retrieve_chunks(
        query,
        user_id=user_id,
        organization_id=organization_id,
        file_uuids=file_uuids,
        size=settings.candidate_pool,
        search_mode=search_mode,
    )
    result.retrieved = len(hits)
    result.timings_ms["retrieve"] = int((time.monotonic() - retrieve_started) * 1000)

    if not hits:
        result.timings_ms["total"] = int((time.monotonic() - started) * 1000)
        return result

    # Rerank BEFORE narrowing: the cross-encoder is what decides relevance, so it
    # must see the whole pool. Diversity is then applied to the reranked order.
    if settings.rerank_enabled:
        from app.services.chat.reranker import rerank

        rerank_started = time.monotonic()
        hits = rerank(query, hits, max_pairs=settings.rerank_max_pairs)
        result.reranked = min(len(hits), settings.rerank_max_pairs)
        result.timings_ms["rerank"] = int((time.monotonic() - rerank_started) * 1000)

    selected = diversity_sample(
        hits,
        max_per_file=settings.max_chunks_per_file,
        cap=settings.final_chunks,
    )
    result.chunks = selected
    result.timings_ms["total"] = int((time.monotonic() - started) * 1000)

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
