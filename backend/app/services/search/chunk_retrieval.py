"""Raw chunk retrieval for RAG (issue #52).

``HybridSearchService.search()`` is built for the search UI: it collapses hits by
file, extracts snippets, highlights matches and redacts them for display. RAG needs
the opposite — the *full* text of the individual best-matching chunks, ungrouped, so
they can be reranked and packed into a prompt.

This module reuses the search service's filter construction and hybrid (BM25 +
neural, RRF-fused) query body, then returns flat chunk hits. It deliberately does NOT
share the search response cache: that cache is keyed and shaped for the collapsed UI
response, and unifying the two would couple an interactive display concern to prompt
construction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.core.config import settings
from app.services.opensearch_service import get_opensearch_client
from app.services.search.fusion import FusionConfig
from app.services.search.hybrid_search_service import HybridSearchService
from app.services.search.hybrid_search_service import ensure_fusion_pipeline

logger = logging.getLogger(__name__)

# Bounds for the adaptive RRF window. A window far larger than the result set
# costs latency for no recall; far smaller loses the tail RRF exists to fuse.
_RRF_WINDOW_MIN = 100


@dataclass
class ChunkHit:
    """One retrieved document, with everything a citation needs.

    Carries hits from **both** planes. ``digest_section`` is the discriminator
    and it is an explicit field rather than a sign test on ``chunk_index``: the
    negative sentinel is an index-sort implementation detail, and code that
    infers "this is a digest" from it breaks silently the day the sentinel
    scheme changes. ``is_digest`` is the only supported check.
    """

    file_uuid: str
    file_id: int
    chunk_index: int
    content: str
    title: str = ""
    speaker: str | None = None
    start_time: float = 0.0
    end_time: float | None = None
    score: float = 0.0
    #: Section number for a digest document; ``None`` for a transcript chunk.
    digest_section: int | None = None

    @property
    def is_digest(self) -> bool:
        return self.digest_section is not None

    def to_cache_dict(self) -> dict[str, Any]:
        return {
            "file_uuid": self.file_uuid,
            "file_id": self.file_id,
            "chunk_index": self.chunk_index,
            "content": self.content,
            "title": self.title,
            "speaker": self.speaker,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "score": self.score,
            "digest_section": self.digest_section,
        }

    @classmethod
    def from_cache_dict(cls, raw: dict[str, Any]) -> ChunkHit:
        section = raw.get("digest_section")
        return cls(
            file_uuid=str(raw.get("file_uuid", "")),
            file_id=int(raw.get("file_id", 0)),
            chunk_index=int(raw.get("chunk_index", 0)),
            content=str(raw.get("content", "")),
            title=str(raw.get("title", "")),
            speaker=raw.get("speaker"),
            start_time=float(raw.get("start_time", 0.0)),
            end_time=raw.get("end_time"),
            score=float(raw.get("score", 0.0)),
            digest_section=None if section is None else int(section),
        )


def dynamic_rrf_window(size: int) -> int:
    """Pick an RRF rank window proportional to how much we actually want back.

    The search UI always fuses over ``SEARCH_RRF_WINDOW_SIZE`` (500) because it
    paginates deep. Chat asks for a few dozen chunks, so a full-size window is
    wasted work — scale with the request and clamp to sane bounds.
    """
    return max(_RRF_WINDOW_MIN, min(size * 4, settings.SEARCH_RRF_WINDOW_SIZE))


def _hit_to_chunk(hit: dict[str, Any]) -> ChunkHit | None:
    source = hit.get("_source") or {}
    file_uuid = source.get("file_uuid")
    content = source.get("content")
    if not file_uuid or not content:
        return None
    return ChunkHit(
        file_uuid=str(file_uuid),
        file_id=int(source.get("file_id") or 0),
        chunk_index=int(source.get("chunk_index") or 0),
        content=str(content),
        title=str(source.get("title") or ""),
        speaker=source.get("speaker"),
        start_time=float(source.get("start_time") or 0.0),
        end_time=source.get("end_time"),
        score=float(hit.get("_score") or 0.0),
    )


def _build_body(
    query: str,
    filters: list[dict[str, Any]],
    size: int,
    use_neural: bool,
    model_id: str | None,
    service: HybridSearchService,
    search_mode: str = "hybrid",
) -> dict[str, Any]:
    """Build the retrieval body for the requested mode.

    The three modes are genuinely different queries, which is what makes the
    user-facing selector meaningful:

    * ``keyword``  — BM25 only. Exact words, no embedding.
    * ``semantic`` — the neural (vector) leg only. Finds passages that mean the
      same thing in different words, at the cost of missing rare literal terms
      like a product code the model never learned.
    * ``hybrid``   — both, fused with RRF. The default, and the right answer
      almost always.
    """
    text_query = service._build_text_query(query, ["content", "content.exact", "title"])
    source_fields = [
        "file_id",
        "file_uuid",
        "chunk_index",
        "content",
        "title",
        "speaker",
        "start_time",
        "end_time",
    ]

    if use_neural and model_id:
        neural_clause = {
            "neural": {
                "embedding": {
                    "query_text": query,
                    "model_id": model_id,
                    "k": dynamic_rrf_window(size),
                }
            }
        }

        if search_mode == "semantic":
            # Vector only — no BM25 leg, and therefore no RRF pipeline needed.
            return {
                "size": size,
                "query": {"bool": {"must": [neural_clause], "filter": filters}},
                "_source": source_fields,
                "track_total_hits": False,
            }

        return {
            "size": size,
            "query": {
                "hybrid": {
                    "queries": [
                        {"bool": {"must": [text_query], "filter": filters}},
                        {"bool": {"must": [neural_clause], "filter": filters}},
                    ]
                }
            },
            "_source": source_fields,
            "track_total_hits": False,
        }

    return {
        "size": size,
        "query": {"bool": {"must": [text_query], "filter": filters}},
        "_source": source_fields,
        "track_total_hits": False,
    }


def retrieve_chunks(
    query: str,
    *,
    user_id: int,
    organization_id: int | None = None,
    file_uuids: list[str] | None = None,
    speakers: list[str] | None = None,
    size: int = 48,
    search_mode: str = "hybrid",
    fusion: FusionConfig | None = None,
) -> list[ChunkHit]:
    """Retrieve the best-matching transcript chunks for ``query``.

    Args:
        query: The (possibly rewritten) user question.
        user_id: Caller — enforced via the ``accessible_user_ids`` term.
        organization_id: Active tenant, or None for personal scope.
        file_uuids: Resolved scope. ``None`` means every accessible transcript;
            an empty list means nothing matches (a scope that resolved to no files).
        speakers: Restrict to chunks spoken by these display names. Exact, because
            chunks are speaker turns — one chunk is one person talking.
        size: Candidate pool size to return before reranking.
        search_mode: ``hybrid`` (BM25 + vector), ``semantic``, or ``keyword``.
        fusion: Hybrid fusion strategy for **this call** (#363). None uses the
            configured default. Chat fuses over ``dynamic_rrf_window(size)``
            while the search UI always fuses over 500, so an A/B here does not
            characterise ``/api/search`` and vice versa — measure both.

    Returns:
        Chunk hits in provider-ranked order; empty on any retrieval failure —
        chat degrades to a context-free answer rather than erroring out.
    """
    clean = (query or "").strip()
    if not clean:
        return []
    if file_uuids is not None and not file_uuids:
        logger.info("Chat retrieval skipped: scope resolved to zero files")
        return []

    client = get_opensearch_client()
    if not client:
        logger.warning("Chat retrieval unavailable: no OpenSearch client")
        return []

    service = HybridSearchService()
    filters = service._build_filters(
        user_id,
        speakers or None,
        None,
        None,
        None,
        organization_id=organization_id,
        file_uuids=file_uuids,
    )

    _, use_neural, use_neural_query = service._generate_query_embedding(clean, search_mode)
    model_id = service._get_neural_model_id() if use_neural_query else None
    body = _build_body(clean, filters, size, use_neural_query, model_id, service, search_mode)

    params = {}
    # The fusion pipeline fuses the two legs of a hybrid query. A semantic-only or
    # BM25-only body has one leg, so applying it would be meaningless work.
    if use_neural_query and model_id and search_mode != "semantic":
        params["search_pipeline"] = ensure_fusion_pipeline(fusion)

    try:
        response = client.search(
            index=settings.OPENSEARCH_CHUNKS_INDEX, body=body, params=params or None
        )
    except Exception as exc:  # noqa: BLE001 — retrieval failure must not break chat
        logger.warning(f"Chat chunk retrieval failed: {exc}")
        return []

    hits = response.get("hits", {}).get("hits", [])
    chunks = [chunk for chunk in (_hit_to_chunk(h) for h in hits) if chunk is not None]
    logger.info(
        "Chat retrieval: %d chunks (mode=%s, neural=%s, scope=%s files, speakers=%s)",
        len(chunks),
        search_mode,
        bool(model_id),
        "all" if file_uuids is None else len(file_uuids),
        len(speakers) if speakers else "any",
    )
    return chunks


def _digest_hit_to_chunk(hit: dict[str, Any]) -> ChunkHit | None:
    """A digest document as a :class:`ChunkHit`, carrying its real timestamps.

    Addendum **G7**: a digest indexed with ``start_time=0`` would deep-link a
    citation to ``0:00``, which is a plausible-looking wrong answer. The
    extractive digest already paid for real section timestamps, so they are read
    here rather than re-derived per citation.

    ``speaker`` is left unset on purpose. A digest is not attributable to one
    person, and inventing an attribution is exactly the merge base rule 5
    forbids.
    """
    source = hit.get("_source") or {}
    file_uuid = source.get("file_uuid")
    content = source.get("content")
    section = source.get("digest_section")
    if not file_uuid or not content or section is None:
        return None
    return ChunkHit(
        file_uuid=str(file_uuid),
        file_id=int(source.get("file_id") or 0),
        chunk_index=int(source.get("chunk_index") or 0),
        content=str(content),
        title=str(source.get("title") or ""),
        speaker=None,
        start_time=float(source.get("start_time") or 0.0),
        end_time=source.get("end_time"),
        score=float(hit.get("_score") or 0.0),
        digest_section=int(section),
    )


def retrieve_digests(
    query: str,
    *,
    user_id: int,
    organization_id: int | None = None,
    file_uuids: list[str] | None = None,
    size: int = 12,
    search_mode: str = "hybrid",
    fusion: FusionConfig | None = None,
) -> list[ChunkHit]:
    """Retrieve digest-plane documents for ``query`` (#403 Stage 4).

    **A separate query, never fused with the chunk leg.** Route, don't fuse: a
    55-word digest section and a 200-word speaker turn have different length
    distributions, and RRF over a mixed pool ranks by an artefact of that
    difference rather than by relevance. The caller combines the two result
    lists; OpenSearch never sees them together.

    There is deliberately **no speaker parameter**. A digest document carries no
    single-valued ``speaker`` field at all, and its ``speakers`` array goes stale
    after a rename until the next reindex (``rename_propagation_task`` rewrites
    the chunk plane only), so a speaker-scoped digest query would silently drop
    the renamed speaker's material. The router removes the digest tier when a
    speaker filter is active; this signature makes that impossible to undo by
    accident.

    Args:
        query: The (possibly rewritten) user question.
        user_id: Caller — enforced via ``accessible_user_ids``.
        organization_id: Active tenant, or None for personal scope.
        file_uuids: Resolved scope. ``None`` = every accessible transcript; an
            empty list matches nothing.
        size: How many digest sections to return.
        search_mode: ``hybrid`` | ``semantic`` | ``keyword``.
        fusion: Hybrid fusion strategy for **this call** (#363); None uses the
            configured default. Threaded separately from ``retrieve_chunks``
            because the two legs are routed, never fused together — a sweep may
            legitimately want a different strategy on each.

    Returns:
        Digest hits in provider-ranked order; empty on any failure, so a broken
        digest leg degrades the answer rather than breaking the turn.
    """
    from app.services.ingest_artifacts.index_mapping import digest_plane_clause

    clean = (query or "").strip()
    if not clean:
        return []
    if file_uuids is not None and not file_uuids:
        return []

    client = get_opensearch_client()
    if not client:
        return []

    service = HybridSearchService()
    filters = service._build_filters(
        user_id,
        None,
        None,
        None,
        None,
        organization_id=organization_id,
        file_uuids=file_uuids,
    )
    # `_build_filters` hard-codes the CHUNK plane (that is its job for every
    # other reader). Swap that one clause rather than forking the function, so
    # the access gate and the tenant gate stay in exactly one place.
    from app.services.ingest_artifacts.index_mapping import chunk_plane_clause

    chunk_clause = chunk_plane_clause()
    filters = [f for f in filters if f != chunk_clause]
    filters.append(digest_plane_clause())

    _, _use_neural, use_neural_query = service._generate_query_embedding(clean, search_mode)
    model_id = service._get_neural_model_id() if use_neural_query else None
    body = _build_body(clean, filters, size, use_neural_query, model_id, service, search_mode)
    body["_source"] = [*body["_source"], "digest_section"]

    params = {}
    if use_neural_query and model_id and search_mode != "semantic":
        params["search_pipeline"] = ensure_fusion_pipeline(fusion)

    try:
        response = client.search(
            index=settings.OPENSEARCH_CHUNKS_INDEX, body=body, params=params or None
        )
    except Exception as exc:  # noqa: BLE001 — a failed digest leg must not break chat
        logger.warning(f"Chat digest retrieval failed: {exc}")
        return []

    hits = response.get("hits", {}).get("hits", [])
    digests = [d for d in (_digest_hit_to_chunk(h) for h in hits) if d is not None]
    logger.info("Chat digest retrieval: %d sections (mode=%s)", len(digests), search_mode)
    return digests


def diversity_sample(hits: list[ChunkHit], *, max_per_file: int, cap: int) -> list[ChunkHit]:
    """Round-robin across files so one long recording can't crowd out the rest.

    Chatting across several transcripts is the point of the feature; a purely
    score-ordered top-N regularly returns every chunk from the single longest
    file. Files are visited in order of their best-scoring chunk, taking one
    chunk each pass, which preserves "best file first" while guaranteeing the
    other selected files are represented.

    Args:
        hits: Retrieved chunks in score order.
        max_per_file: Ceiling on chunks contributed by any one file.
        cap: Total chunks to return.

    Returns:
        Re-ordered subset of ``hits``, at most ``cap`` long.
    """
    if not hits:
        return []

    by_file: dict[str, list[ChunkHit]] = {}
    for hit in hits:
        by_file.setdefault(hit.file_uuid, []).append(hit)

    # File order = best chunk each file achieved (hits arrive score-ordered, so
    # first-seen is best-seen).
    file_order = list(by_file.keys())

    selected: list[ChunkHit] = []
    for round_index in range(max_per_file):
        for file_uuid in file_order:
            chunks = by_file[file_uuid]
            if round_index < len(chunks):
                selected.append(chunks[round_index])
                if len(selected) >= cap:
                    return selected
    return selected
