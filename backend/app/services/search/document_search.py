"""Document search — the ``GET /api/search?result_type=documents|all`` leg (issue #463).

Carved out of ``chunk_retrieval.py`` (the repo's ~300-line file-size guideline —
``chunk_retrieval.py`` had grown to ~783 lines) — a **pure move**, no behaviour change.
Everything here used to live at the bottom of that
module; ``chunk_retrieval.py`` re-exports every name below so existing callers'
``from app.services.search.chunk_retrieval import search_document_chunks`` (the API
endpoint, several test modules) keep working unchanged.

⚠️ **``_build_body`` and ``get_opensearch_client`` are imported LOCALLY, inside
``search_document_chunks``, not at module level.** ``chunk_retrieval.py`` imports THIS
module (to re-export its names) — a module-level import back the other way would be a
load-time circular import, breakable depending on which module a caller happens to
import first. Deferring the import to call time sidesteps that entirely (both modules
are always fully loaded by the time anything actually calls this function) and, as a
side effect, keeps existing tests that ``patch("app.services.search.chunk_retrieval.
get_opensearch_client", ...)`` working unmodified: the lazy `from ... import
get_opensearch_client` re-reads whatever the patched module attribute currently is.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field
from typing import Any

logger = logging.getLogger(__name__)

#: Chunk-level candidates fetched from OpenSearch before grouping into
#: file-level hits and paginating in Python (below). Scaled by the requested
#: page so a deep page still has enough candidates to reach it, capped so a
#: pathological page_size can't request an unbounded pool.
_DOCUMENT_SEARCH_CANDIDATE_FLOOR = 100
_DOCUMENT_SEARCH_CANDIDATE_CAP = 1000
#: Matching chunks kept per document hit — mirrors the search UI's per-file
#: occurrence cap in spirit; documents don't need the full snippet/highlight
#: machinery ``HybridSearchService`` builds for the transcript leg.
_DOCUMENT_SEARCH_MATCHES_PER_FILE = 3
_DOCUMENT_SNIPPET_CHARS = 300


@dataclass
class DocumentChunkMatch:
    """One matching chunk inside a document hit."""

    chunk_index: int
    page: int | None
    section_path: list[str]
    snippet: str
    score: float


@dataclass
class DocumentSearchHit:
    """A file-level document search result — ``file_id``/``file_uuid`` address
    ``Document.id``/``Document.uuid``, a SEPARATE id space from ``MediaFile``
    (see ``ChunkHit.source_kind``'s docstring). Never merge this with a
    transcript ``SearchHitSchema`` result keyed only by a bare integer id.
    """

    file_uuid: str
    file_id: int
    title: str
    matches: list[DocumentChunkMatch] = field(default_factory=list)


@dataclass
class DocumentSearchResult:
    """A page of document search results."""

    results: list[DocumentSearchHit] = field(default_factory=list)
    total_results: int = 0
    total_files: int = 0


def _document_snippet(text: str) -> str:
    clean = " ".join(text.split())
    if len(clean) <= _DOCUMENT_SNIPPET_CHARS:
        return clean
    cut = clean[:_DOCUMENT_SNIPPET_CHARS]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut + "…"


def search_document_chunks(
    query: str,
    *,
    user_id: int,
    organization_id: int | None = None,
    page: int = 1,
    page_size: int = 20,
    search_mode: str = "hybrid",
) -> DocumentSearchResult:
    """Search the document-chunk plane for ``GET /api/search?result_type=documents``.

    Grouped and paginated at the FILE level, same shape as the summary search
    leg (``services/search/summary_search.py``) and the transcript leg's
    collapsed response — never a bare list of chunk hits, which would show the
    same document several times on one results page.

    **Candidate-window pagination, not a true collapse.** A `collapse` query
    against the RRF-fused hybrid body risks the same
    `ArrayIndexOutOfBoundsException` `hybrid_search_service`'s module docs warn
    about for aggregations on that pipeline shape, so this fetches a bounded
    pool of chunk hits (already relevance-ordered by OpenSearch), groups them by
    ``file_uuid`` in Python preserving first-seen (= best-scoring) order, and
    paginates the resulting file list. ``total_files``/``total_results`` are
    therefore exact **within the fetched candidate window**, not a true
    corpus-wide count — the same trade-off ``dynamic_rrf_window`` documents for
    chat. A page deep enough to exceed the window returns short rather than
    wrong; widen ``_DOCUMENT_SEARCH_CANDIDATE_CAP`` if that is ever observed in
    practice.

    Access control is the same ``accessible_user_ids`` term + tenant gate every
    other plane of this index uses — documents have no separate sharing model
    yet (``index_document_chunks`` stamps ``accessible_user_ids: [owner_id]``
    at index time; ``update_document_access_index`` is the rewrite path a
    future document-sharing lane will drive).

    Args:
        query: Search text.
        user_id: Caller — enforced via ``accessible_user_ids``.
        organization_id: Active tenant, or None for personal scope.
        page: 1-indexed page number, over FILES.
        page_size: Files per page.
        search_mode: ``hybrid`` | ``semantic`` | ``keyword``.

    Returns:
        A page of file-level hits. Empty on any failure — a broken document
        leg must not break the rest of a combined ``result_type=all`` search.
    """
    from app.core.config import settings
    from app.services.ingest_artifacts.index_mapping import document_chunk_plane_clause
    from app.services.search.chunk_retrieval import _build_body
    from app.services.search.chunk_retrieval import get_opensearch_client
    from app.services.search.hybrid_search_service import HybridSearchService
    from app.services.search.hybrid_search_service import ensure_fusion_pipeline
    from app.services.search.tenant_scope import org_filter_clauses

    clean = (query or "").strip()
    if not clean:
        return DocumentSearchResult()

    client = get_opensearch_client()
    if not client:
        logger.warning("Document search unavailable: no OpenSearch client")
        return DocumentSearchResult()

    filters: list[dict[str, Any]] = [{"terms": {"accessible_user_ids": [user_id]}}]
    filters.extend(org_filter_clauses(organization_id))
    filters.append(document_chunk_plane_clause())

    service = HybridSearchService()
    _, _use_neural, use_neural_query = service._generate_query_embedding(clean, search_mode)
    model_id = service._get_neural_model_id() if use_neural_query else None

    candidate_size = min(
        _DOCUMENT_SEARCH_CANDIDATE_CAP,
        max(_DOCUMENT_SEARCH_CANDIDATE_FLOOR, page * page_size * 10),
    )
    body = _build_body(
        clean, filters, candidate_size, use_neural_query, model_id, service, search_mode
    )

    params = {}
    if use_neural_query and model_id and search_mode != "semantic":
        params["search_pipeline"] = ensure_fusion_pipeline(None)

    try:
        response = client.search(
            index=settings.OPENSEARCH_CHUNKS_INDEX, body=body, params=params or None
        )
    except Exception as exc:  # noqa: BLE001 — a failed document leg must not break search
        logger.warning(f"Document search failed: {exc}")
        return DocumentSearchResult()

    hits = response.get("hits", {}).get("hits", [])

    by_file: dict[str, DocumentSearchHit] = {}
    file_order: list[str] = []
    for hit in hits:
        source = hit.get("_source") or {}
        file_uuid = source.get("file_uuid")
        content = source.get("content")
        if not file_uuid or not content:
            continue
        file_uuid = str(file_uuid)
        doc_hit = by_file.get(file_uuid)
        if doc_hit is None:
            doc_hit = DocumentSearchHit(
                file_uuid=file_uuid,
                file_id=int(source.get("file_id") or 0),
                title=str(source.get("title") or ""),
            )
            by_file[file_uuid] = doc_hit
            file_order.append(file_uuid)
        if len(doc_hit.matches) < _DOCUMENT_SEARCH_MATCHES_PER_FILE:
            doc_hit.matches.append(
                DocumentChunkMatch(
                    chunk_index=int(source.get("chunk_index") or 0),
                    page=source.get("page"),
                    section_path=list(source.get("section_path") or []),
                    snippet=_document_snippet(str(content)),
                    score=float(hit.get("_score") or 0.0),
                )
            )

    total_files = len(file_order)
    start = max(0, (page - 1) * page_size)
    page_uuids = file_order[start : start + page_size]
    results = [by_file[u] for u in page_uuids]

    logger.info(
        "Document search: %d candidate chunks -> %d files (mode=%s, neural=%s)",
        len(hits),
        total_files,
        search_mode,
        bool(model_id),
    )
    return DocumentSearchResult(results=results, total_results=len(hits), total_files=total_files)
