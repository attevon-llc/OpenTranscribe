"""Speaker-cluster centroid storage and cluster kNN matching."""

import datetime
import logging

from app.services.opensearch_service import client as _client
from app.services.opensearch_service.aliases import get_active_speaker_index
from app.services.opensearch_service.client import _speaker_org_filter_clauses
from app.services.opensearch_service.indices import ensure_indices_exist

logger = logging.getLogger(__name__)


# Flag to avoid redundant ensure_indices_exist() calls during batch operations.
_indices_verified = False


def store_cluster_embedding(
    cluster_uuid: str,
    user_id: int,
    embedding: list[float],
    label: str | None = None,
    refresh: str | bool = "wait_for",
    organization_id: int | None = None,
) -> bool:
    """Store a cluster centroid embedding in OpenSearch.

    Args:
        cluster_uuid: UUID of the speaker cluster.
        user_id: Owner user ID.
        embedding: L2-normalized centroid embedding vector.
        label: Optional cluster label.
        refresh: Index refresh policy. Use ``False`` during batch operations
            and issue a single ``indices.refresh()`` at the end.
        organization_id: Tenant scope of the cluster (None = personal). Only
            written for org clusters — personal/community docs carry no
            ``organization_id`` field, matching the per-file speaker docs.

    Returns:
        True if successful, False otherwise.
    """
    global _indices_verified

    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return False

    try:
        if not _indices_verified:
            ensure_indices_exist()
            _indices_verified = True
        active_index = get_active_speaker_index()

        doc = {
            "document_type": "cluster",
            "cluster_uuid": str(cluster_uuid),
            "user_id": user_id,
            "embedding": embedding,
            "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        }
        if label:
            doc["label"] = label
        if organization_id is not None:
            doc["organization_id"] = organization_id

        _client.opensearch_client.index(
            index=active_index,
            body=doc,
            id=f"cluster_{cluster_uuid}",
            refresh=refresh,
        )

        logger.info(f"Stored cluster {cluster_uuid} centroid in OpenSearch")
        return True

    except Exception as e:
        logger.error(f"Error storing cluster embedding: {e}")
        return False


def delete_cluster_embedding(cluster_uuid: str) -> bool:
    """Delete a cluster centroid from OpenSearch.

    Args:
        cluster_uuid: UUID of the speaker cluster.

    Returns:
        True if successful, False otherwise.
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return False

    try:
        active_index = get_active_speaker_index()
        _client.opensearch_client.delete(
            index=active_index,
            id=f"cluster_{cluster_uuid}",
        )
        logger.info(f"Removed cluster {cluster_uuid} embedding from OpenSearch")
        return True

    except Exception as e:
        logger.warning(f"Error removing cluster embedding (may not exist): {e}")
        return False


def find_matching_clusters(
    embedding: list[float],
    user_id: int,
    k: int = 5,
    threshold: float = 0.75,
    organization_id: int | None = None,
) -> list[dict]:
    """Find matching cluster centroids for a speaker embedding using kNN.

    Args:
        embedding: L2-normalized speaker embedding vector.
        user_id: Owner user ID.
        k: Number of nearest neighbors.
        threshold: Minimum cosine similarity.
        organization_id: Active org id (None = personal) — tenant gate.

    Returns:
        List of dicts with cluster_uuid, similarity, label.
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return []

    try:
        active_index = get_active_speaker_index()
        query = {
            "size": k,
            "query": {
                "knn": {
                    "embedding": {
                        "vector": embedding,
                        "k": k,
                        "filter": {
                            "bool": {
                                "filter": [
                                    {"term": {"document_type": "cluster"}},
                                    {"term": {"user_id": user_id}},
                                    *_speaker_org_filter_clauses(organization_id),
                                ]
                            }
                        },
                    }
                }
            },
        }

        response = _client.opensearch_client.search(index=active_index, body=query)

        matches = []
        for hit in response["hits"]["hits"]:
            score = hit["_score"]
            # OpenSearch Lucene engine with cosinesimil space returns:
            #   score = (1 + cosine_similarity) / 2
            # Convert back to raw cosine similarity for threshold comparison.
            cosine_sim = 2.0 * score - 1.0
            if cosine_sim < threshold:
                continue
            source = hit["_source"]
            matches.append(
                {
                    "cluster_uuid": source.get("cluster_uuid"),
                    "similarity": float(cosine_sim),
                    "label": source.get("label"),
                }
            )

        return matches

    except Exception as e:
        logger.error(f"Error finding matching clusters: {e}")
        return []


def update_cluster_embedding(
    cluster_uuid: str,
    embedding: list[float],
    label: str | None = None,
) -> bool:
    """Update an existing cluster centroid embedding in OpenSearch.

    Re-indexes the full document (same as store) so that the embedding vector
    is replaced atomically.

    Args:
        cluster_uuid: UUID of the cluster.
        embedding: Updated centroid embedding vector.
        label: Optional updated label for the cluster.

    Returns:
        True if successful, False otherwise.
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return False

    try:
        ensure_indices_exist()
        active_index = get_active_speaker_index()

        # Fetch existing document to preserve user_id
        doc_id = f"cluster_{cluster_uuid}"
        try:
            existing = _client.opensearch_client.get(index=active_index, id=doc_id)
            user_id = existing["_source"].get("user_id")
            existing_label = existing["_source"].get("label")
        except Exception:
            logger.error(f"Cannot update cluster {cluster_uuid}: document not found")
            return False

        doc = {
            "document_type": "cluster",
            "cluster_uuid": str(cluster_uuid),
            "user_id": user_id,
            "embedding": embedding,
            "label": label if label is not None else existing_label,
            "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        }

        _client.opensearch_client.index(
            index=active_index,
            body=doc,
            id=doc_id,
            refresh="wait_for",
        )

        logger.info(f"Updated cluster {cluster_uuid} embedding in OpenSearch")
        return True

    except Exception as e:
        logger.error(f"Error updating cluster embedding for {cluster_uuid}: {e}")
        return False
