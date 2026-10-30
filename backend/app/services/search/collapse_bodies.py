"""Request bodies for the collapsed (one group per file) transcript search.

Covers the hybrid, neural-only (starved keyword leg, #606) and BM25-only bodies, and the
sort/over-fetch rules they share.
"""

import logging
from typing import TYPE_CHECKING
from typing import Any

from app.core.config import settings
from app.core.constants import SEARCH_MAX_SNIPPETS_PER_FILE
from app.services.ingest_artifacts.index_mapping import chunk_plane_clause
from app.services.search.query_building import _build_highlight_fields
from app.services.search.query_building import _build_text_query
from app.services.search.query_building import _get_search_fields

if TYPE_CHECKING:  # pragma: no cover - import cost paid only by type checkers
    from app.services.search.hybrid_search_service import HybridSearchService

logger = logging.getLogger(__name__)


def _apply_sort_clause(
    body: dict[str, Any],
    sort_by: str,
    sort_order: str,
    page: int,
    page_size: int,
    use_search_pipeline: bool = False,
) -> int:
    """Apply sort and pagination to a search body.

    For relevance sorts, OpenSearch's default _score ordering is used and
    pagination is handled client-side via over-fetch. For non-relevance sorts
    with a search pipeline (hybrid/RRF), over-fetch is used because the RRF
    normalization pipeline does not support mixing _score with other sort
    criteria. For BM25-only non-relevance sorts, OpenSearch sorts natively so
    the over-fetch window holds the first files IN THAT ORDER, and the page
    is still cut client-side like every other path. It used to be cut
    server-side too (``from``/``size=page_size``), and the caller then paged
    that one-page response a second time, so page 2+ was always empty and
    ``total_pages`` was always 1 (issue #1078).

    Args:
        body: Search body dict (modified in place).
        sort_by: Sort field name.
        sort_order: Sort direction ("asc" or "desc").
        page: Page number (1-indexed).
        page_size: Results per page.
        use_search_pipeline: If True, a search pipeline (RRF) is active
            and _score cannot be mixed with other sort criteria.

    Returns:
        The outer_size to use for the query.
    """
    # Dynamic over-fetch: scale with page depth for full coverage.
    # Page 1/20 → 200, page 5/20 → 500, capped at SEARCH_MAX_OVERFETCH.
    min_fetch = page * page_size
    over_fetch = min(max(page_size * 10, min_fetch + page_size * 5), settings.SEARCH_MAX_OVERFETCH)
    if sort_by == "relevance" or use_search_pipeline:
        return over_fetch

    # BM25-only: server-side sort with _score as tiebreaker is safe
    sort_map = {
        "upload_time": "upload_time",
        "completed_at": "upload_time",
        "filename": "title.keyword",
        "duration": "duration",
        "file_size": "file_size",
    }
    sort_field = sort_map.get(sort_by, "upload_time")
    body["sort"] = [
        {sort_field: {"order": sort_order}},
        {"_score": {"order": "desc"}},
    ]
    return over_fetch


def _build_collapsed_search_body(
    service: "HybridSearchService",
    query: str,
    filters: list[dict[str, Any]],
    page: int,
    page_size: int,
    has_speaker_filter: bool,
    use_neural: bool,
    sort_by: str = "relevance",
    sort_order: str = "desc",
    sources: frozenset[str] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Build a search body with native collapse + inner_hits.

    OpenSearch groups results by file_uuid server-side, returning only
    the top N groups with their inner segments. This eliminates the need
    to over-fetch thousands of chunks and group them in Python.

    Args:
        query: Search query text.
        filters: OpenSearch filter clauses.
        page: Page number (1-indexed).
        page_size: Results per page (number of collapsed groups).
        has_speaker_filter: Whether a speaker filter is active.
        use_neural: Whether to use neural query (server-side embedding).
        sort_by: Sort field.
        sort_order: Sort direction.

    Returns:
        Tuple of (OpenSearch search body dict with collapse configuration,
        whether the caller must attach the ``search_pipeline`` param — see
        the starvation check below for why this is not simply ``use_neural``).
    """
    search_fields = service._get_search_fields(has_speaker_filter, sources=sources)
    text_query_clause = service._build_text_query(query, search_fields)
    highlight_fields = service._build_highlight_fields(has_speaker_filter, sources=sources)

    # Inner hits: top segments per file group
    inner_hits_config: dict[str, Any] = {
        "name": "segments",
        "size": SEARCH_MAX_SNIPPETS_PER_FILE,
        "sort": [{"_score": {"order": "desc"}}],
        "highlight": {"fields": highlight_fields},
        "_source": {"excludes": ["embedding"]},
    }

    collapse_config: dict[str, Any] = {
        "field": "file_uuid",
        "inner_hits": inner_hits_config,
        "max_concurrent_group_searches": settings.SEARCH_COLLAPSE_MAX_CONCURRENT,
    }

    if use_neural and query and query.strip():
        model_id = service._get_neural_model_id()
        if not model_id:
            logger.warning(
                "Neural model_id lookup returned None during query construction; "
                "falling through to BM25-only for query='%s'",
                query,
            )
        if model_id:
            neural_clause = {
                "bool": {
                    "must": [
                        {
                            "neural": {
                                "embedding": {
                                    "query_text": query,
                                    "model_id": model_id,
                                    "k": settings.SEARCH_RRF_WINDOW_SIZE,
                                }
                            }
                        }
                    ],
                    "filter": filters,
                }
            }

            # A fully-starved keyword leg (zero BM25 matches — the normal
            # case for a genuinely semantic query, see `_build_text_query`)
            # must NOT be handed to the hybrid `search_pipeline` (issue
            # #606). OpenSearch 3.4's `collapse` + hybrid RRF
            # (`score-ranker-processor`) silently returns a WRONG,
            # QUERY-INDEPENDENT ranking whenever one of the two hybrid
            # legs matches nothing — measured directly: two unrelated
            # queries ("space exploration", "artificial intelligence"),
            # both with zero keyword hits, produced byte-identical
            # collapsed scores and file ordering, dominated by a file with
            # no topical relevance to either. The bug is specific to
            # `collapse` — the same starved-leg RRF fusion is correct at
            # the raw chunk level (no collapse), and a plain neural-only
            # collapse query (no `hybrid` wrapper at all) is also correct.
            # The pre-check below is a `count` (no scoring, no fetch), not
            # a second scored `search`, so it stays cheap.
            if service._bm25_leg_is_starved(text_query_clause, filters):
                body = {
                    "size": 0,  # Placeholder — set by _apply_sort_clause
                    "query": neural_clause,
                    "collapse": collapse_config,
                    "highlight": {"fields": highlight_fields},
                    "_source": {"excludes": ["embedding"]},
                    "track_total_hits": False,
                }
                body["size"] = service._apply_sort_clause(
                    body, sort_by, sort_order, page, page_size, use_search_pipeline=False
                )
                return body, False

            body = {
                "size": 0,  # Placeholder — set by _apply_sort_clause
                "query": {
                    "hybrid": {
                        "queries": [
                            {
                                "bool": {
                                    "must": [text_query_clause],
                                    "filter": filters,
                                }
                            },
                            neural_clause,
                        ]
                    }
                },
                "collapse": collapse_config,
                "highlight": {"fields": highlight_fields},
                "_source": {"excludes": ["embedding"]},
                "track_total_hits": False,
                # NOTE: Do NOT add "aggs" here. OpenSearch 3.4 has a bug
                # where cardinality aggregations combined with hybrid query
                # + collapse + RRF search_pipeline triggers an
                # ArrayIndexOutOfBoundsException in score-ranker-processor.
                # total_files is computed from collapsed results instead.
            }
            # The fused window is sized as for page 1 on EVERY page (#1079).
            # Its groups are the head of the result list and the BM25
            # backfill (`_backfill_starved_groups`) is the tail; a window that
            # grew with the page number would pull tail files into the head
            # on deep pages and shift every boundary — measured: 5 files
            # shown twice across 9 pages. Keyword matches past the window
            # stay reachable through the backfill, whose BM25 order is stable
            # as ITS window grows.
            body["size"] = service._apply_sort_clause(
                body, sort_by, sort_order, 1, page_size, use_search_pipeline=True
            )
            return body, True

    # BM25-only collapse
    return (
        service._build_collapsed_bm25_body(
            query,
            filters,
            page,
            page_size,
            has_speaker_filter,
            highlight_fields,
            sort_by,
            sort_order,
        ),
        False,
    )


def _bm25_leg_is_starved(
    service: "HybridSearchService", text_query_clause: dict[str, Any], filters: list[dict[str, Any]]
) -> bool:
    """Whether the BM25/keyword leg matches zero chunk documents.

    A cheap ``count`` (no scoring, no fetch) against the same clause
    ``_build_collapsed_search_body`` would otherwise put in the hybrid
    query's keyword leg. See issue #606: this check exists specifically
    to route around a broken collapse+hybrid-RRF combination when the
    answer is yes.

    Args:
        text_query_clause: The clause `_build_text_query` produced.
        filters: OpenSearch filter clauses (tenant scope, quarantine, etc).
            Callers on this path already include `chunk_plane_clause()`
            (`_build_filters`), but this count explicitly ANDs it in again
            itself rather than trusting that — a reader of this index must
            decide its own plane, not inherit the caller's (#403 Stage 3
            addendum G3/G4; `tests/unit/test_chunk_plane_compat_arm.py`).

    Returns:
        True if the keyword leg would match nothing. Fails open to False
        (i.e. "assume it matches something, use the normal hybrid path")
        on any error — a starvation *false negative* only costs the
        OpenSearch-level bug being observed as narrow scores again as
        already documented (`artificial intelligence`'s prior skip);
        fail-closed here would instead risk a spurious neural-only
        fallback for every query if `count` itself is unhealthy.
    """
    client = service._client()
    if not client:
        return False
    try:
        resp = client.count(
            index=settings.OPENSEARCH_CHUNKS_INDEX,
            body={
                "query": {
                    "bool": {
                        "must": [text_query_clause],
                        "filter": [*filters, chunk_plane_clause()],
                    }
                }
            },
        )
        return int(resp.get("count", 0)) == 0
    except Exception as e:  # noqa: BLE001 — best-effort pre-check, see docstring
        logger.debug(f"BM25 starvation pre-check failed, assuming non-empty: {e}")
        return False


def _build_collapsed_bm25_body(
    query: str,
    filters: list[dict[str, Any]],
    page: int,
    page_size: int,
    has_speaker_filter: bool,
    highlight_fields: dict[str, Any] | None = None,
    sort_by: str = "relevance",
    sort_order: str = "desc",
    sources: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Build a BM25-only search body with native collapse.

    Used when neural search is unavailable but collapse is supported.

    Args:
        query: Search query text.
        filters: OpenSearch filter clauses.
        page: Page number (1-indexed).
        page_size: Results per page.
        has_speaker_filter: Whether a speaker filter is active.
        highlight_fields: Pre-built highlight config (reuses caller's if provided).
        sort_by: Sort field.
        sort_order: Sort direction.

    Returns:
        OpenSearch search body dict.
    """
    search_fields = _get_search_fields(has_speaker_filter, use_exact=True, sources=sources)
    text_query_clause = _build_text_query(query, search_fields)

    # Always rebuild highlight fields with use_exact=True to match BM25
    # search fields (content.exact). OpenSearch's unified highlighter uses
    # require_field_match=true by default, so highlight fields must match
    # the fields being queried.
    bm25_highlight_fields = _build_highlight_fields(
        has_speaker_filter, use_exact=True, sources=sources
    )

    inner_hits_config: dict[str, Any] = {
        "name": "segments",
        "size": SEARCH_MAX_SNIPPETS_PER_FILE,
        "sort": [{"_score": {"order": "desc"}}],
        "highlight": {"fields": bm25_highlight_fields},
        "_source": {"excludes": ["embedding"]},
    }

    collapse_config: dict[str, Any] = {
        "field": "file_uuid",
        "inner_hits": inner_hits_config,
        "max_concurrent_group_searches": settings.SEARCH_COLLAPSE_MAX_CONCURRENT,
    }

    body: dict[str, Any] = {
        "size": 0,  # Placeholder — set by _apply_sort_clause
        "query": {
            "bool": {
                "must": [text_query_clause],
                "filter": filters,
            }
        },
        "collapse": collapse_config,
        "highlight": {"fields": bm25_highlight_fields},
        "_source": {"excludes": ["embedding"]},
        "track_total_hits": False,
        "aggs": {
            "total_files": {"cardinality": {"field": "file_uuid", "precision_threshold": 10000}}
        },
    }
    body["size"] = _apply_sort_clause(body, sort_by, sort_order, page, page_size)
    return body
