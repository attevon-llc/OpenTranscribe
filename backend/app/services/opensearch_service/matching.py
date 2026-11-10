"""Speaker-to-speaker kNN matching against stored voiceprints."""

import logging
from typing import Any

from opensearchpy.exceptions import OpenSearchException

from app.core.config import settings
from app.core.constants import SPEAKER_CONFIDENCE_MEDIUM
from app.services.opensearch_service import client as _client
from app.services.opensearch_service.aliases import get_active_speaker_index
from app.services.opensearch_service.client import OpenSearchUnavailableError
from app.services.opensearch_service.client import _is_index_corruption_error
from app.services.opensearch_service.client import _speaker_org_filter_clauses
from app.services.opensearch_service.client import query_embedding_dimension_mismatch
from app.services.opensearch_service.indices import ensure_indices_exist
from app.services.opensearch_service.repair import _repair_index
from app.services.search.availability_field import exclude_unavailable_clause
from app.utils.cosine_space import raw_cosine_from_opensearch_score

logger = logging.getLogger(__name__)


def _extract_speaker_match(hit: dict, threshold: float) -> dict[str, Any] | None:
    """Convert a single OpenSearch kNN hit to a speaker match dict.

    Converts the OpenSearch cosinesimil score ``(1 + cosine) / 2`` back to
    raw cosine similarity so thresholds represent real cosine values.
    """
    cosine_sim = raw_cosine_from_opensearch_score(hit["_score"])
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


def _top_hit_match(response: dict[str, Any], threshold: float) -> dict[str, Any] | None:
    """The best hit as a match, or None when there is none above ``threshold``."""
    hits = response["hits"]["hits"]
    return _extract_speaker_match(hits[0], threshold) if hits else None


def find_matching_speaker(
    embedding: list[float],
    user_id: int,
    threshold: float = SPEAKER_CONFIDENCE_MEDIUM,
    collection_ids: list[int] | None = None,
    exclude_speaker_ids: list[int] | None = None,
    accessible_user_ids: list[int] | None = None,
    organization_id: int | None = None,
    *,
    strict: bool = False,
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
        strict: Raise instead of returning None when the cluster could not answer.
            Matching must pass it: "no match" stores the voiceprint as a new voice.

    Returns:
        Dictionary with speaker info and confidence if a match is found, None otherwise

    Raises:
        OpenSearchUnavailableError: ``strict`` and the search failed.
    """
    if not _client.opensearch_client:
        if strict and settings.OPENSEARCH_ENABLED:
            raise OpenSearchUnavailableError("OpenSearch client unavailable")
        logger.warning("OpenSearch client not initialized, skipping speaker matching")
        return None

    try:
        # Ensure indices exist before searching
        ensure_indices_exist()

        active_index = get_active_speaker_index()
        if query_embedding_dimension_mismatch(active_index, embedding):
            # A dimension mismatch means this is a stale query embedding
            # against a mid-migration index; a real query will simply have
            # come from the wrong model. Fail closed (no match) but visibly.
            return None

        # Build filter conditions - use accessible user IDs for shared profile scope
        filters: list[dict[str, Any]]
        if accessible_user_ids:
            filters = [{"terms": {"user_id": accessible_user_ids}}]
        else:
            filters = [{"term": {"user_id": user_id}}]

        # Tenant gate (org term when set, else exclude org-stamped voiceprints).
        filters.extend(
            _speaker_org_filter_clauses(organization_id, user_ids=accessible_user_ids or [user_id])
        )

        # A trashed file's voices are never matched (#1101).
        filters.append(exclude_unavailable_clause())

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
        response = _client.opensearch_client.search(index=get_active_speaker_index(), body=query)
        return _top_hit_match(response, threshold)

    except Exception as e:
        if _is_index_corruption_error(e):
            logger.warning(
                "Index corruption detected during speaker matching, attempting repair..."
            )
            if _repair_index(get_active_speaker_index()):
                try:
                    response = _client.opensearch_client.search(
                        index=get_active_speaker_index(), body=query
                    )
                    return _top_hit_match(response, threshold)
                except Exception as retry_err:
                    logger.error(f"Retry after repair failed for speaker matching: {retry_err}")
                    if strict:
                        raise OpenSearchUnavailableError(
                            "retry after refreshing the index also failed"
                        ) from retry_err
                    return None
        logger.error(f"Error finding matching speaker: {e}")
        if strict:
            raise OpenSearchUnavailableError("speaker kNN failed") from e
        return None


def msearch_speaker_similarities(
    speaker_data: list[dict],
    user_id: int,
    k: int = 10,
    batch_size: int = 50,
    organization_id: int | None = None,
    *,
    strict: bool = False,
) -> list[list[dict]]:
    """Batch kNN search for building a similarity graph.

    Args:
        speaker_data: List of dicts with speaker_uuid and embedding.
        user_id: Owner user ID.
        k: Number of nearest neighbors per query.
        batch_size: Number of speakers per msearch request.
        organization_id: Active org id (None = personal) — tenant gate.
        strict: Raise instead of returning empty lists when the cluster could
            not answer, so "no neighbours" and "could not look" differ.

    Returns:
        List of result lists, one per input embedding.

    Raises:
        OpenSearchUnavailableError: ``strict`` and the search failed.
    """
    if not _client.opensearch_client or not speaker_data:
        if strict and speaker_data and settings.OPENSEARCH_ENABLED:
            raise OpenSearchUnavailableError("OpenSearch client unavailable")
        return [[] for _ in speaker_data]

    try:
        import json

        active_index = get_active_speaker_index()
        first_embedding: list[float] | None = (
            speaker_data[0].get("embedding") if speaker_data else None
        )
        if first_embedding and query_embedding_dimension_mismatch(active_index, first_embedding):
            return [[] for _ in speaker_data]
        all_results: list[list[dict]] = []
        org_clauses = _speaker_org_filter_clauses(organization_id, user_ids=[user_id])

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
                                            exclude_unavailable_clause(),
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
                            "similarity": raw_cosine_from_opensearch_score(hit["_score"]),
                            "speaker_id": source.get("speaker_id"),
                        }
                    )
                all_results.append(hits)

        return all_results

    except OpenSearchException as e:
        logger.exception(f"Error in msearch speaker similarities: {e}")
        if strict:
            raise OpenSearchUnavailableError("speaker similarity search failed") from e
        return [[] for _ in speaker_data]
