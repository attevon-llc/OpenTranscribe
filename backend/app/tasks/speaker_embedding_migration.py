"""
Celery task for normalizing existing speaker embeddings to L2 unit vectors.

This task runs on backend startup and normalizes all speaker embeddings in OpenSearch
that were created before L2 normalization was added. Normalized embeddings provide
optimal cosine similarity performance for speaker matching.

Uses batch processing and numpy vectorized operations for efficiency.
"""

import logging
from contextlib import suppress
from typing import Any

import numpy as np

from app.core.celery import celery_app
from app.core.constants import CPUPriority
from app.core.constants import get_speaker_index
from app.services.opensearch_service import get_opensearch_client

logger = logging.getLogger(__name__)

# Batch size for scroll API
SCROLL_BATCH_SIZE = 500
SCROLL_TIMEOUT = "5m"

# Tolerance for checking if already normalized
NORM_TOLERANCE = 0.01


def _is_normalized(embedding: list[float]) -> bool:
    """Check if an embedding is already L2 normalized (norm ≈ 1.0)."""
    norm = float(np.linalg.norm(embedding))
    return abs(norm - 1.0) < NORM_TOLERANCE


def _normalize_embeddings_batch(embeddings: list[list[float]]) -> list[list[float]]:
    """Normalize a batch of embeddings using vectorized numpy operations."""
    if not embeddings:
        return []

    arr = np.array(embeddings, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms = np.where(norms > 0, norms, 1.0)
    normalized = arr / norms
    result: list[list[float]] = normalized.tolist()
    return result


def _process_hits_batch(
    hits: list[dict[str, Any]], summary: dict[str, Any]
) -> tuple[list[str], list[list[float]]]:
    """Process a batch of hits and return doc IDs and embeddings needing normalization."""
    embeddings_to_normalize: list[list[float]] = []
    doc_ids: list[str] = []

    for hit in hits:
        summary["total_found"] += 1
        doc_id = hit["_id"]
        embedding = hit.get("_source", {}).get("embedding")

        if embedding is None:
            continue

        if _is_normalized(embedding):
            summary["already_normalized"] += 1
            continue

        embeddings_to_normalize.append(embedding)
        doc_ids.append(doc_id)

    return doc_ids, embeddings_to_normalize


def _bulk_update_embeddings(
    client: Any,
    index_name: str,
    doc_ids: list[str],
    normalized_embeddings: list[list[float]],
    summary: dict[str, Any],
) -> None:
    """Bulk update embeddings in OpenSearch."""
    bulk_body: list[dict[str, Any]] = []
    for doc_id, normalized in zip(doc_ids, normalized_embeddings, strict=True):
        bulk_body.append({"update": {"_index": index_name, "_id": doc_id}})
        bulk_body.append({"doc": {"embedding": normalized}})

    if not bulk_body:
        return

    try:
        bulk_response = client.bulk(body=bulk_body, refresh=False)

        if bulk_response.get("errors"):
            for item in bulk_response.get("items", []):
                if "error" in item.get("update", {}):
                    summary["failed"] += 1
                    logger.error(f"Bulk update error: {item['update']['error']}")
                else:
                    summary["normalized"] += 1
        else:
            summary["normalized"] += len(normalized_embeddings)

    except Exception as e:
        logger.error(f"Bulk update failed: {e}")
        summary["failed"] += len(normalized_embeddings)


def _sample_check_normalized(client: Any, index_name: str, sample_size: int = 50) -> bool:
    """Check a random sample of embeddings to see if normalization is needed.

    Returns True if all sampled vectors are already L2-normalized,
    meaning a full scan can be skipped.
    """
    try:
        resp = client.search(
            index=index_name,
            body={
                "size": sample_size,
                "query": {"function_score": {"query": {"match_all": {}}, "random_score": {}}},
                "_source": ["embedding"],
            },
        )
        hits = resp.get("hits", {}).get("hits", [])
        if not hits:
            return True  # empty index — nothing to do

        for hit in hits:
            emb = hit.get("_source", {}).get("embedding")
            if emb and not _is_normalized(emb):
                return False
        return True
    except Exception as e:
        logger.warning("Sample normalization check failed for %s: %s", index_name, e)
        return False  # be safe — run full scan


def _normalize_index(
    client: Any,
    index_name: str,
    batch_size: int,
    summary: dict[str, Any],
) -> None:
    """Normalize all embeddings in a single OpenSearch index."""
    try:
        if not client.indices.exists(index=index_name):
            logger.info(f"Index {index_name} does not exist, skipping")
            return
    except Exception as e:
        logger.error(f"Error checking index {index_name}: {e}")
        return

    # Fast pre-check: sample random vectors. If all normalized, skip full scan.
    if _sample_check_normalized(client, index_name):
        logger.info(
            "Index %s: sample check passed — all embeddings already normalized, skipping full scan",
            index_name,
        )
        return

    logger.info("Index %s: unnormalized vectors detected, running full scan", index_name)

    scroll_id = None
    try:
        response = client.search(
            index=index_name,
            body={"size": batch_size, "query": {"match_all": {}}, "_source": ["embedding"]},
            scroll=SCROLL_TIMEOUT,
        )

        scroll_id = response.get("_scroll_id")
        hits = response.get("hits", {}).get("hits", [])

        while hits:
            summary["batches_processed"] += 1
            doc_ids, embeddings_to_normalize = _process_hits_batch(hits, summary)

            if embeddings_to_normalize:
                normalized = _normalize_embeddings_batch(embeddings_to_normalize)
                _bulk_update_embeddings(client, index_name, doc_ids, normalized, summary)

            if summary["batches_processed"] % 10 == 0:
                logger.info(
                    "Embedding normalization progress [%s]: %d checked, "
                    "%d normalized, %d already OK",
                    index_name,
                    summary["total_found"],
                    summary["normalized"],
                    summary["already_normalized"],
                )

            response = client.scroll(scroll_id=scroll_id, scroll=SCROLL_TIMEOUT)
            scroll_id = response.get("_scroll_id")
            hits = response.get("hits", {}).get("hits", [])

        try:
            client.indices.refresh(index=index_name)
        except Exception as e:
            logger.warning(f"Failed to refresh {index_name} after normalization: {e}")

    except Exception as e:
        logger.error(f"Error normalizing index {index_name}: {e}")
        summary.setdefault("errors", []).append(str(e))

    finally:
        if scroll_id:
            with suppress(Exception):
                client.clear_scroll(scroll_id=scroll_id)


@celery_app.task(name="migration.normalize_embeddings", bind=True, priority=CPUPriority.ADMIN_BATCH)
def normalize_speaker_embeddings_task(self, batch_size: int = SCROLL_BATCH_SIZE) -> dict[str, Any]:
    """Normalize all speaker/cluster/profile embeddings to L2 unit vectors.

    Scans the main speaker index AND the active speaker index (which
    holds cluster centroids). Idempotent — already-normalized vectors
    are skipped (tolerance 0.01).

    Runs automatically on startup via the FastAPI lifespan hook.
    """
    from app.core.redis import get_redis

    r = get_redis()
    if not r.set("normalize_embeddings_lock", "1", nx=True, ex=600):
        logger.info("Embedding normalization already running, skipping")
        return {"status": "already_running"}

    try:
        return _run_normalize_embeddings(batch_size)
    finally:
        r.delete("normalize_embeddings_lock")


def _run_normalize_embeddings(batch_size: int) -> dict[str, Any]:
    """Inner implementation of embedding normalization."""
    summary: dict[str, Any] = {
        "total_found": 0,
        "already_normalized": 0,
        "normalized": 0,
        "failed": 0,
        "batches_processed": 0,
    }

    client = get_opensearch_client()
    if not client:
        logger.warning("OpenSearch client not available, skipping embedding normalization")
        summary["error"] = "OpenSearch client not available"
        return summary

    # Collect all indices that may hold embeddings
    indices_to_scan: set[str] = set()
    indices_to_scan.add(get_speaker_index())
    try:
        from app.services.opensearch_service import get_active_speaker_index

        active = get_active_speaker_index()
        indices_to_scan.add(active)
    except Exception as e:
        logger.debug("Could not detect active speaker index: %s", e)

    logger.info("Starting embedding normalization across indices: %s", indices_to_scan)

    for index_name in indices_to_scan:
        _normalize_index(client, index_name, batch_size, summary)

    logger.info(
        "Speaker embedding normalization completed: "
        "%d total, %d normalized, %d already normalized, %d failed",
        summary["total_found"],
        summary["normalized"],
        summary["already_normalized"],
        summary["failed"],
    )
    return summary
