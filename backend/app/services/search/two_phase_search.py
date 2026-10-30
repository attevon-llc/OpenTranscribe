"""Two-phase search for non-relevance sorts in hybrid mode (hybrid discovery, BM25 page)."""

import logging
import time
from typing import TYPE_CHECKING
from typing import Any

from app.core.config import settings
from app.core.constants import SEARCH_MAX_SNIPPETS_PER_FILE
from app.services.search.result_processing import _process_inner_hits
from app.services.search.search_types import SearchHit
from app.services.search.search_types import SearchResponse

if TYPE_CHECKING:  # pragma: no cover - import cost paid only by type checkers
    from app.services.search.hybrid_search_service import HybridSearchService

logger = logging.getLogger(__name__)


def _bucket_metadata(bucket: dict[str, Any]) -> dict[str, Any]:
    """Extract file metadata from a Phase 1 terms-aggregation bucket.

    Defined as a static method (not a closure) to avoid B023 lint errors
    and to ensure the bucket reference is properly bound.

    Args:
        bucket: Single bucket from the 'by_file' terms aggregation.

    Returns:
        Dict with title, language, content_type, speakers, tags,
        duration, file_size, upload_time, file_id.
    """

    def _first(agg: str) -> str:
        kw_b = bucket.get(agg, {}).get("buckets", [])
        return str(kw_b[0].get("key", "")) if kw_b else ""

    def _all(agg: str) -> list[str]:
        return [str(b.get("key", "")) for b in bucket.get(agg, {}).get("buckets", [])]

    return {
        "title": _first("title_kw"),
        "language": _first("language_kw"),
        "content_type": _first("content_type_kw"),
        "speakers": _all("speakers_kw"),
        "tags": _all("tags_kw"),
        "duration": float(bucket.get("max_duration", {}).get("value") or 0.0),
        "file_size": int(bucket.get("max_file_size", {}).get("value") or 0),
        "upload_time": str(bucket.get("max_upload_time", {}).get("value_as_string") or ""),
        "file_id": int(bucket.get("min_file_id", {}).get("value") or 0),
    }


def _phase2_lookup(
    phase2_resp: dict[str, Any],
    query: str,
    sources: frozenset[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Build a file_uuid → hit-details lookup from a Phase 2 BM25 response.

    Args:
        phase2_resp: OpenSearch collapse query response.
        query: Original query string (for inner-hit processing).

    Returns:
        Dict mapping file_uuid to occurrence/highlight details.
    """
    lookup: dict[str, dict[str, Any]] = {}
    for outer_hit in phase2_resp.get("hits", {}).get("hits", []):
        src = outer_hit.get("_source", {})
        fid = src.get("file_uuid", "")
        if not fid:
            continue
        inner_data = outer_hit.get("inner_hits", {}).get("segments", {}).get("hits", {})
        occs, title_hl, msrcs, kw_cnt, sem_cnt, score = _process_inner_hits(
            inner_data.get("hits", []), 1.0, query, src.get("language"), sources
        )
        lookup[fid] = {
            "occurrences": occs,
            "title_highlighted": title_hl,
            "match_sources": msrcs,
            "keyword_count": kw_cnt,
            "semantic_count": sem_cnt,
            "best_score": score,
        }
    return lookup


def _sort_buckets(
    sort_by: str,
    sort_order: str,
    buckets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Sort aggregation buckets by the requested metadata field.

    Args:
        sort_by: Field to sort by (duration, file_size, upload_time, filename).
        sort_order: 'asc' or 'desc'.
        buckets: Raw aggregation bucket list from Phase 1.

    Returns:
        Sorted list of buckets.
    """

    def _key(b: dict[str, Any]) -> Any:
        if sort_by == "duration":
            return b.get("max_duration", {}).get("value") or 0.0
        if sort_by == "file_size":
            return b.get("max_file_size", {}).get("value") or 0
        if sort_by in ("upload_time", "completed_at"):
            return b.get("max_upload_time", {}).get("value_as_string") or ""
        if sort_by == "filename":
            kw_b = b.get("title_kw", {}).get("buckets", [])
            return (kw_b[0].get("key") or "").lower() if kw_b else ""
        return 0

    return sorted(buckets, key=_key, reverse=sort_order != "asc")


def _build_search_hit_from_bucket(
    bucket: dict[str, Any],
    p2: dict[str, Any] | None,
    query_lower: str,
    sources: frozenset[str] | None = None,
) -> SearchHit | None:
    """Merge Phase 1 metadata and Phase 2 highlights into a SearchHit.

    Args:
        bucket: Phase 1 aggregation bucket for one file.
        p2: Phase 2 lookup entry for the same file (may be None).
        query_lower: Lower-cased query for speaker-match detection.

    Returns:
        SearchHit, or None if the file has no usable occurrences.
    """
    meta = _bucket_metadata(bucket)
    file_uuid = bucket["key"]
    title = meta["title"]

    if not p2 or not p2["occurrences"]:
        return None

    occurrences = p2["occurrences"]
    title_highlighted = p2["title_highlighted"] or title
    match_sources: list[str] = list(p2["match_sources"])
    keyword_count: int = p2["keyword_count"]
    semantic_count: int = p2["semantic_count"]
    best_score: float = p2["best_score"]

    speakers: list[str] = meta["speakers"]
    if query_lower and (sources is None or "speaker" in sources):
        for speaker_name in speakers:
            ql_in_sp = query_lower in speaker_name.lower()
            sp_in_ql = speaker_name.lower() in query_lower
            if (ql_in_sp or sp_in_ql) and "metadata_speaker" not in match_sources:
                match_sources.append("metadata_speaker")
                break

    is_semantic_only = keyword_count == 0
    # No confidence badge on this path: Phase 2 scores are raw BM25
    # (unbounded, ~1-30), not the RRF score `SEARCH_SEMANTIC_HIGH_CONFIDENCE`
    # was tuned against. Labelling here would read "high" for nearly every
    # result — see issue #698. An absent badge beats a meaningless one.
    semantic_confidence = ""
    if is_semantic_only and "semantic" not in match_sources:
        match_sources.append("semantic")

    has_both = keyword_count > 0 and semantic_count > 0
    total_occurrences = max(len(occurrences), keyword_count + semantic_count)

    return SearchHit(
        file_uuid=file_uuid,
        file_id=meta["file_id"],
        title=title,
        speakers=speakers,
        tags=meta["tags"],
        upload_time=meta["upload_time"],
        language=meta["language"],
        content_type=meta["content_type"],
        relevance_score=best_score,
        occurrences=occurrences,
        total_occurrences=total_occurrences,
        title_highlighted=title_highlighted,
        keyword_occurrences=keyword_count,
        semantic_only=is_semantic_only,
        semantic_confidence=semantic_confidence,
        match_sources=match_sources,
        duration=meta["duration"],
        file_size=meta["file_size"],
        semantic_occurrences=semantic_count,
        has_both_match_types=has_both,
    )


def _search_with_two_phase(
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
    search_pipeline: str,
    sources: frozenset[str] | None = None,
) -> SearchResponse:
    """Two-phase search for non-relevance sorts with hybrid mode.

    Phase 1: Hybrid aggregation to discover ALL matching file UUIDs with
    their metadata. Eliminates the 200-file cap for metadata-field sorts.

    Phase 2: BM25 collapse on current page's file UUIDs to get proper
    snippet highlights and occurrence details.

    This is the industry-standard pattern used by large-scale search
    systems when combining semantic ranking with metadata-field sorting.

    Args:
        query: Original query (for display/caching).
        search_query: Cleaned query (operators removed).
        filters: OpenSearch filter clauses.
        page: Page number (1-indexed).
        page_size: Results per page.
        sort_by: Non-relevance sort field.
        sort_order: Sort direction.
        search_mode: Search mode string.
        filters_applied: Filter metadata for response.
        start_time: Timestamp for elapsed time.
        has_speaker_filter: Whether a speaker filter is active.
        search_pipeline: The fusion pipeline id resolved for this request
            (#363). Passed down rather than re-read from settings so an A/B
            arm cannot lose its strategy on the way to the wire.

    Returns:
        SearchResponse with correctly sorted and paginated results.
    """
    client = service._client()
    if not client:
        return service._empty_response(query, page, page_size)

    model_id = service._get_neural_model_id()
    if not model_id:
        # Fall back to single-phase BM25
        return service._search_with_collapse(
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
            use_neural=False,
            search_pipeline=search_pipeline,
            sources=sources,
        )

    search_fields = service._get_search_fields(has_speaker_filter, sources=sources)
    text_query_clause = service._build_text_query(search_query, search_fields)

    # ── Phase 1: Hybrid file discovery ──────────────────────────────────
    # OpenSearch 3.4 bug: aggs + hybrid + RRF pipeline triggers
    # ArrayIndexOutOfBoundsException.  Use a collapse-based approach
    # instead: fetch all matching file_uuids via collapse (no inner_hits,
    # lightweight), then extract metadata in Phase 2.
    t_p1 = time.time()
    phase1_body: dict[str, Any] = {
        "query": {
            "hybrid": {
                "queries": [
                    {
                        "bool": {
                            "must": [text_query_clause],
                            "filter": filters,
                        }
                    },
                    {
                        "bool": {
                            "must": [
                                {
                                    "neural": {
                                        "embedding": {
                                            "query_text": search_query or query,
                                            "model_id": model_id,
                                            "k": settings.SEARCH_RRF_WINDOW_SIZE,
                                        }
                                    }
                                }
                            ],
                            "filter": filters,
                        }
                    },
                ]
            }
        },
        "size": 5000,
        "track_total_hits": False,
        "_source": [
            "file_uuid",
            "file_id",
            "title",
            "speakers",
            "tags",
            "upload_time",
            "language",
            "content_type",
            "duration",
            "file_size",
        ],
        "collapse": {"field": "file_uuid"},
    }

    try:
        phase1_resp = client.search(
            index=settings.OPENSEARCH_CHUNKS_INDEX,
            body=phase1_body,
            params={"search_pipeline": search_pipeline},
        )
    except Exception as e:
        logger.warning(f"Two-phase Phase 1 failed, falling back to single-phase: {e}")
        return service._search_with_collapse(
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
            use_neural=False,
            search_pipeline=search_pipeline,
        )

    p1_ms = round((time.time() - t_p1) * 1000)

    # Convert collapsed hits to pseudo-bucket format for _sort_buckets
    # and _bucket_metadata compatibility.
    collapsed_hits = phase1_resp.get("hits", {}).get("hits", [])
    buckets = []
    for hit in collapsed_hits:
        src = hit.get("_source", {})
        fuid = src.get("file_uuid", "")
        if not fuid:
            continue
        buckets.append(
            {
                "key": fuid,
                "title_kw": {
                    "buckets": [{"key": src.get("title", "")}] if src.get("title") else []
                },
                "language_kw": {
                    "buckets": [{"key": src.get("language", "")}] if src.get("language") else []
                },
                "content_type_kw": {
                    "buckets": [{"key": src.get("content_type", "")}]
                    if src.get("content_type")
                    else []
                },
                "speakers_kw": {"buckets": [{"key": s} for s in (src.get("speakers") or [])]},
                "tags_kw": {"buckets": [{"key": t} for t in (src.get("tags") or [])]},
                "max_duration": {"value": src.get("duration") or 0.0},
                "max_file_size": {"value": src.get("file_size") or 0},
                "max_upload_time": {"value_as_string": src.get("upload_time") or ""},
                "min_file_id": {"value": src.get("file_id") or 0},
            }
        )
    if not buckets:
        return service._sort_and_paginate(
            query,
            [],
            sort_by,
            sort_order,
            search_mode,
            page,
            page_size,
            filters_applied,
            start_time,
        )

    sorted_buckets = service._sort_buckets(sort_by, sort_order, buckets)

    total_files = len(sorted_buckets)
    total_pages = max(1, (total_files + page_size - 1) // page_size)
    start_idx = (page - 1) * page_size
    page_buckets = sorted_buckets[start_idx : start_idx + page_size]
    page_file_uuids = [b["key"] for b in page_buckets]

    if not page_file_uuids:
        elapsed_ms = round((time.time() - start_time) * 1000, 1)
        return SearchResponse(
            query=query,
            results=[],
            total_results=0,
            total_files=total_files,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            search_time_ms=elapsed_ms,
            filters_applied=filters_applied,
            search_mode=search_mode,
        )

    # ── Phase 2: BM25 collapse on page file UUIDs ────────────────────────
    # Fetch highlighted snippets for just the current page's files.
    t_p2 = time.time()
    highlight_fields = service._build_highlight_fields(
        has_speaker_filter, use_exact=True, sources=sources
    )
    bm25_fields = service._get_search_fields(has_speaker_filter, use_exact=True, sources=sources)
    p2_text_query = service._build_text_query(search_query, bm25_fields)

    phase2_body: dict[str, Any] = {
        "query": {
            "bool": {
                "must": [p2_text_query],
                "filter": [{"terms": {"file_uuid": page_file_uuids}}],
            }
        },
        "collapse": {
            "field": "file_uuid",
            "inner_hits": {
                "name": "segments",
                "size": SEARCH_MAX_SNIPPETS_PER_FILE,
                "sort": [{"_score": {"order": "desc"}}],
                "highlight": {"fields": highlight_fields},
                "_source": {"excludes": ["embedding"]},
            },
            "max_concurrent_group_searches": settings.SEARCH_COLLAPSE_MAX_CONCURRENT,
        },
        "size": len(page_file_uuids),
        "_source": {"excludes": ["embedding"]},
        "track_total_hits": False,
    }

    try:
        phase2_resp = client.search(
            index=settings.OPENSEARCH_CHUNKS_INDEX,
            body=phase2_body,
        )
    except Exception as e:
        logger.warning(f"Two-phase Phase 2 failed: {e}")
        phase2_resp = {"hits": {"hits": []}}

    p2_ms = round((time.time() - t_p2) * 1000)

    # Build lookup: file_uuid → (occurrences, title_highlighted, match_sources, ...)
    p2_hits_by_uuid = service._phase2_lookup(phase2_resp, query, sources)

    # ── Merge Phase 1 metadata + Phase 2 highlights ──────────────────────
    query_lower = query.lower().strip() if query else ""
    results: list[SearchHit] = []
    for bucket in page_buckets:
        hit = service._build_search_hit_from_bucket(
            bucket, p2_hits_by_uuid.get(bucket["key"]), query_lower, sources
        )
        if hit is not None:
            results.append(hit)

    service._normalize_relevance_percent(results)
    service._apply_semantic_highlights(results, query)

    elapsed_ms = round((time.time() - start_time) * 1000, 1)
    total_results = sum(
        h.keyword_occurrences if h.keyword_occurrences > 0 else h.total_occurrences for h in results
    )

    logger.info(
        f"TWO-PHASE SEARCH: p1={p1_ms}ms p2={p2_ms}ms "
        f"total_files={total_files} page_files={len(results)} sort={sort_by} query='{query}'"
    )

    return SearchResponse(
        query=query,
        results=results,
        total_results=total_results,
        total_files=total_files,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        search_time_ms=elapsed_ms,
        filters_applied=filters_applied,
        search_mode=search_mode,
    )
