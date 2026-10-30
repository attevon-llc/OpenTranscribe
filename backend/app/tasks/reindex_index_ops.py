"""OpenSearch index structure and bulk-mode operations used by the reindex tasks."""

import logging
from contextlib import AbstractContextManager
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

#: How long one holder may own the chunks-index structure lock. Sized to the
#: guarded work (a delete + create + alias swap), NOT to a whole reindex — a TTL
#: long enough to cover hours of bulk indexing is also long enough to freeze
#: every other owner if the holder dies.
INDEX_STRUCTURE_LOCK_TIMEOUT_SECONDS = 60

#: How long a coordinator waits for that lock before proceeding without the
#: structural check. Bounded so a stuck holder delays a reindex rather than
#: failing it.
INDEX_STRUCTURE_LOCK_WAIT_SECONDS = 30


def _ensure_neural_pipeline_ready() -> bool:
    """Ensure neural search pipeline is ready for indexing.

    Returns:
        True if neural search is available and pipeline is ready.
    """
    if not settings.OPENSEARCH_NEURAL_SEARCH_ENABLED:
        return False

    try:
        from app.services.search.pipelines import ensure_neural_ingest_pipeline
        from app.services.search.pipelines import reset_neural_pipeline_state

        # Reset state to force re-check
        reset_neural_pipeline_state()

        if ensure_neural_ingest_pipeline():
            logger.info("Neural ingest pipeline ready for reindexing")
            return True
        else:
            logger.warning("Neural ingest pipeline not available")
            return False
    except Exception as e:
        logger.warning(f"Could not setup neural pipeline: {e}")
        return False


def _handle_model_switch(model_id: str) -> dict[str, Any] | None:
    """Switch embedding model if valid. Returns error dict on failure, None on success.

    Only handles OpenSearch neural models.
    """
    from app.core.embedding_models import OPENSEARCH_EMBEDDING_MODELS
    from app.services.search.index_admin import recreate_index_for_dimension

    if model_id in OPENSEARCH_EMBEDDING_MODELS:
        model_info = OPENSEARCH_EMBEDDING_MODELS[model_id]
        new_dimension: int = model_info["dimension"]  # type: ignore[assignment]

        # Recreate index with new dimension if needed
        if not recreate_index_for_dimension(new_dimension):
            logger.error(f"Failed to recreate index for dimension {new_dimension}")
            return {"error": "Failed to recreate index for new model dimension"}

        logger.info(f"Worker using neural model {model_id} ({new_dimension}d)")
        return None

    logger.warning(f"Unknown model_id: {model_id}, using current model")
    return None


def _refresh_index_and_clear_cache() -> None:
    """Refresh the search index after a reindex completes.

    Issue #666: this used to also call ``hybrid_search_service.clear_search_cache()``,
    a process-local dict clear that ran in whichever worker executed this
    coordinator — never the API process that actually serves cached search
    responses, so it was a no-op where it mattered. The response cache is now
    invalidated by its own ``corpus_version`` cache-key field
    (``hybrid_search_service._search_corpus_version``), which
    ``index_transcript_chunks`` already bumps for every file this reindex
    processes — no explicit clear needed, cross-process, for free.
    """
    try:
        from app.services.opensearch_service import opensearch_client

        if opensearch_client:
            opensearch_client.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)
            logger.info("Refreshed search index after reindex completion")
    except Exception as e:
        logger.warning(f"Index refresh after reindex failed: {e}")


def _set_bulk_indexing_mode() -> None:
    """Temporarily disable refresh_interval for faster bulk ingestion.

    During large reindex operations, the default 1-second refresh creates
    unnecessary Lucene segments. Explicit _periodic_refresh() calls still
    work independently of this setting.
    """
    try:
        from app.services.opensearch_service import opensearch_client

        if opensearch_client:
            opensearch_client.indices.put_settings(
                index=settings.OPENSEARCH_CHUNKS_INDEX,
                body={"index": {"refresh_interval": "-1"}},
            )
            logger.info("Set refresh_interval=-1 for bulk reindex")
    except Exception as e:
        logger.warning(f"Could not set bulk indexing mode: {e}")


def _restore_normal_mode() -> None:
    """Restore normal refresh_interval after bulk ingestion."""
    try:
        from app.services.opensearch_service import opensearch_client

        if opensearch_client:
            opensearch_client.indices.put_settings(
                index=settings.OPENSEARCH_CHUNKS_INDEX,
                body={"index": {"refresh_interval": "1s"}},
            )
            logger.info("Restored refresh_interval=1s after reindex")
    except Exception as e:
        logger.warning(f"Could not restore normal mode: {e}")


def _force_merge_after_reindex() -> None:
    """Force merge to consolidate Lucene segments after a full reindex.

    For HNSW kNN, each segment has its own graph — fewer segments means
    better recall. This is only called after a full reindex, not per-file.
    """
    try:
        from app.services.opensearch_service import opensearch_client

        if opensearch_client:
            opensearch_client.indices.forcemerge(
                index=settings.OPENSEARCH_CHUNKS_INDEX,
                max_num_segments=1,
                params={"wait_for_completion": "false"},
            )
            logger.info("Force merge initiated (running in background)")
    except Exception as e:
        logger.warning(f"Force merge after reindex failed (non-fatal): {e}")


def _periodic_refresh(files_since_refresh: int) -> int:
    """Trigger a Lucene segment flush if enough files have been indexed.

    Called periodically during large reindex operations to keep search
    results fresh rather than waiting until the very end.

    Args:
        files_since_refresh: Number of files indexed since the last refresh.

    Returns:
        0 if refresh was triggered (counter reset), otherwise the unchanged count.
    """
    interval = settings.SEARCH_REINDEX_REFRESH_INTERVAL
    if files_since_refresh < interval:
        return files_since_refresh

    try:
        from app.services.opensearch_service import opensearch_client

        if opensearch_client:
            opensearch_client.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)
            logger.info(f"Periodic index refresh after {files_since_refresh} files")
    except Exception as e:
        logger.warning(f"Periodic index refresh failed: {e}")

    return 0


def _index_structure_lock() -> "AbstractContextManager[bool]":
    """Mutual exclusion over structural changes to the shared chunks index.

    ``reindex_lock:{user_id}`` serialises coordinators **per owner**, which is
    correct for the data-copy phase and useless for the structure phase: the
    index is one shared object, and both ``_check_and_recreate_stale_index`` and
    ``recreate_index_for_dimension`` can delete it. Two owners' coordinators
    starting together could therefore have one delete the index out from under
    the other's freshly created one.

    Blocking, but briefly. The guarded calls are no-ops unless a genuine
    recreate is due, so waiting is cheap in the common case; a caller that gives
    up still indexes its own files (additive, always safe) and the next pass
    re-checks the structure.

    Returns:
        Context manager yielding True when the lock is held.
    """
    from app.utils.task_lock import task_lock_manager

    return task_lock_manager.acquire_lock(
        f"opensearch_index_structure_lock:{settings.OPENSEARCH_CHUNKS_INDEX}",
        timeout=INDEX_STRUCTURE_LOCK_TIMEOUT_SECONDS,
        blocking_timeout=INDEX_STRUCTURE_LOCK_WAIT_SECONDS,
    )


def _check_and_recreate_stale_index() -> None:
    """Check if the search index has a stale schema and recreate if needed.

    When _INDEX_VERSION bumps (new analyzers, fields, etc.), the existing
    index mapping is incompatible. A full reindex is the right time to
    recreate the index with the latest mapping.

    Recreating means **deleting** the chunks index, and the reindex that follows
    only repopulates the calling user's files — so every other user's chunks are
    gone until they each run their own reindex. Three refusals bound that:

    - **No ``_meta`` block at all** → the index's provenance is unknown, so its
      version cannot be compared and it is left alone. A missing ``_meta`` used
      to read as version 0, i.e. unconditionally destroy.
    - **Unreadable embedding dimension** → abort. Defaulting to 384 gave a 768d
      deployment a wrong-dimension index in which *every* subsequent write fails.
    - **Index body built before the delete**, and a create failure is followed by
      an immediate rebuild attempt from the mapping just read, because the
      alternative state is no index at all.
    """
    try:
        from app.services.opensearch_service import opensearch_client
        from app.services.search.index_admin import _get_index_body_with_dimension
        from app.services.search.index_schema import _INDEX_VERSION

        if not opensearch_client:
            return

        index_name = settings.OPENSEARCH_CHUNKS_INDEX
        if not opensearch_client.indices.exists(index=index_name):
            return

        # Read stored version from index _meta
        mapping = opensearch_client.indices.get_mapping(index=index_name)
        index_mappings = mapping.get(index_name, {}).get("mappings", {})
        meta = index_mappings.get("_meta", {})
        stored_version = meta.get("version")

        if stored_version is None:
            logger.warning(
                f"Index '{index_name}' carries no _meta version. Refusing to recreate it: "
                "an index of unknown provenance may hold every user's chunks, and only "
                "the calling user's files get reindexed afterwards. Fix the mapping's "
                "_meta by hand if this index really is stale."
            )
            return

        if stored_version >= _INDEX_VERSION:
            logger.debug(
                f"Index '{index_name}' is at version {stored_version}, no recreation needed"
            )
            return

        logger.warning(
            f"Index '{index_name}' is version {stored_version}, "
            f"latest is {_INDEX_VERSION}. Recreating index with updated mapping."
        )

        # Get current dimension before deleting. No default: a guessed dimension
        # produces an index that rejects every write it is asked to accept.
        current_dim = index_mappings.get("properties", {}).get("embedding", {}).get("dimension")
        if not isinstance(current_dim, int) or current_dim <= 0:
            logger.error(
                f"Refusing to recreate '{index_name}': its embedding dimension could not "
                f"be read from the mapping (got {current_dim!r}). Recreating with a "
                "guessed dimension makes every subsequent write fail."
            )
            return

        # Build the new body BEFORE anything is destroyed, so a body-construction
        # failure cannot strand the deployment with no index.
        index_body = _get_index_body_with_dimension(current_dim)

        # Remove alias if it exists
        alias_name = "transcript_search"
        try:
            if opensearch_client.indices.exists_alias(name=alias_name):
                opensearch_client.indices.delete_alias(index=index_name, name=alias_name)
        except Exception as e:
            logger.debug(f"Could not remove alias '{alias_name}': {e}")

        opensearch_client.indices.delete(index=index_name)

        # The create gets its own handler: sharing one `except` with the delete
        # meant a create failure left the deployment with NO index and a single
        # warning line. Fall back to re-creating the index we just read, so the
        # write path keeps working even if the new mapping is rejected.
        try:
            opensearch_client.indices.create(index=index_name, body=index_body)
        except Exception as create_err:
            logger.error(
                f"Creating '{index_name}' with version {_INDEX_VERSION} failed after the "
                f"old index was deleted: {create_err}. Restoring the previous mapping."
            )
            opensearch_client.indices.create(index=index_name, body={"mappings": index_mappings})
            logger.warning(
                f"Restored '{index_name}' with its previous mapping (version "
                f"{stored_version}); the schema upgrade did NOT happen."
            )
            return

        # Recreate alias
        opensearch_client.indices.put_alias(index=index_name, name=alias_name)

        logger.info(f"Recreated index '{index_name}' with version {_INDEX_VERSION}")

    except Exception as e:
        logger.error(f"Failed to check/recreate stale index: {e}")
