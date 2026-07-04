"""
Profile Embedding Service for managing speaker profile embeddings.

This service handles the consolidation and management of speaker embeddings
at the profile level to improve cross-file speaker recognition accuracy.

The service provides intelligent embedding aggregation that:
- Consolidates multiple speaker embeddings into profile-level representations
- Enables accurate cross-video speaker recognition
- Maintains embedding quality through incremental updates
- Supports both PostgreSQL metadata and OpenSearch vector storage
"""

import logging
from datetime import UTC
from datetime import datetime
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from app.models.media import Speaker
from app.models.media import SpeakerProfile
from app.services.opensearch_service import get_speaker_embedding

logger = logging.getLogger(__name__)


def _clear_profile_embedding_from_opensearch(profile_id: int) -> None:
    """Clear a profile embedding from OpenSearch."""
    try:
        from app.services.opensearch_service import remove_profile_embedding

        remove_profile_embedding(str(profile_id))
    except Exception as e:
        logger.warning(f"Could not clear profile {profile_id} embedding from OpenSearch: {e}")


def _store_profile_embedding_to_opensearch(
    profile_id: int,
    profile_uuid: str,
    profile_name: str,
    embedding: list[float],
    speaker_count: int,
    user_id: int,
    organization_id: int | None = None,
) -> None:
    """Store a profile embedding to OpenSearch (org-stamped for org profiles)."""
    try:
        from app.services.opensearch_service import store_profile_embedding

        store_profile_embedding(
            profile_id=profile_id,
            profile_uuid=profile_uuid,
            profile_name=profile_name,
            embedding=embedding,
            speaker_count=speaker_count,
            user_id=user_id,
            organization_id=organization_id,
        )
    except Exception as e:
        logger.warning(f"Failed to sync profile {profile_id} embedding to OpenSearch: {e}")


def _collect_speaker_embeddings(speakers: list[Speaker]) -> dict[int, list[float]]:
    """Collect embeddings for all speakers, keyed by speaker ID.

    Uses batch fetch to avoid N+1 OpenSearch calls per speaker.
    """
    from app.services.opensearch_service import get_speaker_embeddings_batch

    uuid_to_id = {str(s.uuid): int(s.id) for s in speakers}
    batch_result = get_speaker_embeddings_batch(list(uuid_to_id.keys()))
    return {uuid_to_id[uuid]: emb for uuid, emb in batch_result.items() if uuid in uuid_to_id}


def _calculate_average_embedding(embeddings: list[list[float]]) -> list[float]:
    """Calculate the L2-normalized average of multiple embeddings.

    Averaging L2-normalized vectors produces a non-unit vector. Re-normalizing
    ensures consistent cosine similarity when stored alongside per-speaker
    embeddings that are always L2-normalized.
    """
    embeddings_array = np.array(embeddings)
    avg_array = np.mean(embeddings_array, axis=0)
    norm = np.linalg.norm(avg_array)
    if norm > 1e-8:
        avg_array = avg_array / norm
    result: list[float] = avg_array.tolist()
    return result


def _process_profile_with_no_speakers(
    profile: SpeakerProfile,
    profile_id: int,
) -> bool:
    """Handle case when profile has no speakers assigned."""
    profile.embedding_count = 0  # type: ignore[assignment]
    profile.last_embedding_update = datetime.now(UTC)  # type: ignore[assignment]
    _clear_profile_embedding_from_opensearch(profile_id)
    return True


def _process_profile_with_embeddings(
    profile: SpeakerProfile,
    profile_id: int,
    embeddings: list[list[float]],
) -> bool:
    """Process a profile that has valid embeddings."""
    averaged_embedding = _calculate_average_embedding(embeddings)

    profile.embedding_count = len(embeddings)  # type: ignore[assignment]
    profile.last_embedding_update = datetime.now(UTC)  # type: ignore[assignment]

    _store_profile_embedding_to_opensearch(
        profile_id=profile_id,
        profile_uuid=str(profile.uuid),
        profile_name=str(profile.name),
        embedding=averaged_embedding,
        speaker_count=len(embeddings),
        user_id=int(profile.user_id),
        # Full-recalc must preserve the tenant stamp, or an org profile's doc
        # would silently fall back to personal scope until the next backfill.
        organization_id=profile.organization_id,
    )

    logger.info(f"Updated profile {profile_id} embedding with {len(embeddings)} speaker embeddings")
    return True


def _check_opensearch_profile_prerequisites(
    user_id: int,
) -> tuple[Any, Any, bool]:
    """
    Check prerequisites for OpenSearch profile similarity search.

    Returns:
        Tuple of (opensearch_client, None, should_continue)
    """
    from app.core.constants import get_speaker_index
    from app.services.opensearch_service import opensearch_client

    if not opensearch_client:
        logger.warning("OpenSearch client not initialized")
        return None, None, False

    if not opensearch_client.indices.exists(index=get_speaker_index()):
        logger.info("Speakers index does not exist yet, skipping profile similarity search")
        return None, None, False

    return opensearch_client, None, True


def _check_profiles_exist_in_opensearch(
    opensearch_client: Any,
    index_name: str,
    user_id: int,
    accessible_profile_ids: set[int] | None = None,
) -> bool:
    """Check if any profile documents exist for the user/scope in OpenSearch."""
    from app.services.opensearch_service import _is_index_corruption_error
    from app.services.opensearch_service import _repair_index

    # Build filter based on scope
    user_filter: dict[str, Any]
    if accessible_profile_ids is not None:
        user_filter = {"terms": {"profile_id": list(accessible_profile_ids)}}
    else:
        user_filter = {"term": {"user_id": user_id}}

    profile_check_query = {
        "size": 1,
        "query": {
            "bool": {
                "filter": [
                    {"term": {"document_type": "profile"}},
                    user_filter,
                ]
            }
        },
    }

    try:
        profile_check = opensearch_client.search(index=index_name, body=profile_check_query)
        if profile_check["hits"]["total"]["value"] == 0:
            logger.info(f"No profile documents found for user/scope {user_id}, skipping KNN search")
            return False
    except Exception as e:
        if _is_index_corruption_error(e):
            logger.warning(f"Index corruption during profile check, attempting repair: {e}")
            if _repair_index(index_name):
                try:
                    profile_check = opensearch_client.search(
                        index=index_name, body=profile_check_query
                    )
                    if profile_check["hits"]["total"]["value"] == 0:
                        logger.info(
                            f"No profile documents found for user/scope {user_id} after repair"
                        )
                        return False
                    return True
                except Exception as retry_err:
                    logger.error(f"Profile check retry after repair failed: {retry_err}")
        logger.warning(f"Profile document check failed: {e}, proceeding with KNN query")

    return True


def _execute_knn_search(
    opensearch_client: Any,
    index_name: str,
    embedding: list[float],
    user_id: int,
    threshold: float,
    accessible_profile_ids: set[int] | None = None,
    organization_id: int | None = None,
) -> list[dict]:
    """Execute KNN search and return matches above threshold."""
    from app.services.opensearch_service import _is_index_corruption_error
    from app.services.opensearch_service import _repair_index
    from app.services.opensearch_service import _speaker_org_filter_clauses

    if accessible_profile_ids is not None:
        filters = [
            {"term": {"document_type": "profile"}},
            {"terms": {"profile_id": list(accessible_profile_ids)}},
        ]
    else:
        filters = [
            {"term": {"document_type": "profile"}},
            {"term": {"user_id": user_id}},
        ]
    # Tenant gate on profile docs (org term, else exclude org-stamped).
    filters.extend(_speaker_org_filter_clauses(organization_id))

    query = {
        "size": 25,
        "query": {
            "knn": {
                "embedding": {
                    "vector": embedding,
                    "k": 25,
                    "filter": {"bool": {"filter": filters}},
                }
            }
        },
    }

    try:
        response = opensearch_client.search(index=index_name, body=query)
    except Exception as e:
        if _is_index_corruption_error(e):
            logger.warning(f"Index corruption during KNN search, attempting repair: {e}")
            if _repair_index(index_name):
                try:
                    response = opensearch_client.search(index=index_name, body=query)
                    return _extract_matches_from_response(response, threshold)
                except Exception as retry_err:
                    logger.error(f"KNN search retry after repair failed: {retry_err}")
        logger.error(f"Error in OpenSearch KNN search: {e}")
        return []

    return _extract_matches_from_response(response, threshold)


def _extract_matches_from_response(response: dict, threshold: float) -> list[dict]:
    """Extract and filter matches from OpenSearch response.

    OpenSearch cosinesimil returns ``(1 + cosine) / 2`` (range 0-1).
    We convert back to raw cosine (range -1 to 1) so thresholds like
    0.75 mean real cosine similarity.
    """
    matches = []
    for hit in response["hits"]["hits"]:
        # Convert OpenSearch score to raw cosine similarity
        cosine_sim = 2.0 * hit["_score"] - 1.0

        if cosine_sim < threshold:
            continue

        source = hit["_source"]
        profile_id = source.get("profile_id")
        profile_uuid = source.get("profile_uuid")
        profile_name = source.get("profile_name")

        if not profile_id or not profile_name:
            continue

        matches.append(
            {
                "profile_id": profile_id,
                "profile_uuid": profile_uuid or str(profile_id),
                "profile_name": profile_name,
                "similarity": cosine_sim,
                "embedding_count": source.get("speaker_count", 1),
                "last_update": source.get("updated_at"),
            }
        )

    return matches


class ProfileEmbeddingService:
    """Service for managing profile-level speaker embeddings."""

    @staticmethod
    def update_profile_embedding(db: Session, profile_id: int) -> bool:
        """
        Update the consolidated embedding for a speaker profile by averaging
        all embeddings from speakers assigned to this profile.

        Args:
            db: Database session
            profile_id: ID of the speaker profile to update

        Returns:
            True if successful, False otherwise
        """
        try:
            # Get the profile
            profile = db.query(SpeakerProfile).filter(SpeakerProfile.id == profile_id).first()
            if not profile:
                logger.error(f"Profile {profile_id} not found")
                return False

            # Get all speakers assigned to this profile
            speakers = db.query(Speaker).filter(Speaker.profile_id == profile_id).all()

            if not speakers:
                logger.warning(f"No speakers assigned to profile {profile_id}")
                _process_profile_with_no_speakers(profile, profile_id)
                db.commit()
                return True

            # Collect embeddings from all speakers
            embeddings: list[list[float]] = []
            for speaker in speakers:
                embedding = get_speaker_embedding(str(speaker.uuid))
                if embedding:
                    embeddings.append(embedding)
                else:
                    logger.warning(
                        f"No embedding found for speaker {speaker.uuid} in profile {profile_id}"
                    )

            if not embeddings:
                logger.warning(f"No valid embeddings found for profile {profile_id}")
                return False

            # Delegate averaging + OpenSearch storage to shared helpers
            _process_profile_with_embeddings(profile, profile_id, embeddings)
            db.commit()

            logger.info(
                f"Updated profile {profile_id} embedding with average of {len(embeddings)} speaker embeddings"
            )
            return True

        except Exception as e:
            logger.error(f"Error updating profile embedding for profile {profile_id}: {e}")
            db.rollback()
            return False

    @staticmethod
    def add_speaker_to_profile_embedding(db: Session, speaker_id: int, profile_id: int) -> bool:
        """
        Add a speaker's embedding to the profile's consolidated embedding.

        Performs a full recalculation by averaging all speakers assigned to the
        profile, ensuring the stored embedding always represents the true centroid.

        Args:
            db: Database session
            speaker_id: ID of the speaker being added
            profile_id: ID of the profile to update

        Returns:
            True if successful, False otherwise
        """
        logger.info(f"Adding speaker {speaker_id} to profile {profile_id}, recalculating average")
        return ProfileEmbeddingService.update_profile_embedding(db, profile_id)

    @staticmethod
    def remove_speaker_from_profile_embedding(
        db: Session, speaker_id: int, profile_id: int
    ) -> bool:
        """
        Remove a speaker's contribution from the profile's consolidated embedding.
        This triggers a full recalculation of the profile embedding.

        Args:
            db: Database session
            speaker_id: ID of the speaker to remove
            profile_id: ID of the profile to update

        Returns:
            True if successful, False otherwise
        """
        try:
            logger.info(f"Removing speaker {speaker_id} from profile {profile_id} embedding")
            # For removal, we always do a full recalculation to ensure accuracy
            return ProfileEmbeddingService.update_profile_embedding(db, profile_id)

        except Exception as e:
            logger.error(
                f"Error removing speaker {speaker_id} from profile {profile_id} embedding: {e}"
            )
            return False

    @staticmethod
    def _group_speakers_by_profile(speakers: list[Speaker]) -> dict[int, list[Speaker]]:
        """Group speakers by their profile ID."""
        speakers_by_profile: dict[int, list[Speaker]] = {}
        for speaker in speakers:
            # Callers pass speakers filtered on profile_id (non-None grouping key)
            assert speaker.profile_id is not None
            profile_id = int(speaker.profile_id)
            if profile_id not in speakers_by_profile:
                speakers_by_profile[profile_id] = []
            speakers_by_profile[profile_id].append(speaker)
        return speakers_by_profile

    @staticmethod
    def _process_single_profile_in_batch(
        profile: SpeakerProfile,
        profile_id: int,
        speakers: list[Speaker],
        speaker_embeddings: dict[int, list[float]],
    ) -> bool:
        """Process a single profile during batch update."""
        if not speakers:
            return _process_profile_with_no_speakers(profile, profile_id)

        embeddings = [
            speaker_embeddings[int(speaker.id)]
            for speaker in speakers
            if int(speaker.id) in speaker_embeddings
        ]

        if not embeddings:
            logger.warning(f"No valid embeddings found for profile {profile_id}")
            return False

        return _process_profile_with_embeddings(profile, profile_id, embeddings)

    @staticmethod
    def batch_update_profile_embeddings(db: Session, profile_ids: list[int]) -> dict[int, bool]:
        """
        Update embeddings for multiple profiles in a batch operation.

        This method efficiently processes multiple profiles by:
        - Collecting all speaker data in bulk queries
        - Processing embeddings in batches
        - Minimizing database round trips

        Args:
            db: SQLAlchemy database session
            profile_ids: List of profile IDs to update

        Returns:
            Dictionary mapping profile_id to success status (True/False)
        """
        if not profile_ids:
            return {}

        results: dict[int, bool] = {}

        try:
            # Bulk fetch all profiles and speakers
            profiles = db.query(SpeakerProfile).filter(SpeakerProfile.id.in_(profile_ids)).all()
            profile_map: dict[int, SpeakerProfile] = {
                int(profile.id): profile for profile in profiles
            }

            all_speakers = db.query(Speaker).filter(Speaker.profile_id.in_(profile_ids)).all()
            speakers_by_profile = ProfileEmbeddingService._group_speakers_by_profile(all_speakers)
            speaker_embeddings = _collect_speaker_embeddings(all_speakers)

            # Process each profile
            for profile_id in profile_ids:
                profile = profile_map.get(profile_id)
                if not profile:
                    logger.error(f"Profile {profile_id} not found in batch")
                    results[profile_id] = False
                    continue

                try:
                    speakers = speakers_by_profile.get(profile_id, [])
                    results[profile_id] = ProfileEmbeddingService._process_single_profile_in_batch(
                        profile, profile_id, speakers, speaker_embeddings
                    )
                except Exception as e:
                    logger.error(f"Error updating profile {profile_id} in batch: {e}")
                    results[profile_id] = False

            db.commit()
            success_count = sum(1 for r in results.values() if r)
            logger.info(f"Batch updated {success_count} profiles successfully")

        except Exception as e:
            logger.error(f"Error in batch profile embedding update: {e}")
            db.rollback()
            for profile_id in profile_ids:
                results[profile_id] = False

        return results

    @staticmethod
    def get_profile_embedding(db: Session, profile_id: int) -> list[float] | None:
        """
        Get the consolidated embedding for a speaker profile.

        Args:
            db: Database session
            profile_id: ID of the speaker profile

        Returns:
            The profile's embedding vector, or None if not available
        """
        try:
            # Get profile object to extract UUID
            from app.models.media import SpeakerProfile

            profile = db.query(SpeakerProfile).filter(SpeakerProfile.id == profile_id).first()
            if not profile:
                logger.error(f"Profile {profile_id} not found")
                return None

            # Get embedding from OpenSearch (primary storage for vectors) using UUID
            from app.services.opensearch_service import get_profile_embedding

            result = get_profile_embedding(str(profile.uuid))
            return list(result) if result else None

        except Exception as e:
            logger.error(f"Error retrieving profile embedding for profile {profile_id}: {e}")
            return None

    @staticmethod
    def calculate_profile_similarity(
        db: Session,
        embedding: list[float],
        user_id: int,
        threshold: float = 0.7,
        accessible_profile_ids: set[int] | None = None,
        organization_id: int | None = None,
    ) -> list[dict]:
        """
        Find speaker profiles using OpenSearch native kNN search for optimal performance.

        Completely leverages OpenSearch's HNSW algorithm with cosine similarity
        instead of manual calculations for maximum efficiency.

        Args:
            db: Database session
            embedding: The embedding to compare against
            user_id: User ID to filter profiles
            threshold: Minimum similarity threshold
            accessible_profile_ids: Optional set of profile IDs to search within
                (for shared profile scope). If None, filters by user_id.
            organization_id: Active org id (None = personal) — tenant gate.

        Returns:
            List of matching profiles with similarity scores
        """
        try:
            from app.core.constants import get_speaker_index

            opensearch_client, _, should_continue = _check_opensearch_profile_prerequisites(user_id)
            if not should_continue:
                return []

            speaker_index = get_speaker_index()
            if not _check_profiles_exist_in_opensearch(
                opensearch_client,
                speaker_index,
                user_id,
                accessible_profile_ids=accessible_profile_ids,
            ):
                return []

            matches = _execute_knn_search(
                opensearch_client,
                speaker_index,
                embedding,
                user_id,
                threshold,
                accessible_profile_ids=accessible_profile_ids,
                organization_id=organization_id,
            )

            logger.info(
                f"OpenSearch found {len(matches)} profile matches above threshold {threshold}"
            )
            return matches

        except Exception as e:
            logger.error(f"Error in OpenSearch profile similarity search: {e}")
            return []
