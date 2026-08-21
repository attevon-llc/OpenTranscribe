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
from dataclasses import field
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

#: The BM25 field list ``_build_body`` has always queried when no override is given —
#: pulled out as a name so ``TEXT_FIELD_PRESET_DEFAULT`` and the hard-coded fallback can
#: never drift apart.
_DEFAULT_TEXT_FIELDS = ("content", "content.exact", "title")

#: Named ``text_fields`` presets for :func:`retrieve_chunks`/:func:`retrieve_digests`
#: (issue #506, the no-stemmed-leg arm). ``"default"`` resolves to ``None`` — the field
#: list this module has always queried, so a caller that never asks for a preset gets a
#: byte-identical query body. ``"no-stem"`` drops the STEMMED ``content`` leg entirely by
#: reusing ``HybridSearchService._get_search_fields``'s own boost logic
#: (``use_exact=True``) rather than hand-rolling a second field list that could drift
#: from the search UI's.
TEXT_FIELD_PRESET_DEFAULT = "default"
TEXT_FIELD_PRESET_NO_STEM = "no-stem"
TEXT_FIELD_PRESETS = (TEXT_FIELD_PRESET_DEFAULT, TEXT_FIELD_PRESET_NO_STEM)


def resolve_text_field_preset(preset: str, *, has_speaker_filter: bool = False) -> list[str] | None:
    """Turn a named ``text_fields`` preset into the field list ``_build_body`` should query.

    Args:
        preset: One of :data:`TEXT_FIELD_PRESETS`.
        has_speaker_filter: Forwarded to ``HybridSearchService._get_search_fields`` — a
            speaker-scoped turn already narrows to one person's chunks, so the
            ``"no-stem"`` preset drops the ``speaker`` field the same way the search UI's
            own boosted field list does.

    Returns:
        ``None`` for ``"default"`` — the caller's existing hard-coded field list applies
        unchanged — or the resolved field list for ``"no-stem"``.

    Raises:
        ValueError: ``preset`` is not one of :data:`TEXT_FIELD_PRESETS`.
    """
    if preset == TEXT_FIELD_PRESET_DEFAULT:
        return None
    if preset == TEXT_FIELD_PRESET_NO_STEM:
        return HybridSearchService()._get_search_fields(has_speaker_filter, use_exact=True)
    raise ValueError(f"Unknown text field preset {preset!r}; expected one of {TEXT_FIELD_PRESETS}")


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
    #: ISO 639-1 code of the recording this chunk came from, straight off the chunk
    #: document's ``language`` keyword. Empty when the file predates detection.
    #: Read by the chat reranker, which must not reorder text it cannot read.
    language: str = ""
    #: Section number for a digest document; ``None`` for a transcript chunk.
    digest_section: int | None = None
    #: ``"media"`` (default) or ``"document"`` — which table ``file_id`` addresses.
    #: **Load-bearing, not descriptive.** ``Document.id`` and ``MediaFile.id`` are
    #: independent SERIAL sequences that WILL collide in any real deployment (both
    #: get written into the same ``file_id`` index field), so any code that queries
    #: a table by ``chunk.file_id`` — masking chief among them
    #: (``services/chat/redactor.py``) — must dispatch on this field FIRST and never
    #: infer the source from "the MediaFile lookup returned None". Populated from the
    #: index document's ``doc_type`` (``document_chunk`` → ``"document"``, anything
    #: else → ``"media"``) at construction time, never guessed downstream.
    source_kind: str = "media"
    #: 1-based page number a document chunk falls on, or ``None`` for a
    #: transcript chunk/digest (no page concept) or a document chunk whose
    #: source format has no pages. **Must round-trip through the cache** —
    #: see the note on ``to_cache_dict``/``from_cache_dict`` below.
    page: int | None = None
    #: Heading breadcrumb a document chunk falls under (``["Chapter 2", "2.1
    #: Scope"]``), empty for anything that is not a document chunk.
    section_path: list[str] = field(default_factory=list)
    #: Character offsets into the parsed document's full text. ``None`` for
    #: anything that is not a document chunk.
    char_start: int | None = None
    char_end: int | None = None
    #: True once ``chat/context_expansion.py`` has widened this chunk's own
    #: ``start_time``/``end_time``/``content`` to its surrounding exchange
    #: (issue #523). Never set at construction time — only
    #: ``context_expansion._widen_from_segments`` ever writes it — and
    #: deliberately excluded from :meth:`to_cache_dict`/:meth:`from_cache_dict`:
    #: expansion is a READ-TIME step that runs strictly after a retrieval
    #: result is fetched (cached or not), never before, so a cached hit always
    #: round-trips as unexpanded and gets the chance to expand fresh on this
    #: read (issue #526). ``citations.build_citation`` surfaces it as
    #: ``expanded`` so a citation naming a widened span can be told apart from
    #: one naming exactly its own indexed chunk.
    expanded: bool = False

    @property
    def is_digest(self) -> bool:
        return self.digest_section is not None

    @property
    def is_document(self) -> bool:
        return self.source_kind == "document"

    def to_cache_dict(self) -> dict[str, Any]:
        """Serialize for the Redis retrieval cache.

        ⚠️ **``page``/``section_path``/``char_start``/``char_end`` MUST stay
        here and in :meth:`from_cache_dict`, in lockstep with the ``_source``
        allowlist in :func:`_build_body`.** Drop any one of the three and a
        document citation renders correctly on a cache MISS (fresh from
        OpenSearch) and silently loses its page/section on a cache HIT — an
        intermittent bug that looks like a frontend rendering defect and isn't.
        """
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
            "source_kind": self.source_kind,
            "page": self.page,
            "section_path": self.section_path,
            "char_start": self.char_start,
            "char_end": self.char_end,
        }

    @classmethod
    def from_cache_dict(cls, raw: dict[str, Any]) -> ChunkHit:
        section = raw.get("digest_section")
        page = raw.get("page")
        char_start = raw.get("char_start")
        char_end = raw.get("char_end")
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
            source_kind=str(raw.get("source_kind") or "media"),
            page=None if page is None else int(page),
            section_path=list(raw.get("section_path") or []),
            char_start=None if char_start is None else int(char_start),
            char_end=None if char_end is None else int(char_end),
        )


def dynamic_rrf_window(size: int) -> int:
    """Pick an RRF rank window proportional to how much we actually want back.

    The search UI always fuses over ``SEARCH_RRF_WINDOW_SIZE`` (500) because it
    paginates deep. Chat asks for a few dozen chunks, so a full-size window is
    wasted work — scale with the request and clamp to sane bounds.
    """
    return max(_RRF_WINDOW_MIN, min(size * 4, settings.SEARCH_RRF_WINDOW_SIZE))


def _hit_to_chunk(hit: dict[str, Any]) -> ChunkHit | None:
    from app.services.ingest_artifacts.index_mapping import DOC_TYPE_DOCUMENT_CHUNK

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
        language=str(source.get("language") or ""),
        source_kind="document" if source.get("doc_type") == DOC_TYPE_DOCUMENT_CHUNK else "media",
        page=None if source.get("page") is None else int(source["page"]),
        section_path=list(source.get("section_path") or []),
        char_start=None if source.get("char_start") is None else int(source["char_start"]),
        char_end=None if source.get("char_end") is None else int(source["char_end"]),
    )


def _build_body(
    query: str,
    filters: list[dict[str, Any]],
    size: int,
    use_neural: bool,
    model_id: str | None,
    service: HybridSearchService,
    search_mode: str = "hybrid",
    text_fields: list[str] | None = None,
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

    Args:
        text_fields: The BM25 field list for the text leg. ``None`` (the default)
            queries :data:`_DEFAULT_TEXT_FIELDS` — this module's historical,
            unboosted field list — so an ordinary caller's query body is
            unchanged. A non-``None`` value (see :func:`resolve_text_field_preset`)
            overrides it entirely; it is never merged with the default.
    """
    text_query = service._build_text_query(query, text_fields or list(_DEFAULT_TEXT_FIELDS))
    source_fields = [
        "file_id",
        "file_uuid",
        "chunk_index",
        "content",
        "title",
        "speaker",
        "start_time",
        "end_time",
        "language",
        "doc_type",
        # Document-chunk fields (issue #463). Absent on a transcript chunk/digest
        # hit, so they read back as None there — see the ChunkHit fields' own
        # docstrings for why dropping any of these here (or from the cache
        # round-trip) is the specific intermittent-render trap this allowlist
        # exists to close.
        "page",
        "section_path",
        "char_start",
        "char_end",
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


def _widen_to_document_plane(
    filters: list[dict[str, Any]], speakers: list[str] | None
) -> list[dict[str, Any]]:
    """OR the document-chunk plane into the chunk plane :func:`_build_filters` built.

    Document chunks are meant to **join** the same retrieval leg transcript
    chunks use, never replace or fuse-rank against it as a second query — one
    ``bool``/``should`` in place of the single ``chunk_plane_clause()`` entry
    ``HybridSearchService._build_filters`` always appends.

    ⚠️ **Speaker-filtered turns must never widen.** A document has no ``speaker``
    field, so a speaker-scoped question ("what did Dana say about X") that
    included the document plane would silently dilute an attributable answer
    with unattributable text — worse than not finding it, because nothing marks
    it as unattributable. Guarding this here, rather than trusting every caller
    to remember, is the same defense-in-depth ``retrieve_digests`` uses by
    simply having no ``speakers`` parameter at all; this function can't offer
    that (transcript chunks and the speaker filter both live on this leg), so
    it checks instead.

    Args:
        filters: The filter list ``HybridSearchService._build_filters`` returned
            — must still contain its ``chunk_plane_clause()`` entry unchanged.
        speakers: The caller's speaker filter, if any.

    Returns:
        ``filters`` unchanged when ``speakers`` is truthy; otherwise a new list
        with the chunk-plane entry replaced by an OR of it and
        ``document_chunk_plane_clause()``.
    """
    if speakers:
        return filters
    from app.services.ingest_artifacts.index_mapping import chunk_plane_clause
    from app.services.ingest_artifacts.index_mapping import document_chunk_plane_clause

    chunk_clause = chunk_plane_clause()
    widened = {
        "bool": {
            "should": [chunk_clause, document_chunk_plane_clause()],
            "minimum_should_match": 1,
        }
    }
    return [widened if f == chunk_clause else f for f in filters]


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
    diagnostics: dict[str, Any] | None = None,
    text_fields: list[str] | None = None,
) -> list[ChunkHit]:
    """Retrieve the best-matching transcript chunks for ``query``.

    **Also retrieves document chunks** (issue #463), joined onto the SAME leg
    as transcript chunks — never a second, separately-ranked query — via
    :func:`_widen_to_document_plane`. Downstream (masking, citations) already
    dispatches on ``ChunkHit.source_kind``/``is_document``; nothing about that
    plumbing changes here. Suppressed automatically when ``speakers`` is set.

    Args:
        query: The (possibly rewritten) user question.
        user_id: Caller — enforced via the ``accessible_user_ids`` term.
        organization_id: Active tenant, or None for personal scope.
        file_uuids: Resolved scope. ``None`` means every accessible transcript;
            an empty list means nothing matches (a scope that resolved to no files).
        speakers: Restrict to chunks spoken by these display names. Exact, because
            chunks are speaker turns — one chunk is one person talking. Also
            gates document-chunk inclusion (see below) — a document has no
            speaker, so any non-empty ``speakers`` excludes the document plane
            entirely rather than returning unattributable hits.
        size: Candidate pool size to return before reranking.
        search_mode: ``hybrid`` (BM25 + vector), ``semantic``, or ``keyword``.
        fusion: Hybrid fusion strategy for **this call** (#363). None uses the
            configured default. Chat fuses over ``dynamic_rrf_window(size)``
            while the search UI always fuses over 500, so an A/B here does not
            characterise ``/api/search`` and vice versa — measure both.
        diagnostics: Optional out-param. On any retrieval FAILURE (no client
            configured, or the search itself raising) this is set to
            ``{"retrieval_failed": True}`` so a caller holding an empty list can
            tell "the search backend was down" from "nothing matched" (issue
            #438's open half — the `no_context` warning could not yet say
            which). Left untouched — not even set to ``False`` — on every path
            that legitimately found nothing (blank query, an empty resolved
            scope, or a search that genuinely returned zero hits), so its
            ABSENCE is itself the "ordinary empty result" signal.
        text_fields: BM25 field override for the #506 no-stemmed-leg arm — see
            :func:`resolve_text_field_preset`. ``None`` (the default) queries
            this module's historical field list, unchanged. ⚠️ A caller that
            wraps this function in a response cache (chat's
            ``retrieval_cache.cache_key``) MUST fold the resolved preset into
            its cache key, or two arms silently share one cached result.

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
        if diagnostics is not None:
            diagnostics["retrieval_failed"] = True
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
    filters = _widen_to_document_plane(filters, speakers)

    _, use_neural, use_neural_query = service._generate_query_embedding(clean, search_mode)
    model_id = service._get_neural_model_id() if use_neural_query else None
    body = _build_body(
        clean,
        filters,
        size,
        use_neural_query,
        model_id,
        service,
        search_mode,
        text_fields=text_fields,
    )

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
        if diagnostics is not None:
            diagnostics["retrieval_failed"] = True
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
        language=str(source.get("language") or ""),
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
    text_fields: list[str] | None = None,
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
        text_fields: Same #506 BM25 field override as :func:`retrieve_chunks` —
            see that parameter's docstring, including the response-cache warning.

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
    body = _build_body(
        clean,
        filters,
        size,
        use_neural_query,
        model_id,
        service,
        search_mode,
        text_fields=text_fields,
    )
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


# --------------------------------------------------------------------------- #
# Document search — the /api/search?result_type=documents|all leg (issue #463).
#
# Carved out to document_search.py (this repo's ~300-line file-length guideline): the
# dataclasses (DocumentChunkMatch/DocumentSearchHit/DocumentSearchResult) and
# search_document_chunks itself now live there. Re-exported below so every
# existing `from app.services.search.chunk_retrieval import search_document_chunks`
# (the API endpoint, several test modules) keeps working unchanged — a pure
# move, not an API change. Import placement is load-bearing: this sits at the
# BOTTOM of the file, after _build_body/get_opensearch_client are already
# defined/bound above, which is what lets document_search.py's own (deliberately
# LOCAL, see its module docstring) `from .chunk_retrieval import _build_body`
# resolve without a load-time circular-import failure.
# --------------------------------------------------------------------------- #
from app.services.search.document_search import DocumentChunkMatch  # noqa: E402
from app.services.search.document_search import DocumentSearchHit  # noqa: E402
from app.services.search.document_search import DocumentSearchResult  # noqa: E402
from app.services.search.document_search import search_document_chunks  # noqa: E402

__all__ = [
    "ChunkHit",
    "DocumentChunkMatch",
    "DocumentSearchHit",
    "DocumentSearchResult",
    "diversity_sample",
    "dynamic_rrf_window",
    "resolve_text_field_preset",
    "retrieve_chunks",
    "retrieve_digests",
    "search_document_chunks",
]
