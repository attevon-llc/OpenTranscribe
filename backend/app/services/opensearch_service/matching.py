"""Speaker-to-speaker kNN matching against stored voiceprints."""

import logging
from typing import Any

from app.core.constants import get_speaker_index
from app.services.opensearch_service import client as _client
from app.services.opensearch_service.aliases import get_active_speaker_index
from app.services.opensearch_service.client import _is_index_corruption_error
from app.services.opensearch_service.client import _speaker_org_filter_clauses
from app.services.opensearch_service.indices import ensure_indices_exist
from app.services.opensearch_service.repair import _repair_index

logger = logging.getLogger(__name__)


def _extract_speaker_match(hit: dict, threshold: float) -> dict[str, Any] | None:
    """Convert a single OpenSearch kNN hit to a speaker match dict.

    Converts the OpenSearch cosinesimil score ``(1 + cosine) / 2`` back to
    raw cosine similarity so thresholds represent real cosine values.
    """
    cosine_sim = 2.0 * hit["_score"] - 1.0
    if cosine_sim < threshold:
        return None

    source = hit["_source"]
    if "speaker_id" not in source:
        logger.debug(f"Skipping profile document in speaker matching: {source.get('profile_id')}")
        return None

    return {
        "speaker_id": source["speaker_id"],
        "speaker_uuid": source.get("speaker_uuid"),
        "profile_id": source.get("profile_id"),
        "profile_uuid": source.get("profile_uuid"),
        "name": source["name"],
        "confidence": cosine_sim,
        "media_file_id": source.get("media_file_id"),
        "collection_ids": source.get("collection_ids", []),
    }


def find_matching_speaker(
    embedding: list[float],
    user_id: int,
    threshold: float = 0.5,
    collection_ids: list[int] | None = None,
    exclude_speaker_ids: list[int] | None = None,
    accessible_user_ids: list[int] | None = None,
    organization_id: int | None = None,
) -> dict[str, Any] | None:
    """
    Find a matching speaker for a given embedding with confidence score

    Args:
        embedding: Speaker embedding vector
        user_id: ID of the user
        threshold: Minimum similarity threshold (0-1) for matching
        collection_ids: Optional list of collection IDs to search within
        exclude_speaker_ids: Optional list of speaker IDs to exclude
        accessible_user_ids: Optional list of user IDs to search within
            (for shared profile scope). If None, filters by user_id.
        organization_id: Active org id (None = personal). Adds the default-deny
            tenant gate so cross-org voiceprints never match. Community-edition
            invariance: None + org-less docs => the personal gate is a no-op.

    Returns:
        Dictionary with speaker info and confidence if a match is found, None otherwise
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized, skipping speaker matching")
        return None

    try:
        # Ensure indices exist before searching
        ensure_indices_exist()

        # Build filter conditions - use accessible user IDs for shared profile scope
        filters: list[dict[str, Any]]
        if accessible_user_ids:
            filters = [{"terms": {"user_id": accessible_user_ids}}]
        else:
            filters = [{"term": {"user_id": user_id}}]

        # Tenant gate (org term when set, else exclude org-stamped voiceprints).
        filters.extend(_speaker_org_filter_clauses(organization_id))

        # Add collection filter if specified
        if collection_ids:
            filters.append({"terms": {"collection_ids": collection_ids}})

        # Add exclusion filter if specified
        if exclude_speaker_ids:
            filters.append({"bool": {"must_not": {"terms": {"speaker_id": exclude_speaker_ids}}}})

        # Build a kNN query to find similar speaker embeddings
        # Using the proper OpenSearch knn query syntax based on documentation
        query = {
            "size": 5,
            "query": {
                "knn": {
                    "embedding": {
                        "vector": embedding,
                        "k": 5,
                        "filter": {"bool": {"filter": filters}},
                    }
                }
            },
        }

        # Execute search
        response = _client.opensearch_client.search(index=get_speaker_index(), body=query)

        # Check if we have a match
        if len(response["hits"]["hits"]) > 0:
            match = _extract_speaker_match(response["hits"]["hits"][0], threshold)
            if match:
                return match

        # No match found or score below threshold
        return None

    except Exception as e:
        if _is_index_corruption_error(e):
            logger.warning(
                "Index corruption detected during speaker matching, attempting repair..."
            )
            if _repair_index(get_speaker_index()):
                try:
                    response = _client.opensearch_client.search(
                        index=get_speaker_index(), body=query
                    )
                    if len(response["hits"]["hits"]) > 0:
                        match = _extract_speaker_match(response["hits"]["hits"][0], threshold)
                        if match:
                            return match
                    return None
                except Exception as retry_err:
                    logger.error(f"Retry after repair failed for speaker matching: {retry_err}")
                    return None
        logger.error(f"Error finding matching speaker: {e}")
        return None


def batch_find_matching_speakers(
    embeddings: list[dict[str, Any]],
    user_id: int,
    threshold: float = 0.5,
    max_candidates: int = 5,
    organization_id: int | None = None,
) -> list[dict[str, Any]]:
    """
    Find matching speakers for multiple embeddings in a single query (efficient batch operation)

    Args:
        embeddings: List of dicts with 'id' and 'embedding' keys
        user_id: ID of the user
        threshold: Minimum similarity threshold
        max_candidates: Maximum candidates per embedding
        organization_id: Active org id (None = personal) — tenant gate.

    Returns:
        List of match results for each input embedding
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return []

    try:
        # Ensure indices exist before searching
        ensure_indices_exist()

        # Use multi-search for efficient batch processing
        msearch_body: list[dict[str, Any]] = []
        org_clauses = _speaker_org_filter_clauses(organization_id)

        for emb_data in embeddings:
            # Add search header
            msearch_body.append({"index": get_speaker_index()})

            # Add search query with self-exclusion
            query_body: dict[str, Any] = {
                "size": max_candidates,
                "query": {
                    "bool": {
                        "filter": [{"term": {"user_id": user_id}}, *org_clauses],
                        "must_not": [
                            {"term": {"speaker_id": emb_data["id"]}},  # Exclude self
                            {"exists": {"field": "document_type"}},  # Exclude profile documents
                        ],
                    }
                },
                "knn": {
                    "embedding": {
                        "vector": emb_data["embedding"],
                        "k": max_candidates,
                    }
                },
            }
            msearch_body.append(query_body)

        # Execute multi-search
        response = _client.opensearch_client.msearch(body=msearch_body)

        # Process results
        results = []
        for i, emb_data in enumerate(embeddings):
            search_response = response["responses"][i]

            matches = []
            if "hits" in search_response and search_response["hits"]["hits"]:
                for hit in search_response["hits"]["hits"]:
                    score = 2.0 * hit["_score"] - 1.0  # raw cosine
                    if score >= threshold:
                        source = hit["_source"]
                        matches.append(
                            {
                                "speaker_id": source["speaker_id"],
                                "speaker_uuid": source.get("speaker_uuid"),
                                "profile_id": source.get("profile_id"),
                                "profile_uuid": source.get("profile_uuid"),
                                "name": source["name"],
                                "confidence": score,
                                "media_file_id": source.get("media_file_id"),
                            }
                        )

            results.append({"input_id": emb_data["id"], "matches": matches})

        return results

    except Exception as e:
        logger.error(f"Error in batch speaker matching: {e}")
        return []


def msearch_speaker_similarities(
    speaker_data: list[dict],
    user_id: int,
    k: int = 10,
    batch_size: int = 50,
    organization_id: int | None = None,
) -> list[list[dict]]:
    """Batch kNN search for building a similarity graph.

    Args:
        speaker_data: List of dicts with speaker_uuid and embedding.
        user_id: Owner user ID.
        k: Number of nearest neighbors per query.
        batch_size: Number of speakers per msearch request.
        organization_id: Active org id (None = personal) — tenant gate.

    Returns:
        List of result lists, one per input embedding.
    """
    if not _client.opensearch_client or not speaker_data:
        return [[] for _ in speaker_data]

    try:
        import json

        active_index = get_active_speaker_index()
        all_results: list[list[dict]] = []
        org_clauses = _speaker_org_filter_clauses(organization_id)

        # Process in batches to avoid memory issues
        for batch_start in range(0, len(speaker_data), batch_size):
            batch = speaker_data[batch_start : batch_start + batch_size]

            # Build msearch body for this batch
            body_parts: list[str] = []
            for sd in batch:
                header = {"index": active_index}
                query = {
                    "size": k,
                    "query": {
                        "knn": {
                            "embedding": {
                                "vector": sd["embedding"],
                                "k": k,
                                "filter": {
                                    "bool": {
                                        "filter": [
                                            {"term": {"user_id": user_id}},
                                            {
                                                "bool": {
                                                    "must_not": [
                                                        {"exists": {"field": "document_type"}},
                                                    ]
                                                }
                                            },
                                            *org_clauses,
                                        ]
                                    }
                                },
                            }
                        }
                    },
                }
                body_parts.append(json.dumps(header))
                body_parts.append(json.dumps(query))

            msearch_body = "\n".join(body_parts) + "\n"
            response = _client.opensearch_client.msearch(body=msearch_body)

            for resp in response.get("responses", []):
                hits: list[dict] = []
                for hit in resp.get("hits", {}).get("hits", []):
                    source = hit.get("_source", {})
                    hits.append(
                        {
                            "speaker_uuid": source.get("speaker_uuid"),
                            "similarity": 2.0 * float(hit["_score"]) - 1.0,  # raw cosine
                            "speaker_id": source.get("speaker_id"),
                        }
                    )
                all_results.append(hits)

        return all_results

    except Exception as e:
        logger.error(f"Error in msearch speaker similarities: {e}")
        return [[] for _ in speaker_data]
