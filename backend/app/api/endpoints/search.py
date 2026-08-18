"""Search API endpoints with hybrid BM25 + vector search."""

import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Body
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.auth import get_current_admin_user
from app.core.config import settings
from app.core.constants import OPENSEARCH_EMBEDDING_MODELS
from app.core.constants import SEARCH_DEFAULT_PAGE_SIZE
from app.core.constants import SEARCH_MAX_PAGE_SIZE
from app.core.constants import get_speaker_index
from app.core.constants import get_speaker_index_v4
from app.core.redis import get_redis
from app.db.base import get_db
from app.models.user import User
from app.schemas.search import SetEmbeddingModelSchema
from app.services.ingest_artifacts.index_mapping import chunk_plane_clause

logger = logging.getLogger(__name__)

router = APIRouter()


def _search_response_to_schema(response) -> dict[str, Any]:
    """Convert HybridSearchService response to serializable dict.

    Carries an ``embedding_warning`` when the index is a PROVEN mix of two
    embedding models (#437). Until this, the mixed verdict had three readers —
    the status endpoint, the model-switch response and a beat-task log — and none
    of them is the person reading the results, so a mixed index went on ranking
    two incomparable vector populations against each other in silence. The
    advisory is deployment-level and TTL-cached (see
    ``embedding_provenance.search_provenance_advisory``), so an ordinary search
    pays nothing and the key is absent in the healthy case.
    """
    from app.services.search.embedding_provenance import search_provenance_advisory

    advisory = search_provenance_advisory()
    return {
        **({"embedding_warning": advisory} if advisory else {}),
        "query": response.query,
        "results": [
            {
                "file_uuid": hit.file_uuid,
                "file_id": hit.file_id,
                "title": hit.title,
                "speakers": hit.speakers,
                "tags": hit.tags,
                "upload_time": hit.upload_time,
                "language": hit.language,
                "content_type": hit.content_type,
                "relevance_score": hit.relevance_score,
                "occurrences": [
                    {
                        "snippet": occ.snippet,
                        "speaker": occ.speaker,
                        "speaker_highlighted": occ.speaker_highlighted,
                        "start_time": occ.start_time,
                        "end_time": occ.end_time,
                        "chunk_index": occ.chunk_index,
                        "score": occ.score,
                        "match_type": occ.match_type,
                        "has_keyword_match": occ.has_keyword_match,
                        "highlight_type": occ.highlight_type,
                    }
                    for occ in hit.occurrences
                ],
                "total_occurrences": hit.total_occurrences,
                "title_highlighted": hit.title_highlighted,
                "keyword_occurrences": hit.keyword_occurrences,
                "semantic_only": hit.semantic_only,
                "semantic_confidence": hit.semantic_confidence,
                "match_sources": hit.match_sources,
                "relevance_percent": hit.relevance_percent,
                "duration": hit.duration,
                "file_size": hit.file_size,
                "semantic_occurrences": hit.semantic_occurrences,
                "has_both_match_types": hit.has_both_match_types,
            }
            for hit in response.results
        ],
        "total_results": response.total_results,
        "total_files": response.total_files,
        "page": response.page,
        "page_size": response.page_size,
        "total_pages": response.total_pages,
        "search_time_ms": response.search_time_ms,
        "filters_applied": response.filters_applied,
        "search_mode": getattr(response, "search_mode", "hybrid"),
    }


@router.get("")
def search_transcripts(
    q: str = Query(..., min_length=1, description="Search query"),
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(
        SEARCH_DEFAULT_PAGE_SIZE, ge=1, le=SEARCH_MAX_PAGE_SIZE, description="Results per page"
    ),
    speakers: list[str] = Query(None, description="Filter by speaker names"),
    tags: list[str] = Query(None, description="Filter by tags"),
    date_from: str | None = Query(None, description="Filter from date (ISO format)"),
    date_to: str | None = Query(None, description="Filter to date (ISO format)"),
    sort_by: str = Query(
        "relevance",
        description=(
            "Sort by: relevance, upload_time, completed_at, filename, duration, file_size. "
            "Note: completed_at uses upload_time for search results (completion time not indexed)."
        ),
    ),
    sort_order: str = Query("desc", description="Sort order: asc or desc"),
    search_mode: str = Query("hybrid", description="Search mode: hybrid or keyword"),
    file_type: list[str] | None = Query(None, description="Filter by file type: audio, video"),
    collection_id: int | None = Query(None, description="Filter by collection ID"),
    min_duration: float | None = Query(None, description="Minimum duration in seconds"),
    max_duration: float | None = Query(None, description="Maximum duration in seconds"),
    min_file_size: int | None = Query(None, description="Minimum file size in bytes"),
    max_file_size: int | None = Query(None, description="Maximum file size in bytes"),
    language: str | None = Query(None, description="Filter by language code"),
    title_filter: str | None = Query(
        None, description="Filter by filename/title (substring match)"
    ),
    file_uuid: str | None = Query(
        None,
        description=(
            "Scope results to a single file (its UUID). Used by the in-page transcript "
            "find bar to list every match across the whole paginated transcript."
        ),
    ),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    _active: User = Depends(get_current_active_user),  # preserve the is_active gate
) -> dict[str, Any]:
    """
    Google-style hybrid search across all transcripts.

    Returns results grouped by file with timestamped occurrences.
    Uses BM25 + vector search with Reciprocal Rank Fusion (RRF).

    Args:
        q: Search query text.
        page: Page number (1-indexed).
        page_size: Number of results per page.
        speakers: Optional speaker name filters.
        tags: Optional tag filters.
        date_from: Optional start date filter.
        date_to: Optional end date filter.
        sort_by: Sort field - relevance, upload_time, completed_at, filename, duration, file_size.
        sort_order: Sort direction - asc or desc.

    Returns:
        Search results grouped by file with highlighted snippets.

    Notes:
        - Results are sorted in unified order by the requested field. RRF scores
          already combine both keyword and semantic signals.
        - Sorting by 'completed_at' uses upload_time as a fallback since the completion
          timestamp is not indexed in the search layer.
    """
    valid_sort_fields = (
        "relevance",
        "upload_time",
        "completed_at",
        "filename",
        "duration",
        "file_size",
    )
    if sort_by not in valid_sort_fields:
        raise HTTPException(
            status_code=400,
            detail=f"sort_by must be one of: {', '.join(valid_sort_fields)}",
        )

    if sort_order not in ("asc", "desc"):
        raise HTTPException(status_code=400, detail="sort_order must be: asc or desc")

    if search_mode not in ("hybrid", "keyword"):
        raise HTTPException(status_code=400, detail="search_mode must be: hybrid or keyword")

    from app.services.search.hybrid_search_service import HybridSearchService

    search_service = HybridSearchService()
    response = search_service.search(
        query=q,
        user_id=ctx.user.id,
        page=page,
        page_size=page_size,
        speakers=speakers,
        tags=tags,
        date_from=date_from,
        date_to=date_to,
        sort_by=sort_by,
        sort_order=sort_order,
        search_mode=search_mode,
        file_type=file_type,
        collection_id=collection_id,
        min_duration=min_duration,
        max_duration=max_duration,
        min_file_size=min_file_size,
        max_file_size=max_file_size,
        language=language,
        title_filter=title_filter,
        organization_id=ctx.org_id,
        file_uuid=file_uuid,
    )

    # Abuse/DMCA: the OpenSearch transcript index has no quarantine field, so
    # drop any taken-down files from the result page against the DB (page-sized,
    # one IN query). Admins keep visibility for review.
    if not ctx.user.is_admin:
        _drop_quarantined_search_hits(db, response)

    return _search_response_to_schema(response)


@router.get("/count")
def search_match_count(
    q: str = Query(..., min_length=1, description="Search query"),
    file_uuid: str | None = Query(None, description="Scope the count to a single file (its UUID)."),
    ctx: RequestContext = Depends(get_current_context),
    _active: User = Depends(get_current_active_user),  # preserve the is_active gate
) -> dict[str, int]:
    """Lightweight transcript match-count for the in-page find bar.

    Returns just ``{"total": N}`` — the number of transcript chunks matching ``q``
    (optionally within a single file). This is intentionally far cheaper than the full
    hybrid ``/search`` (no query embedding, RRF pipeline, snippets, or highlighting),
    so it stays fast under concurrent use: the find bar polls it as the user types to
    learn whether matches exist beyond the segments currently loaded in the browser.
    """
    from app.services.search.hybrid_search_service import HybridSearchService

    service = HybridSearchService()
    total = service.count_matches(
        q, user_id=ctx.user.id, file_uuid=file_uuid, organization_id=ctx.org_id
    )
    return {"total": total}


def _drop_quarantined_search_hits(db: Session, response: Any) -> None:
    """Remove quarantined files from a search response in place (non-admin).

    The hidden files 404 on detail/stream anyway (the per-resource gate), so this
    just keeps them out of the result list/snippets too — the search-snippet
    redaction surface for takedowns.
    """
    hits = getattr(response, "results", None) or []
    if not hits:
        return
    from app.models.media import MediaFile

    uuids = [h.file_uuid for h in hits if getattr(h, "file_uuid", None)]
    if not uuids:
        return
    quarantined = {
        str(row[0])
        for row in db.query(MediaFile.uuid)
        .filter(MediaFile.uuid.in_(uuids), MediaFile.is_quarantined.is_(True))
        .all()
    }
    if not quarantined:
        return
    kept = [h for h in hits if str(h.file_uuid) not in quarantined]
    removed = len(hits) - len(kept)
    response.results = kept
    # Keep the reported totals consistent with the trimmed page.
    if hasattr(response, "total_results"):
        response.total_results = max(0, int(getattr(response, "total_results", 0)) - removed)
    if hasattr(response, "total_files"):
        response.total_files = max(0, int(getattr(response, "total_files", 0)) - removed)


@router.get("/suggestions")
def search_suggestions(
    q: str = Query(..., min_length=2, description="Search prefix"),
    limit: int = Query(8, ge=1, le=20, description="Max suggestions"),
    ctx: RequestContext = Depends(get_current_context),
    _active: User = Depends(get_current_active_user),  # preserve the is_active gate
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """
    Auto-complete suggestions as user types.

    Returns ranked suggestions from title prefix matches,
    speaker name matches, and frequent content terms.

    Args:
        q: Search prefix text.
        limit: Maximum number of suggestions.

    Returns:
        List of suggestion items with type and text.
    """
    from app.services.search.hybrid_search_service import HybridSearchService

    search_service = HybridSearchService()
    suggestions = search_service.get_suggestions(
        prefix=q,
        user_id=ctx.user.id,
        limit=limit,
        organization_id=ctx.org_id,
    )

    # Abuse/DMCA: like the results page, the chunks index carries no quarantine
    # field — drop taken-down files' titles from autocomplete against the DB
    # (admins keep visibility for review). Speaker-name suggestions carry no
    # file linkage and stay as-is.
    if suggestions and not ctx.user.is_admin:
        from app.models.media import MediaFile

        uuids = [s["file_uuid"] for s in suggestions if s.get("file_uuid")]
        if uuids:
            quarantined = {
                str(row[0])
                for row in db.query(MediaFile.uuid)
                .filter(MediaFile.uuid.in_(uuids), MediaFile.is_quarantined.is_(True))
                .all()
            }
            if quarantined:
                suggestions = [s for s in suggestions if str(s.get("file_uuid")) not in quarantined]

    return suggestions


@router.get("/filters")
def get_available_filters(
    ctx: RequestContext = Depends(get_current_context),
    _active: User = Depends(get_current_active_user),  # preserve the is_active gate
) -> dict[str, Any]:
    """
    Return available filter options (speakers, tags, date range).

    Returns:
        Dict with speakers, tags, and date_range filter options.
    """
    from app.services.search.hybrid_search_service import HybridSearchService

    search_service = HybridSearchService()
    return search_service.get_available_filters(user_id=ctx.user.id, organization_id=ctx.org_id)


@router.post("/reindex")
def trigger_reindex(
    file_uuids: list[str] | None = Body(
        None, description="Optional list of specific file UUIDs to reindex"
    ),
    pending_only: bool = Query(False, description="Only reindex files without chunks"),
    current_user: User = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """
    Trigger re-indexing of existing transcripts as a background Celery task.

    Args:
        file_uuids: Optional list of specific file UUIDs. None = all files.
        pending_only: If True, only reindex files that have no chunks in OpenSearch.

    Returns:
        Dict with task_id and status.
    """
    if pending_only and file_uuids is None:
        # Find file UUIDs that are NOT yet indexed in OpenSearch
        from sqlalchemy import exists
        from sqlalchemy import select

        from app.db.session_utils import session_scope
        from app.models.media import FileStatus
        from app.models.media import MediaFile
        from app.models.media import TranscriptSegment
        from app.services.opensearch_service import opensearch_client

        # Get completed file UUIDs that have transcript segments (indexable)
        with session_scope() as db:
            has_segments = exists(
                select(TranscriptSegment.id).where(TranscriptSegment.media_file_id == MediaFile.id)
            )
            completed_files = (
                db.query(MediaFile.uuid)
                .filter(
                    MediaFile.user_id == current_user.id,
                    MediaFile.status == FileStatus.COMPLETED,
                    has_segments,
                )
                .all()
            )
            all_uuids = {str(row[0]) for row in completed_files}

        if not all_uuids:
            return {
                "task_id": None,
                "status": "no_pending",
                "message": "No completed files found to index.",
            }

        # Find which file UUIDs already have chunks in OpenSearch
        indexed_uuids: set[str] = set()
        if opensearch_client:
            try:
                index_name = settings.OPENSEARCH_CHUNKS_INDEX
                if opensearch_client.indices.exists(index=index_name):
                    agg_response = opensearch_client.search(
                        index=index_name,
                        body={
                            "size": 0,
                            "query": {
                                "bool": {
                                    "filter": [
                                        {"term": {"user_id": current_user.id}},
                                        # G4: "which files still need indexing?".
                                        # A file left with only a digest (a rebuild
                                        # that failed part-way) would otherwise read
                                        # as indexed and never be repaired.
                                        chunk_plane_clause(),
                                    ]
                                }
                            },
                            "aggs": {
                                "indexed_files": {
                                    "terms": {
                                        "field": "file_uuid",
                                        "size": min(len(all_uuids) + 100, 65536),
                                    }
                                }
                            },
                        },
                    )
                    buckets = (
                        agg_response.get("aggregations", {})
                        .get("indexed_files", {})
                        .get("buckets", [])
                    )
                    indexed_uuids = {b["key"] for b in buckets}
            except Exception as e:
                logger.exception(f"Error querying indexed files: {e}")

        # Compute the difference: files that need indexing
        pending_uuids = list(all_uuids - indexed_uuids)

        if not pending_uuids:
            return {
                "task_id": None,
                "status": "no_pending",
                "message": "All files are already indexed.",
            }

        file_uuids = pending_uuids
        logger.info(
            f"Pending-only reindex: {len(pending_uuids)} files need indexing "
            f"(out of {len(all_uuids)} total)"
        )

    from app.tasks.reindex_task import reindex_transcripts_task

    task = reindex_transcripts_task.delay(
        user_id=current_user.id,
        file_uuids=file_uuids,
    )

    logger.info(
        f"Re-index task {task.id} started for user {current_user.id}, "
        f"files: {len(file_uuids) if file_uuids else 'all'}"
    )

    return {
        "task_id": task.id,
        "status": "started",
        "message": "Re-indexing started. Progress will be sent via WebSocket.",
    }


@router.post("/reindex/stop")
def stop_reindex(
    current_user: User = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """Request cancellation of a running reindex task.

    Sets a Redis flag that the reindex task checks between files.
    The task will stop after completing the current file and restore
    normal index settings (refresh_interval).

    Returns:
        Dict with stop status.
    """
    user_id = current_user.id

    if not _check_reindex_task_active(user_id):
        return {
            "status": "not_running",
            "message": "No reindex task is currently running.",
        }

    try:
        redis_client = get_redis()
        redis_client.setex(f"reindex_cancel:{user_id}", 3600, "1")

        logger.info(f"Reindex stop requested for user {user_id}")

        return {
            "status": "stop_requested",
            "message": "Stop signal sent. Reindex will stop after the current file completes.",
        }
    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error("Failed to request reindex stop: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again.",
        ) from e


def _check_reindex_task_active(user_id: int) -> bool:
    """Check if a reindex task is currently active for this user.

    Uses the ``reindex_lock:{user_id}`` Redis key that the reindex task
    itself sets on start (NX, 1-hour TTL) and clears on finish.
    This is a sub-millisecond Redis GET — no Celery broadcast needed.

    Args:
        user_id: The user ID to check for active reindex tasks.

    Returns:
        True if a reindex task is actively running for this user.
    """
    try:
        from app.core.redis import get_redis

        return bool(get_redis().exists(f"reindex_lock:{user_id}"))
    except Exception as e:
        logger.debug(f"Could not check reindex lock: {e}")
        return False


@router.get("/reindex/status")
def reindex_status(
    current_user: User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """
    Check re-indexing status and index health.

    Returns:
        Dict with total_files, indexed_files, pending info, current model.
    """
    from sqlalchemy import exists
    from sqlalchemy import select

    from app.db.session_utils import session_scope
    from app.models.media import FileStatus
    from app.models.media import MediaFile
    from app.models.media import TranscriptSegment
    from app.services.opensearch_service import opensearch_client
    from app.services.search.settings_service import get_search_embedding_settings

    with session_scope() as db:
        # Only count completed files that have transcript segments (indexable)
        has_segments = exists(
            select(TranscriptSegment.id).where(TranscriptSegment.media_file_id == MediaFile.id)
        )
        total_files = (
            db.query(MediaFile)
            .filter(
                MediaFile.user_id == current_user.id,
                MediaFile.status == FileStatus.COMPLETED,
                has_segments,
            )
            .count()
        )

    # Count indexed files in OpenSearch chunks index (skip redundant exists check)
    indexed_files = 0
    last_indexed_at = None
    if opensearch_client:
        try:
            count_response = opensearch_client.search(
                index=settings.OPENSEARCH_CHUNKS_INDEX,
                body={
                    "size": 0,
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"user_id": current_user.id}},
                                # G4, again: the number the admin UI shows as
                                # "indexed files" must count files with chunks.
                                chunk_plane_clause(),
                            ]
                        }
                    },
                    "aggs": {
                        "unique_files": {"cardinality": {"field": "file_uuid"}},
                        "last_indexed": {"max": {"field": "indexed_at"}},
                    },
                },
            )
            aggs = count_response.get("aggregations", {})
            indexed_files = aggs.get("unique_files", {}).get("value", 0)
            last_indexed_at = aggs.get("last_indexed", {}).get("value_as_string")
        except Exception as e:
            logger.exception(f"Error checking index status: {e}")

    # Check if a reindex task is actively running for this user
    in_progress = _check_reindex_task_active(current_user.id)

    # Check if stop has been requested
    stop_requested = False
    if in_progress:
        try:
            redis_client = get_redis()
            stop_requested = bool(redis_client.get(f"reindex_cancel:{current_user.id}"))
        except Exception as e:
            logger.debug(f"Could not check reindex cancellation flag: {e}")

    current_model, current_dimension = get_search_embedding_settings()

    return {
        "total_files": total_files,
        "indexed_files": indexed_files,
        "pending_files": max(0, total_files - indexed_files),
        "in_progress": in_progress,
        "stop_requested": stop_requested,
        "current_model": current_model,
        "current_dimension": current_dimension,
        "last_indexed_at": last_indexed_at,
    }


# =============================================================================
# Index Health & Repair Endpoints
# =============================================================================


@router.get("/index-health")
def get_index_health(
    current_user: User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """Return health status of each OpenSearch index.

    Tests each index with a simple match_all query (size=0) and
    returns per-index status with doc counts.

    Returns:
        Dict with per-index health: {index_name: {status, doc_count, error}}.
    """
    from app.services.opensearch_service import opensearch_client

    indices = [
        get_speaker_index(),
        settings.OPENSEARCH_TRANSCRIPT_INDEX,
        get_speaker_index_v4(),
        settings.OPENSEARCH_CHUNKS_INDEX,
    ]

    health: dict[str, Any] = {}

    if not opensearch_client:
        for idx in indices:
            health[idx] = {
                "status": "red",
                "doc_count": 0,
                "error": "OpenSearch client not initialized",
            }
        return health

    # Resolve aliases to concrete index names, then use a single _cat/indices
    # call to get doc counts for everything at once (replaces 8 sequential
    # HTTP calls with 1-2).
    alias_map: dict[str, str] = {}  # alias → concrete index name
    concrete_names: list[str] = []
    try:
        aliases_response = opensearch_client.cat.aliases(format="json", h="alias,index")
        for row in aliases_response:
            alias_map[row.get("alias", "")] = row.get("index", "")
    except Exception as e:
        logger.debug("Could not resolve aliases: %s", e)

    for idx in indices:
        concrete_names.append(alias_map.get(idx, idx))

    index_stats: dict[str, int] = {}
    try:
        cat_response = opensearch_client.cat.indices(
            index=",".join(concrete_names),
            format="json",
            h="index,docs.count",
        )
        for row in cat_response:
            idx_name = row.get("index", "")
            doc_count_str = row.get("docs.count", "0")
            index_stats[idx_name] = int(doc_count_str) if doc_count_str else 0
    except Exception as e:
        logger.debug("Bulk _cat/indices call failed, will mark missing: %s", e)

    for idx in indices:
        concrete = alias_map.get(idx, idx)
        if concrete in index_stats:
            health[idx] = {
                "status": "green",
                "doc_count": index_stats[concrete],
                "error": None,
            }
        else:
            health[idx] = {
                "status": "red",
                "doc_count": 0,
                "error": "Index does not exist or is unreachable",
            }

    return health


def _probe_index_health(indices: list[str]) -> dict[str, str]:
    """Classify each index as ``healthy`` / ``missing`` / ``unhealthy``.

    Two OpenSearch round trips **per index**, so it runs with **no DB session
    held** — hence a plain list in, a plain dict out, and no ``db`` parameter.
    An exception from either probe means "unhealthy": that is the repair trigger,
    and an ``exists`` call that raises is exactly as broken as a failing search.
    """
    from app.services.opensearch_service import opensearch_client

    health: dict[str, str] = {}
    for idx in indices:
        try:
            if not opensearch_client.indices.exists(index=idx):
                health[idx] = "missing"
                continue
            # Test if index is healthy
            opensearch_client.search(index=idx, body={"query": {"match_all": {}}, "size": 0})
            health[idx] = "healthy"
        except Exception:
            health[idx] = "unhealthy"
    return health


@router.post("/repair-indices")
def repair_indices(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """Repair corrupted OpenSearch indices.

    For the speakers index (kNN), rebuilds from PostgreSQL data.
    For other indices, attempts close/reopen and force merge strategies.

    **Ordered so the request transaction is released across the OpenSearch work.**
    ``db`` comes from ``Depends(get_db)`` and lives for the whole request, so the
    eight probe round trips below used to run with a Postgres transaction open
    (opened by the admin-auth dependency) — ACCESS SHARE held for the length of a
    possibly-unreachable cluster's timeouts. The probes and the non-speaker
    repairs now run with nothing held; only ``rebuild_speaker_index`` needs
    Postgres, and it is left until last so it opens a fresh transaction of its own.

    Returns:
        Dict with per-index repair results.
    """
    from app.services.opensearch_service import _repair_index
    from app.services.opensearch_service import opensearch_client
    from app.services.opensearch_service import rebuild_speaker_index

    if not opensearch_client:
        raise HTTPException(status_code=503, detail="OpenSearch client not available")

    speaker_index = get_speaker_index()
    indices = [
        speaker_index,
        settings.OPENSEARCH_TRANSCRIPT_INDEX,
        get_speaker_index_v4(),
        settings.OPENSEARCH_CHUNKS_INDEX,
    ]

    # Release the request transaction before the OpenSearch phase below.
    db.close()

    # Phase 1 — probe every index, with no Postgres transaction held.
    health = _probe_index_health(indices)
    results: dict[str, str] = {idx: state for idx, state in health.items() if state != "unhealthy"}
    speakers_indexed = 0

    # Phase 2 — repair the non-speaker indices. Still OpenSearch-only.
    for idx in indices:
        if health[idx] != "unhealthy" or idx == speaker_index:
            continue
        repaired = _repair_index(idx)
        results[idx] = "repaired" if repaired else "failed"

    # Phase 3 — the one repair that reads Postgres, in a transaction of its own.
    if health.get(speaker_index) == "unhealthy":
        try:
            rebuild_result = rebuild_speaker_index(db)
            if rebuild_result.get("status") == "rebuilt":
                results[speaker_index] = "rebuilt"
                speakers_indexed = rebuild_result.get("speakers_indexed", 0)
            else:
                results[speaker_index] = "failed"
        except Exception as e:
            logger.exception(f"Failed to rebuild speakers index: {e}")
            results[speaker_index] = "failed"

    any_failed = any(v == "failed" for v in results.values())

    return {
        "status": "partial_failure" if any_failed else "success",
        "indices": results,
        "speakers_indexed": speakers_indexed,
        "message": (
            "Some indices could not be repaired." if any_failed else "Index repair complete."
        ),
    }


# =============================================================================
# Embedding Model Selection Endpoints
# =============================================================================


def _switch_model(model_name: str, triggered_by: int) -> dict[str, Any]:
    """Run the model switch and turn its two refusals into HTTP status codes.

    The switch itself is business logic and lives in
    ``services/search/model_switch.py``; this is the response-shaping half.
    ``409`` rather than ``400`` for an undeployed model because the request is
    well-formed — the *deployment* is not in a state that can honour it, and the
    remedy (register, then deploy) is named in the detail.

    ``503`` for a dispatch failure is the one that reports a **half-applied**
    switch: the settings, pipeline and index mapping have already changed, and
    only the re-embed failed to queue. It is an error rather than a partial
    success on purpose — the resulting index is exactly the mixed vector space
    #437 exists to prevent, and the detail says so.

    Args:
        model_name: A key of ``OPENSEARCH_EMBEDDING_MODELS``.
        triggered_by: User id performing the switch.

    Returns:
        The service's result dict.

    Raises:
        HTTPException: 400 unknown model, 409 model not registered *and*
            deployed, 503 the re-embed could not be queued.
    """
    from app.services.search.model_switch import EmbeddingModelNotDeployedError
    from app.services.search.model_switch import ReindexDispatchError
    from app.services.search.model_switch import UnknownEmbeddingModelError
    from app.services.search.model_switch import apply_embedding_model_switch

    try:
        return apply_embedding_model_switch(model_name, triggered_by)
    except UnknownEmbeddingModelError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except EmbeddingModelNotDeployedError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ReindexDispatchError as e:
        logger.error(f"Model switch left half-applied: {e}")
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.get("/models")
def get_embedding_models(
    current_user: User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """
    Get available embedding models for search indexing.

    Returns the list of sentence transformer models that can be used
    for semantic search embedding.
    """
    from app.services.search.settings_service import get_search_embedding_model

    models = [
        {
            "model_id": model_name,
            "name": info["name"],
            "dimension": info["dimension"],
            "description": info["description"],
            "size_mb": info["size_mb"],
        }
        for model_name, info in OPENSEARCH_EMBEDDING_MODELS.items()
    ]

    return {
        "models": models,
        "current_model_id": get_search_embedding_model(),
    }


@router.post("/models")
def set_embedding_model(
    request: SetEmbeddingModelSchema,
    current_user: User = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """Set the embedding model and reindex every user's transcripts.

    The settings-UI entry point. It delegates to
    ``services/search/model_switch`` — it used to write the settings row and
    nothing else, which changed the recorded model and the index dimension while
    leaving the ingest pipeline embedding with the previous model (issue #437).
    """
    applied = _switch_model(request.model_id, current_user.id)
    model_info = OPENSEARCH_EMBEDDING_MODELS[request.model_id]
    return {
        **applied,
        "status": "model_changed",
        "model_id": applied["model_name"],
        "message": (
            f"Switched to {model_info['name']}. Re-indexing the transcripts of "
            f"{applied['reindex_users']} user(s)."
        ),
    }


# =============================================================================
# OpenSearch Neural Search Model Management Endpoints
# =============================================================================


@router.get("/models/neural")
def get_neural_models(
    current_user: User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """
    Get available OpenSearch neural search models with deployment status.

    Returns both the model registry and current deployment state.

    Returns:
        Dict with models list, neural_enabled flag, and active model info.
    """
    from app.services.search.ml_model_service import get_ml_model_service

    ml_service = get_ml_model_service()

    # Get registered/deployed models from OpenSearch
    deployed_models = ml_service.list_models()
    deployed_by_name = {m["name"]: m for m in deployed_models}

    # Build model list from registry with deployment status
    models = []
    for model_name, info in OPENSEARCH_EMBEDDING_MODELS.items():
        deployed_info = deployed_by_name.get(model_name, {})
        models.append(
            {
                "model_name": model_name,
                "display_name": info["name"],
                "dimension": info["dimension"],
                "size_mb": info["size_mb"],
                "languages": info["languages"],
                "description": info["description"],
                "default": info.get("default", False),
                "registered": bool(deployed_info.get("model_id")),
                "deployed": deployed_info.get("deployed", False),
                "model_id": deployed_info.get("model_id"),
                "state": deployed_info.get("state", "NOT_REGISTERED"),
            }
        )

    # Get active model
    active_model_id = ml_service.get_active_model_id()
    active_model_name = None
    if active_model_id:
        for m in deployed_models:
            if m.get("model_id") == active_model_id:
                active_model_name = m.get("name")
                break

    return {
        "neural_enabled": settings.OPENSEARCH_NEURAL_SEARCH_ENABLED,
        "models": models,
        "active_model_id": active_model_id,
        "active_model_name": active_model_name,
    }


@router.post("/models/neural/{model_name}/register")
def register_neural_model(
    model_name: str,
    current_user: User = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """
    Register a neural model in OpenSearch.

    Downloads and registers the model from HuggingFace. This may take
    several minutes depending on model size.

    Args:
        model_name: Full model name from OPENSEARCH_EMBEDDING_MODELS.

    Returns:
        Dict with registration status and model_id.
    """
    if model_name not in OPENSEARCH_EMBEDDING_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown model: {model_name}. Available: {list(OPENSEARCH_EMBEDDING_MODELS.keys())}",
        )

    from app.services.search.ml_model_service import get_ml_model_service

    ml_service = get_ml_model_service()

    # Check if already registered
    existing_id = ml_service.find_model_by_name(model_name)
    if existing_id:
        return {
            "status": "already_registered",
            "model_id": existing_id,
            "model_name": model_name,
        }

    # Register the model
    model_info = OPENSEARCH_EMBEDDING_MODELS[model_name]
    model_id = ml_service.register_model(
        model_name=model_name,
        model_format=str(model_info.get("model_format", "TORCH_SCRIPT")),
        description=str(model_info.get("description", "")),
    )

    if not model_id:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to register model {model_name}. Check OpenSearch logs.",
        )

    logger.info(f"Registered neural model {model_name} as {model_id}")

    return {
        "status": "registered",
        "model_id": model_id,
        "model_name": model_name,
    }


@router.post("/models/neural/{model_name}/deploy")
def deploy_neural_model(
    model_name: str,
    current_user: User = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """
    Deploy a registered neural model for inference.

    The model must be registered first. Deployment loads the model into
    memory for fast inference.

    Args:
        model_name: Full model name from OPENSEARCH_EMBEDDING_MODELS.

    Returns:
        Dict with deployment status.
    """
    from app.services.search.ml_model_service import get_ml_model_service

    ml_service = get_ml_model_service()

    # Find the model
    model_id = ml_service.find_model_by_name(model_name)
    if not model_id:
        raise HTTPException(
            status_code=404,
            detail=f"Model {model_name} not registered. Register it first.",
        )

    # Check if already deployed
    status = ml_service.get_model_status(model_id)
    if status.get("deployed"):
        return {
            "status": "already_deployed",
            "model_id": model_id,
            "model_name": model_name,
        }

    # Deploy the model
    if not ml_service.deploy_model(model_id):
        raise HTTPException(
            status_code=500,
            detail=f"Failed to deploy model {model_name}. Check OpenSearch logs.",
        )

    logger.info(f"Deployed neural model {model_name} ({model_id})")

    return {
        "status": "deployed",
        "model_id": model_id,
        "model_name": model_name,
    }


@router.post("/models/neural/{model_name}/undeploy")
def undeploy_neural_model(
    model_name: str,
    current_user: User = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """
    Undeploy a neural model to free memory.

    The model remains registered and can be redeployed later.

    Args:
        model_name: Full model name from OPENSEARCH_EMBEDDING_MODELS.

    Returns:
        Dict with undeploy status.
    """
    from app.services.search.ml_model_service import get_ml_model_service

    ml_service = get_ml_model_service()

    # Find the model
    model_id = ml_service.find_model_by_name(model_name)
    if not model_id:
        raise HTTPException(
            status_code=404,
            detail=f"Model {model_name} not registered.",
        )

    if not ml_service.undeploy_model(model_id):
        raise HTTPException(
            status_code=500,
            detail=f"Failed to undeploy model {model_name}.",
        )

    # Reset neural search state since active model may have changed
    from app.services.search.hybrid_search_service import reset_neural_search_state

    reset_neural_search_state()

    logger.info(f"Undeployed neural model {model_name} ({model_id})")

    return {
        "status": "undeployed",
        "model_id": model_id,
        "model_name": model_name,
    }


@router.put("/models/neural/active")
def set_active_neural_model(
    model_name: str = Query(..., description="Model name to set as active"),
    current_user: User = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """Set the active neural model for search and reindex every user's transcripts.

    The model must be registered and deployed first. This will:

    1. Persist the model **and its dimension** to settings
    2. Update the neural ingest pipeline with the new model
    3. Recreate the index if the dimension changed
    4. Dispatch a reindex for **every** user that owns transcripts
    5. Reset search caches

    Step 1 used to be missing (issue #437), so the reindex coordinator then read
    the *previous* dimension and recreated the index a second time at the wrong
    size; step 4 used to cover only the caller, leaving every other user's chunks
    in the old model's vector space. Both live in
    ``services/search/model_switch.apply_embedding_model_switch``, which
    ``POST /search/models`` shares — the two endpoints were each doing a different
    half of the same job.

    Args:
        model_name: Full model name from OPENSEARCH_EMBEDDING_MODELS.

    Returns:
        Dict with status, reindex task ids, and the post-switch provenance survey.
    """
    applied = _switch_model(model_name, current_user.id)
    model_info = OPENSEARCH_EMBEDDING_MODELS[model_name]
    return {
        **applied,
        "status": "active_model_set",
        "message": (
            f"Switched to {model_info['name']}. Re-indexing the transcripts of "
            f"{applied['reindex_users']} user(s) with the neural pipeline."
        ),
    }


@router.get("/models/neural/status")
def get_neural_search_status(
    current_user: User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """
    Get the current neural search status.

    Returns:
        Dict with neural search enabled flag, active model, pipeline status, and
        the ``embedding_provenance`` survey — the one query that answers whether
        the index is a single comparable vector space (issue #437). It lives here
        rather than in ``POST /search/repair-indices`` because that endpoint's
        machinery is close/reopen/force-merge, which cannot repair a mixed vector
        space; reporting it there would imply the repair had addressed it. Only a
        reindex does, and this is the endpoint an operator reads before deciding
        to run one.
    """
    from app.services.search.embedding_provenance import survey_embedding_models
    from app.services.search.indexing_service import is_neural_pipeline_available
    from app.services.search.ml_model_service import get_ml_model_service
    from app.services.search.model_switch import provenance_payload

    ml_service = get_ml_model_service()
    active_model_id = ml_service.get_active_model_id()

    # Get active model details
    active_model_name = None
    active_model_info = None
    if active_model_id:
        status = ml_service.get_model_status(active_model_id)
        active_model_name = status.get("name")
        if active_model_name and active_model_name in OPENSEARCH_EMBEDDING_MODELS:
            active_model_info = OPENSEARCH_EMBEDDING_MODELS[active_model_name]

    return {
        "neural_enabled": settings.OPENSEARCH_NEURAL_SEARCH_ENABLED,
        "neural_pipeline_available": is_neural_pipeline_available(),
        "active_model_id": active_model_id,
        "active_model_name": active_model_name,
        "active_model_dimension": active_model_info["dimension"] if active_model_info else None,
        "pipeline_name": settings.OPENSEARCH_NEURAL_PIPELINE,
        "embedding_provenance": provenance_payload(survey_embedding_models()),
    }
