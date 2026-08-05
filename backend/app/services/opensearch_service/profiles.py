"""Speaker-profile embedding storage and profile kNN matching."""

import datetime
import logging
from typing import Any

from opensearchpy.exceptions import NotFoundError

from app.core.constants import get_speaker_index
from app.core.constants import get_speaker_index_v4
from app.services.opensearch_service import client as _client
from app.services.opensearch_service.client import CLUSTER_UNAVAILABLE_ERRORS
from app.services.opensearch_service.client import OpenSearchUnavailableError
from app.services.opensearch_service.client import _safe_index_exists
from app.services.opensearch_service.client import _speaker_org_filter_clauses
from app.services.opensearch_service.indices import ensure_indices_exist

logger = logging.getLogger(__name__)


def store_profile_embedding_v4(
    profile_id: int,
    profile_uuid: str,
    profile_name: str,
    embedding: list[float],
    speaker_count: int,
    user_id: int,
    organization_id: int | None = None,
) -> bool:
    """Store consolidated profile embedding in the v4 staging index.

    Same document structure as store_profile_embedding() but targets
    speakers_v4 index for migration pre-population.

    Args:
        profile_id: ID of the speaker profile.
        profile_uuid: UUID of the speaker profile (used as document ID).
        profile_name: Name of the speaker profile.
        embedding: Averaged 256-dim embedding vector.
        speaker_count: Number of speakers contributing to this embedding.
        user_id: ID of the user who owns the profile.

    Returns:
        True if successful, False otherwise.
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return False

    v4_index = get_speaker_index_v4()

    try:
        doc = {
            "document_type": "profile",
            "profile_id": profile_id,
            "profile_uuid": str(profile_uuid),
            "profile_name": profile_name,
            "user_id": user_id,
            "embedding": embedding,
            "speaker_count": speaker_count,
            "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        }
        if organization_id is not None:
            doc["organization_id"] = organization_id

        _client.opensearch_client.index(
            index=v4_index,
            body=doc,
            id=f"profile_{profile_uuid}",
            refresh="wait_for",
        )

        logger.info(
            f"Stored v4 profile {profile_uuid} ({profile_name}) embedding "
            f"with {speaker_count} speakers"
        )
        return True

    except Exception as e:
        logger.error(f"Error storing v4 profile embedding for {profile_uuid}: {e}")
        return False


def msearch_profile_knn_batch(
    speaker_embeddings: dict[str, list[float]],
    user_id: int,
    threshold: float = 0.5,
    k: int = 10,
    accessible_profile_ids: set[int] | None = None,
    organization_id: int | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Batch kNN search for profile matches across multiple speakers.

    Executes a single msearch request containing one kNN query per speaker,
    each searching for matching profile documents.

    Args:
        speaker_embeddings: Dict mapping speaker_uuid -> embedding vector.
        user_id: Owner user ID for filtering.
        threshold: Minimum raw cosine similarity to include.
        k: Number of nearest neighbors per query.
        accessible_profile_ids: Optional set of profile IDs to restrict search.
        organization_id: Active org id (None = personal) — tenant gate.

    Returns:
        Dict mapping speaker_uuid -> list of profile match dicts.
    """
    if not _client.opensearch_client or not speaker_embeddings:
        return {uid: [] for uid in speaker_embeddings}

    try:
        ensure_indices_exist()
        speaker_index = get_speaker_index()

        # Build filter (same for all queries)
        if accessible_profile_ids is not None:
            must_filters: list[dict[str, Any]] = [
                {"term": {"document_type": "profile"}},
                {"terms": {"profile_id": list(accessible_profile_ids)}},
            ]
        else:
            must_filters = [
                {"term": {"document_type": "profile"}},
                {"term": {"user_id": user_id}},
            ]
        # Tenant gate on profile documents (org term, else exclude org-stamped).
        must_filters.extend(_speaker_org_filter_clauses(organization_id))

        # Build msearch body
        msearch_body: list[dict[str, Any]] = []
        uuid_order: list[str] = []

        for speaker_uuid, embedding in speaker_embeddings.items():
            uuid_order.append(speaker_uuid)
            msearch_body.append({"index": speaker_index})
            msearch_body.append(
                {
                    "size": k,
                    "query": {
                        "knn": {
                            "embedding": {
                                "vector": embedding,
                                "k": k,
                                "filter": {"bool": {"must": must_filters}},
                            }
                        }
                    },
                }
            )

        response = _client.opensearch_client.msearch(body=msearch_body)

        results: dict[str, list[dict[str, Any]]] = {}
        for i, speaker_uuid in enumerate(uuid_order):
            matches: list[dict[str, Any]] = []
            resp = response["responses"][i]
            for hit in resp.get("hits", {}).get("hits", []):
                score = 2.0 * hit["_score"] - 1.0  # Convert OS cosinesimil to raw cosine
                if score >= threshold:
                    source = hit["_source"]
                    matches.append(
                        {
                            "profile_id": source.get("profile_id"),
                            "profile_name": source.get("profile_name"),
                            "speaker_count": source.get("speaker_count"),
                            "similarity": score,
                        }
                    )
            results[speaker_uuid] = matches

        return results

    except Exception as e:
        logger.error(f"Error in batch profile kNN search: {e}")
        return {uid: [] for uid in speaker_embeddings}


def get_profile_embedding(profile_uuid: str) -> list[float] | None:
    """
    Get the embedding vector for a speaker profile from OpenSearch

    Args:
        profile_uuid: UUID of the speaker profile

    Returns:
        Embedding vector or None if not found
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return None

    try:
        # Ensure indices exist before searching
        ensure_indices_exist()

        # Use UUID-based document ID for profiles
        response = _client.opensearch_client.get(
            index=get_speaker_index(), id=f"profile_{profile_uuid}"
        )

        if response and "_source" in response:
            embedding = response["_source"].get("embedding")
            if embedding is not None:
                return list(embedding)  # Explicit conversion to list[float]
            return None

        return None

    except Exception as e:
        logger.error(f"Error getting profile embedding: {e}")
        return None


def store_profile_embedding(
    profile_id: int,
    profile_uuid: str,
    profile_name: str,
    embedding: list[float],
    speaker_count: int,
    user_id: int,
    organization_id: int | None = None,
) -> bool:
    """
    Store profile embedding with distinct document type for proper filtering.

    Args:
        profile_id: ID of the speaker profile (for internal queries)
        profile_uuid: UUID of the speaker profile (used as document ID)
        profile_name: Name of the speaker profile
        embedding: Embedding vector
        speaker_count: Number of speakers contributing to this embedding
        user_id: ID of the user who owns the profile
        organization_id: Active org id (None = personal). Only written for org
            profiles so personal docs stay org-less (matches the search gate).

    Returns:
        True if successful, False otherwise
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return False

    try:
        ensure_indices_exist()

        doc = {
            "document_type": "profile",  # CRITICAL: Distinguish from speakers
            "profile_id": profile_id,
            "profile_uuid": str(profile_uuid),
            "profile_name": profile_name,
            "user_id": user_id,
            "embedding": embedding,
            "speaker_count": speaker_count,
            "updated_at": datetime.datetime.now(datetime.UTC).isoformat(),
        }
        if organization_id is not None:
            doc["organization_id"] = organization_id

        # Use UUID-based prefixed ID to avoid conflicts with speaker documents
        # Use refresh='wait_for' to ensure the update is immediately searchable
        # This prevents race conditions where voice_suggestions show stale profile names
        _client.opensearch_client.index(
            index=get_speaker_index(),
            body=doc,
            id=f"profile_{profile_uuid}",
            refresh="wait_for",
        )

        logger.info(
            f"Stored profile {profile_uuid} ({profile_name}) embedding in OpenSearch with {speaker_count} speakers"
        )
        return True

    except Exception as e:
        logger.error(f"Error storing profile embedding: {e}")
        return False


def remove_profile_embedding(profile_uuid: str) -> bool:
    """Remove a profile embedding from all speaker indices (main + v4 staging).

    Profiles are stored with doc ID ``profile_{uuid}`` in both the main index
    and the v4 staging index. Both are cleaned here.

    Args:
        profile_uuid: UUID of the speaker profile

    Returns:
        True if the main index deletion succeeded, False otherwise
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return False

    doc_id = f"profile_{profile_uuid}"
    success = False
    try:
        _client.opensearch_client.delete(index=get_speaker_index(), id=doc_id)
        logger.info(f"Removed profile {profile_uuid} embedding from main index")
        success = True
    except NotFoundError:
        logger.debug(f"Profile {profile_uuid} not present in the main speaker index")
    except CLUSTER_UNAVAILABLE_ERRORS as e:
        logger.warning(f"Error removing profile {profile_uuid} embedding from main index: {e}")

    # Also clean the v4 staging index. Best-effort by design: the staging index
    # is rebuilt by the migration task, so a stale doc there is self-correcting
    # and must not fail the caller's main-index delete.
    try:
        v4_index = get_speaker_index_v4()
        if _safe_index_exists(v4_index):
            _client.opensearch_client.delete(index=v4_index, id=doc_id)
            logger.debug(f"Removed profile {profile_uuid} from v4 staging index")
    except NotFoundError:
        logger.debug(f"Profile {profile_uuid} not present in the v4 staging index")
    except (OpenSearchUnavailableError, *CLUSTER_UNAVAILABLE_ERRORS) as e:
        logger.warning(f"Could not clean profile {profile_uuid} from the v4 staging index: {e}")

    return success


def find_matching_profiles(
    embedding: list[float],
    user_id: int,
    threshold: float = 0.7,
    size: int = 5,
    organization_id: int | None = None,
) -> list[dict[str, Any]]:
    """
    Find matching speaker profiles using embedding similarity in OpenSearch.

    Args:
        embedding: Query embedding vector
        user_id: User ID to filter results
        threshold: Minimum similarity threshold
        size: Maximum number of results
        organization_id: Active org id (None = personal) — tenant gate.

    Returns:
        List of matching profiles with similarity scores
    """
    if not _client.opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return []

    try:
        # Ensure indices exist before searching
        ensure_indices_exist()

        # KNN search query for profile embeddings
        query = {
            "size": size,
            "query": {
                "knn": {
                    "embedding": {
                        "vector": embedding,
                        "k": size,
                        "filter": {
                            "bool": {
                                "must": [
                                    {"term": {"user_id": user_id}},
                                    {"term": {"document_type": "profile"}},
                                    *_speaker_org_filter_clauses(organization_id),
                                ]
                            }
                        },
                    }
                }
            },
            "_source": ["profile_id", "profile_name", "embedding_count", "updated_at"],
        }

        response = _client.opensearch_client.search(index=get_speaker_index(), body=query)

        matches = []
        for hit in response["hits"]["hits"]:
            score = 2.0 * hit["_score"] - 1.0  # raw cosine
            if score >= threshold:
                source = hit["_source"]
                matches.append(
                    {
                        "profile_id": source["profile_id"],
                        "profile_name": source["profile_name"],
                        "similarity": score,
                        "embedding_count": source["embedding_count"],
                        "last_update": source.get("updated_at"),
                    }
                )

        logger.info(f"Found {len(matches)} profile matches above threshold {threshold}")
        return matches

    except Exception as e:
        logger.error(f"Error finding matching profiles: {e}")
        return []
