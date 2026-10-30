"""The collapsed transcript search: execute, fall back, backfill starved groups, page."""

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from app.core.config import settings
from app.services.search.collapse_bodies import _build_collapsed_bm25_body
from app.services.search.result_processing import _process_collapsed_results
from app.services.search.search_types import SearchHit
from app.services.search.search_types import SearchResponse
from app.services.search.split_bm25 import _execute_split_bm25_collapse

if TYPE_CHECKING:  # pragma: no cover - import cost paid only by type checkers
    from app.services.search.hybrid_search_service import HybridSearchService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _BackfillHighlights:
    """What highlighting the backfilled groups on a hybrid page needs (#1079)."""

    response: dict[str, Any]
    body: dict[str, Any]
    highlight: dict[str, Any]
    file_uuids: frozenset[str]


def _backfill_starved_groups(
    client: Any,
    grouped: list["SearchHit"],
    search_query: str,
    filters: list[dict[str, Any]],
    page: int,
    page_size: int,
    has_speaker_filter: bool,
    sort_by: str,
    sort_order: str,
    query: str,
    sources: frozenset[str] | None = None,
) -> tuple[list["SearchHit"], "_BackfillHighlights | None"]:
    """Backfill file groups starved out of the hybrid RRF rank window.

    Runs the plain BM25 collapse query (immune to window starvation) and
    appends any file groups the hybrid pass missed. Backfilled hits get
    relevance scores rescaled strictly below the lowest hybrid score —
    BM25 raw scores live on a different scale than RRF scores and must
    never outrank the hybrid-ranked results.

    The BM25 query runs split (``_execute_split_bm25_collapse``, #1064), so
    backfilled groups arrive WITHOUT highlights. The second return value is
    what ``_hydrate_page_highlights`` needs to highlight the ones that land
    on the displayed page (None when nothing was backfilled).

    Best-effort: any failure returns the hybrid groups unchanged.
    """
    try:
        bm25_body = _build_collapsed_bm25_body(
            search_query,
            filters,
            page,
            page_size,
            has_speaker_filter,
            sort_by=sort_by,
            sort_order=sort_order,
            sources=sources,
        )
        bm25_response, highlight = _execute_split_bm25_collapse(client, bm25_body)
    except Exception as e:
        logger.warning(f"BM25 group backfill failed for query='{query}': {e}")
        return grouped, None

    bm25_grouped, _ = _process_collapsed_results(
        bm25_response,
        query,
        is_fused_rrf=False,
        sources=sources,
        assume_keyword_match=True,
    )
    seen = {hit.file_uuid for hit in grouped}
    new_hits = [hit for hit in bm25_grouped if hit.file_uuid not in seen]
    if not new_hits:
        return grouped, None

    # Rescale below the hybrid floor, preserving BM25 relative order
    floor = min((hit.relevance_score for hit in grouped), default=0.0)
    for i, hit in enumerate(new_hits):
        hit.relevance_score = floor - (i + 1) * 1e-6

    logger.info(
        f"Backfilled {len(new_hits)} file groups starved from the hybrid "
        f"window for query='{query}' (hybrid returned {len(grouped)})"
    )
    return grouped + new_hits, _BackfillHighlights(
        response=bm25_response,
        body=bm25_body,
        highlight=highlight,
        file_uuids=frozenset(hit.file_uuid for hit in new_hits),
    )


def _search_with_collapse(
    service: "HybridSearchService",
    query: str,
    search_query: str,
    filters: list[dict[str, Any]],
    page: int,
    page_size: int,
    sort_by: str,
    sort_order: str,
    search_mode: str,
    filters_applied: dict[str, Any],
    start_time: float,
    has_speaker_filter: bool,
    use_neural: bool,
    search_pipeline: str,
    sources: frozenset[str] | None = None,
) -> SearchResponse:
    """Execute search using native collapse + inner_hits.

    High-level orchestrator: builds collapsed query, executes it, processes
    results, applies semantic suppression, and returns SearchResponse.

    For non-relevance sorts, OpenSearch handles sorting and pagination
    server-side via native `sort` and `from` parameters. For relevance
    sorts, results are over-fetched and paginated client-side.

    Args:
        query: Original query (for display/caching).
        search_query: Cleaned query (operators removed).
        filters: OpenSearch filter clauses.
        page: Page number (1-indexed).
        page_size: Results per page.
        sort_by: Sort field.
        sort_order: Sort direction.
        search_mode: Search mode string.
        filters_applied: Filter metadata for response.
        start_time: Timestamp for elapsed time calculation.
        has_speaker_filter: Whether a speaker filter is active.
        use_neural: Whether to use neural query.
        search_pipeline: The fusion pipeline id resolved for this request
            (#363), attached only when the body actually has two legs to
            fuse.

    Returns:
        SearchResponse with grouped results.
    """
    # For non-relevance sorts with hybrid mode: use two-phase approach to
    # avoid the over-fetch cap and ensure ALL matching files are sorted.
    if sort_by != "relevance" and use_neural:
        return service._search_with_two_phase(
            query=query,
            search_query=search_query,
            filters=filters,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
            search_mode=search_mode,
            filters_applied=filters_applied,
            start_time=start_time,
            has_speaker_filter=has_speaker_filter,
            search_pipeline=search_pipeline,
            sources=sources,
        )

    client = service._client()
    if not client:
        return service._empty_response(query, page, page_size)

    # Build collapsed search body
    t_build = time.time()
    search_body, needs_search_pipeline = service._build_collapsed_search_body(
        search_query,
        filters,
        page,
        page_size,
        has_speaker_filter,
        use_neural,
        sort_by,
        sort_order,
        sources=sources,
    )
    build_ms = round((time.time() - t_build) * 1000)

    # Execute with search pipeline if using hybrid. NOT simply `use_neural`
    # (issue #606): a fully keyword-starved query is routed to a pure
    # neural-only body with no `hybrid` wrapper to fuse, so no pipeline is
    # attached for it either — see `_build_collapsed_search_body`.
    t_opensearch = time.time()
    response: dict[str, Any] | None = None
    fell_back_to_bm25 = False
    # A keyword-only body is executed split (issue #1064) — see
    # `_execute_split_bm25_collapse`. Not for an empty query: that body is a
    # `match_all` browse, whose hits are NOT keyword matches.
    split_bm25 = not use_neural and bool(search_query and search_query.strip())
    page_highlight: dict[str, Any] = {}
    try:
        if not client:
            return service._empty_response(query, page, page_size)
        search_params: dict[str, Any] = {}
        if needs_search_pipeline:
            search_params["search_pipeline"] = search_pipeline
        if split_bm25:
            response, page_highlight = service._execute_split_bm25_collapse(client, search_body)
        else:
            response = client.search(
                index=settings.OPENSEARCH_CHUNKS_INDEX,
                body=search_body,
                params=search_params,
            )
    except Exception as e:
        if use_neural:
            # Retry once before falling back — transient errors are common
            logger.warning(f"Hybrid search failed (attempt 1), retrying: {e}")
            try:
                response = client.search(
                    index=settings.OPENSEARCH_CHUNKS_INDEX,
                    body=search_body,
                    params=search_params,
                )
            except Exception:
                # Retry failed — fall back to BM25-only so users
                # still get results instead of an empty page.
                logger.warning(
                    "Hybrid search retry failed, falling back to BM25 for query='%s'",
                    query,
                )
                fell_back_to_bm25 = True
                try:
                    fallback_body = service._build_collapsed_bm25_body(
                        search_query,
                        filters,
                        page,
                        page_size,
                        has_speaker_filter,
                        sort_by=sort_by,
                        sort_order=sort_order,
                        sources=sources,
                    )
                    response = client.search(
                        index=settings.OPENSEARCH_CHUNKS_INDEX,
                        body=fallback_body,
                    )
                except Exception as e3:
                    logger.error(f"BM25 fallback also failed: {e3}")
                    return service._empty_response(query, page, page_size)
        else:
            logger.error(f"Collapsed search failed: {e}")
            return service._empty_response(query, page, page_size)

    opensearch_ms = round((time.time() - t_opensearch) * 1000)

    if response is None:
        return service._empty_response(query, page, page_size)

    # Process collapsed results. `needs_search_pipeline` is True only for
    # the fused `hybrid` body (an RRF search pipeline was attached); the
    # `_bm25_leg_is_starved` arm returns a neural-only body with no
    # pipeline (raw cosinesimil scores), and a retry fallback to BM25
    # after a hybrid failure produces plain BM25 scores despite
    # `needs_search_pipeline` having been True for the original attempt.
    t_process = time.time()
    is_fused_rrf = needs_search_pipeline and not fell_back_to_bm25
    grouped, total_files_est = service._process_collapsed_results(
        response,
        query,
        is_fused_rrf=is_fused_rrf,
        sources=sources,
        assume_keyword_match=split_bm25,
    )
    process_ms = round((time.time() - t_process) * 1000)

    # Group-starvation backfill: with hybrid+RRF, collapse can only return
    # files present in the rank window. A query that densely matches ONE
    # file's chunks (e.g. a speaker name hitting every chunk's speaker^3
    # metadata on a labeled file) fills the window with that single file
    # and starves every other group — "Joe Rogan" returned 1 file from a
    # 2,500-file library. Backfill missing groups from the BM25 collapse
    # query (which discovers groups normally); hybrid-ranked hits keep their
    # positions, backfilled keyword groups rank strictly below them.
    #
    # ALWAYS, not only when the hybrid pass returned less than a page (the
    # old condition, issue #1079): the window is ~200 CHUNKS, so a corpus
    # whose files each match in several chunks yields ~40 groups — more than
    # a page, so no backfill ran, and every other keyword-matching file was
    # unreachable on any page (43 of 165 measured).
    backfill: _BackfillHighlights | None = None
    if use_neural and not fell_back_to_bm25 and search_query:
        grouped, backfill = service._backfill_starved_groups(
            client=client,
            grouped=grouped,
            search_query=search_query,
            filters=filters,
            page=page,
            page_size=page_size,
            has_speaker_filter=has_speaker_filter,
            sort_by=sort_by,
            sort_order=sort_order,
            query=query,
            sources=sources,
        )

    service._apply_semantic_demotion(grouped)

    # Sort and paginate — always client-side for consistent behavior.
    # Hybrid/RRF over-fetches results (sort clause omitted to avoid pipeline
    # incompatibility), and BM25-only server-side sort may not cover all files
    # after semantic suppression. Unified path keeps logic simple.
    t_sort = time.time()
    result = service._sort_and_paginate(
        query,
        grouped,
        sort_by,
        sort_order,
        search_mode,
        page,
        page_size,
        filters_applied,
        start_time,
    )
    result.total_files = len(grouped)
    result.total_pages = max(1, (result.total_files + page_size - 1) // page_size)
    sort_ms = round((time.time() - t_sort) * 1000)

    t_highlight = time.time()
    if split_bm25:
        service._hydrate_page_highlights(
            client, result.results, response, search_body, page_highlight, query, sources
        )
    if backfill is not None:
        service._hydrate_page_highlights(
            client,
            [h for h in result.results if h.file_uuid in backfill.file_uuids],
            backfill.response,
            backfill.body,
            backfill.highlight,
            query,
            sources,
        )

    # Deferred semantic highlighting for current page
    service._apply_semantic_highlights(result.results, query)
    highlight_ms = round((time.time() - t_highlight) * 1000)

    total_ms = round((time.time() - start_time) * 1000)
    logger.info(
        f"COLLAPSE SEARCH TIMING: build={build_ms}ms opensearch={opensearch_ms}ms "
        f"process={process_ms}ms highlighting={highlight_ms}ms sort={sort_ms}ms "
        f"total={total_ms}ms files={len(grouped)} query='{query}'"
    )

    if fell_back_to_bm25:
        result._fell_back_to_bm25 = True

    return result
