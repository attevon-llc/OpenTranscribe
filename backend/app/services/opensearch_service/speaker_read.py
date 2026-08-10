"""Reads of stored speaker embeddings and speaker documents."""

import logging
from collections.abc import Generator
from typing import Any

from app.core.constants import get_speaker_index
from app.services.opensearch_service import client as _client
from app.services.opensearch_service.aliases import get_active_speaker_index
from app.services.opensearch_service.client import _is_index_corruption_error
from app.services.opensearch_service.indices import ensure_indices_exist
from app.services.opensearch_service.repair import _repair_index

logger = logging.getLogger(__name__)


def get_speaker_document(speaker_uuid: str) -> dict[str, Any] | None:
    """
    Get full speaker document (embedding + segment_count) from OpenSearch.

    Args:
        speaker_uuid: UUID of the speaker

    Returns:
        Dict with 'embedding' and 'segment_count' keys, or None if not found
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return None

    try:
        ensure_indices_exist()

        response = _client.opensearch_client.get(index=get_speaker_index(), id=str(speaker_uuid))

        if response and "_source" in response:
            source = response["_source"]
            embedding = source.get("embedding")
            if embedding is not None:
                return {
                    "embedding": list(embedding),
                    "segment_count": int(source.get("segment_count", 1)),
                }
            return None

        return None

    except Exception as e:
        logger.error(f"Error getting speaker document for {speaker_uuid}: {e}")
        return None


def get_speaker_embedding(speaker_uuid: str) -> list[float] | None:
    """Get the embedding vector for a speaker from the active index.

    Queries only the active speaker index (v3 or v4, whichever holds the
    bulk of embeddings).  No cross-index fallback — this guarantees all
    returned embeddings have the same dimensionality.

    Args:
        speaker_uuid: UUID of the speaker

    Returns:
        Embedding vector or None if not found
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return None

    try:
        ensure_indices_exist()

        active_index = get_active_speaker_index()
        response = _client.opensearch_client.get(index=active_index, id=str(speaker_uuid))

        if response and "_source" in response:
            embedding = response["_source"].get("embedding")
            if embedding is not None:
                return list(embedding)

        return None

    except Exception as e:
        if _is_index_corruption_error(e):
            logger.warning(
                f"Index corruption detected getting speaker {speaker_uuid}, attempting repair..."
            )
            active_index = get_active_speaker_index()
            if _repair_index(active_index):
                try:
                    response = _client.opensearch_client.get(
                        index=active_index, id=str(speaker_uuid)
                    )
                    if response and "_source" in response:
                        embedding = response["_source"].get("embedding")
                        if embedding is not None:
                            return list(embedding)
                except Exception as retry_err:
                    logger.error(
                        f"Retry after repair failed for speaker {speaker_uuid}: {retry_err}"
                    )
            return None
        # NotFoundError is expected for speakers not in active index
        if "NotFoundError" not in type(e).__name__:
            logger.error(f"Error getting speaker embedding: {e}")
        return None


def get_speaker_embeddings_batch(speaker_uuids: list[str]) -> dict[str, list[float]]:
    """Get embeddings for multiple speakers in a single mget request.

    Args:
        speaker_uuids: List of speaker UUIDs

    Returns:
        Dict mapping speaker_uuid -> embedding vector (only for found speakers)
    """
    if not _client.opensearch_client or not speaker_uuids:
        return {}

    try:
        ensure_indices_exist()
        active_index = get_active_speaker_index()

        body = {"docs": [{"_index": active_index, "_id": str(uid)} for uid in speaker_uuids]}
        response = _client.opensearch_client.mget(body=body)

        results: dict[str, list[float]] = {}
        for doc in response.get("docs", []):
            if doc.get("found") and "_source" in doc:
                embedding = doc["_source"].get("embedding")
                if embedding is not None:
                    results[doc["_id"]] = list(embedding)
        return results

    except Exception as e:
        logger.error(f"Error batch-fetching speaker embeddings: {e}")
        return {}


def iter_speaker_embeddings(
    user_id: int,
    speaker_uuids: list[str] | None = None,
    batch_size: int = 200,
) -> Generator[list[dict[str, Any]], None, None]:
    """Yield batches of speaker embeddings from the active index.

    If *speaker_uuids* is provided, only those speakers are fetched (via
    mget).  Otherwise all non-cluster speaker docs for *user_id* are
    scrolled.  Embeddings are never accumulated — each batch is yielded
    and can be discarded by the caller.

    Yields:
        Lists of dicts with keys: speaker_uuid, embedding, speaker_id,
        profile_id, display_name.
    """
    if not _client.opensearch_client:
        return

    active_index = get_active_speaker_index()

    if speaker_uuids is not None:
        # Fetch specific speakers via mget in batches
        for i in range(0, len(speaker_uuids), batch_size):
            chunk = speaker_uuids[i : i + batch_size]
            try:
                response = _client.opensearch_client.mget(
                    index=active_index,
                    body={"ids": chunk},
                )
                batch: list[dict[str, Any]] = []
                for doc in response.get("docs", []):
                    if doc.get("found") and "_source" in doc:
                        source = doc["_source"]
                        if "embedding" in source and "speaker_uuid" in source:
                            batch.append(
                                {
                                    "speaker_uuid": source["speaker_uuid"],
                                    "embedding": source["embedding"],
                                    "speaker_id": source.get("speaker_id"),
                                    "profile_id": source.get("profile_id"),
                                    "display_name": source.get("display_name"),
                                }
                            )
                if batch:
                    yield batch
            except Exception as e:
                logger.warning("mget batch failed: %s", e)
                continue
    else:
        # Scroll all speakers for user
        search_after = None
        while True:
            query: dict = {
                "size": batch_size,
                "query": {
                    "bool": {
                        "filter": [{"term": {"user_id": user_id}}],
                        "must_not": [{"exists": {"field": "document_type"}}],
                    }
                },
                "sort": [{"_id": "asc"}],
                "_source": [
                    "speaker_uuid",
                    "embedding",
                    "speaker_id",
                    "profile_id",
                    "display_name",
                ],
            }
            if search_after is not None:
                query["search_after"] = search_after

            try:
                response = _client.opensearch_client.search(index=active_index, body=query)
            except Exception as e:
                logger.error("Scroll failed: %s", e)
                break

            hits = response["hits"]["hits"]
            if not hits:
                break

            batch = []
            for hit in hits:
                source = hit["_source"]
                if "embedding" in source and "speaker_uuid" in source:
                    batch.append(
                        {
                            "speaker_uuid": source["speaker_uuid"],
                            "embedding": source["embedding"],
                            "speaker_id": source.get("speaker_id"),
                            "profile_id": source.get("profile_id"),
                            "display_name": source.get("display_name"),
                        }
                    )
            if batch:
                yield batch

            search_after = hits[-1]["sort"]
            if len(hits) < batch_size:
                break


def get_all_speaker_embeddings(
    user_id: int,
    page_size: int = 500,
) -> list[dict]:
    """Fetch all speaker embeddings for a user from OpenSearch.

    Uses search_after pagination to handle arbitrarily large result sets
    instead of a hard limit.

    Args:
        user_id: Owner user ID.
        page_size: Number of documents per page (scroll batch size).

    Returns:
        List of dicts with speaker_uuid and embedding.
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return []

    try:
        active_index = get_active_speaker_index()
        results: list[dict] = []
        search_after = None

        while True:
            query: dict = {
                "size": page_size,
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"user_id": user_id}},
                        ],
                        "must_not": [
                            {"exists": {"field": "document_type"}},
                        ],
                    }
                },
                "sort": [{"_id": "asc"}],
                "_source": [
                    "speaker_uuid",
                    "embedding",
                    "speaker_id",
                    "profile_id",
                    "display_name",
                ],
            }

            if search_after is not None:
                query["search_after"] = search_after

            response = _client.opensearch_client.search(index=active_index, body=query)

            hits = response["hits"]["hits"]
            if not hits:
                break

            for hit in hits:
                source = hit["_source"]
                if "embedding" in source and "speaker_uuid" in source:
                    results.append(
                        {
                            "speaker_uuid": source["speaker_uuid"],
                            "embedding": source["embedding"],
                            "speaker_id": source.get("speaker_id"),
                            "profile_id": source.get("profile_id"),
                            "display_name": source.get("display_name"),
                        }
                    )

            # Use the sort value of the last hit for search_after
            search_after = hits[-1]["sort"]

            # If we got fewer results than page_size, we're done
            if len(hits) < page_size:
                break

        logger.info(
            f"Fetched {len(results)} speaker embeddings for user {user_id} "
            f"from index '{active_index}'"
        )
        return results

    except Exception as e:
        logger.error(f"Error fetching speaker embeddings: {e}")
        return []
