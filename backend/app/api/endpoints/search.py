"""Search API endpoints with hybrid BM25 + vector search."""

import logging
from typing import Any
from typing import cast

from fastapi import APIRouter
from fastapi import Body
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from sqlalchemy.orm import Session

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

logger = logging.getLogger(__name__)

router = APIRouter()


def _search_response_to_schema(response) -> dict[str, Any]:
    """Convert HybridSearchService response to serializable dict."""
    return {
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
    current_user: User = Depends(get_current_active_user),
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
        user_id=current_user.id,
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
    )

    return _search_response_to_schema(response)


@router.get("/suggestions")
def search_suggestions(
    q: str = Query(..., min_length=2, description="Search prefix"),
    limit: int = Query(8, ge=1, le=20, description="Max suggestions"),
    current_user: User = Depends(get_current_active_user),
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
    return search_service.get_suggestions(
        prefix=q,
        user_id=current_user.id,
        limit=limit,
    )


@router.get("/filters")
def get_available_filters(
    current_user: User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """
    Return available filter options (speakers, tags, date range).

    Returns:
        Dict with speakers, tags, and date_range filter options.
    """
    from app.services.search.hybrid_search_service import HybridSearchService

    search_service = HybridSearchService()
    return search_service.get_available_filters(user_id=current_user.id)


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
                            "query": {"term": {"user_id": current_user.id}},
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
                logger.error(f"Error querying indexed files: {e}")

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
                    "query": {"term": {"user_id": current_user.id}},
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
            logger.error(f"Error checking index status: {e}")

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


@router.post("/repair-indices")
def repair_indices(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """Repair corrupted OpenSearch indices.

    For the speakers index (kNN), rebuilds from PostgreSQL data.
    For other indices, attempts close/reopen and force merge strategies.

    Returns:
        Dict with per-index repair results.
    """
    from app.services.opensearch_service import _repair_index
    from app.services.opensearch_service import opensearch_client
    from app.services.opensearch_service import rebuild_speaker_index

    if not opensearch_client:
        raise HTTPException(status_code=503, detail="OpenSearch client not available")

    indices = [
        get_speaker_index(),
        settings.OPENSEARCH_TRANSCRIPT_INDEX,
        get_speaker_index_v4(),
        settings.OPENSEARCH_CHUNKS_INDEX,
    ]

    results: dict[str, str] = {}
    speakers_indexed = 0

    for idx in indices:
        try:
            if not opensearch_client.indices.exists(index=idx):
                results[idx] = "missing"
                continue
            # Test if index is healthy
            opensearch_client.search(index=idx, body={"query": {"match_all": {}}, "size": 0})
            results[idx] = "healthy"
        except Exception:
            # Index is unhealthy, attempt repair
            if idx == get_speaker_index():
                try:
                    rebuild_result = rebuild_speaker_index(db)
                    if rebuild_result.get("status") == "rebuilt":
                        results[idx] = "rebuilt"
                        speakers_indexed = rebuild_result.get("speakers_indexed", 0)
                    else:
                        results[idx] = "failed"
                except Exception as e:
                    logger.error(f"Failed to rebuild speakers index: {e}")
                    results[idx] = "failed"
            else:
                repaired = _repair_index(idx)
                results[idx] = "repaired" if repaired else "failed"

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
    """
    Set the embedding model and trigger reindex.

    Changing the embedding model requires re-indexing all transcripts
    since the vector dimensions will change.
    """
    model_id = request.model_id
    if model_id not in OPENSEARCH_EMBEDDING_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown model: {model_id}",
        )

    from app.services.search.settings_service import save_search_embedding_model

    model_info = OPENSEARCH_EMBEDDING_MODELS[model_id]
    dimension = cast(int, model_info["dimension"])

    # Update setting
    save_search_embedding_model(model_id, dimension)

    # Trigger reindex
    from app.tasks.reindex_task import reindex_transcripts_task

    task = reindex_transcripts_task.delay(
        user_id=current_user.id,
        file_uuids=None,
    )

    logger.info(f"Embedding model changed to {model_id} ({dimension}d), reindex task: {task.id}")

    return {
        "status": "model_changed",
        "model_id": model_id,
        "reindex_task_id": task.id,
        "message": f"Switched to {model_info['name']}. Re-indexing all transcripts.",
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
    """
    Set the active neural model for search and trigger reindex.

    The model must be deployed before it can be set as active.
    This will:
    1. Update the neural ingest pipeline with the new model
    2. Trigger a full reindex of all transcripts
    3. Reset search caches

    Args:
        model_name: Full model name from OPENSEARCH_EMBEDDING_MODELS.

    Returns:
        Dict with status and reindex task info.
    """
    if model_name not in OPENSEARCH_EMBEDDING_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown model: {model_name}",
        )

    from app.services.search.ml_model_service import get_ml_model_service

    ml_service = get_ml_model_service()

    # Find and verify the model is deployed
    model_id = ml_service.find_model_by_name(model_name)
    if not model_id:
        raise HTTPException(
            status_code=404,
            detail=f"Model {model_name} not registered. Register and deploy it first.",
        )

    status = ml_service.get_model_status(model_id)
    if not status.get("deployed"):
        raise HTTPException(
            status_code=400,
            detail=f"Model {model_name} is not deployed. Deploy it first.",
        )

    # Set as active model
    ml_service.set_active_model_id(model_id)

    # Update the neural ingest pipeline
    from app.services.search.indexing_service import ensure_neural_ingest_pipeline
    from app.services.search.indexing_service import reset_neural_pipeline_state

    reset_neural_pipeline_state()
    ensure_neural_ingest_pipeline(model_id)

    # Reset search caches
    from app.services.search.hybrid_search_service import clear_search_cache
    from app.services.search.hybrid_search_service import reset_neural_search_state

    clear_search_cache()
    reset_neural_search_state()

    # Update dimension in settings
    model_info = OPENSEARCH_EMBEDDING_MODELS[model_name]
    new_dimension = cast(int, model_info["dimension"])

    # Recreate index if dimension changed
    from app.services.search.indexing_service import recreate_index_for_dimension

    recreate_index_for_dimension(new_dimension)

    # Trigger full reindex
    from app.tasks.reindex_task import reindex_transcripts_task

    task = reindex_transcripts_task.delay(
        user_id=current_user.id,
        file_uuids=None,  # All files
    )

    logger.info(f"Set active neural model to {model_name} ({model_id}), reindex task: {task.id}")

    return {
        "status": "active_model_set",
        "model_name": model_name,
        "model_id": model_id,
        "dimension": new_dimension,
        "reindex_task_id": task.id,
        "message": (
            f"Switched to {model_info['name']}. Re-indexing all transcripts with neural pipeline."
        ),
    }


@router.get("/models/neural/status")
def get_neural_search_status(
    current_user: User = Depends(get_current_active_user),
) -> dict[str, Any]:
    """
    Get the current neural search status.

    Returns:
        Dict with neural search enabled flag, active model, and pipeline status.
    """
    from app.services.search.indexing_service import is_neural_pipeline_available
    from app.services.search.ml_model_service import get_ml_model_service

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
    }
