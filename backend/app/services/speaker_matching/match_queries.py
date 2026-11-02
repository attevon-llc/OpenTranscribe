"""Read-only match lookups: a voiceprint against known speakers, profiles and unlabeled speakers."""

import logging
from typing import TYPE_CHECKING
from typing import Any

import numpy as np

from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.models.speaker import Speaker
from app.models.speaker import SpeakerProfile
from app.services.opensearch_service import find_matching_speaker
from app.services.speaker_matching.thresholds import ConfidenceLevel
from app.utils.cosine_space import raw_cosine_from_opensearch_score

if TYPE_CHECKING:
    from app.services.speaker_matching_service import SpeakerMatchingService

logger = logging.getLogger(__name__)


def match_speaker_to_known_speakers(
    self: "SpeakerMatchingService",
    embedding: np.ndarray,
    user_id: int,
    accessible_profile_ids: set[int] | None = None,
    organization_id: OrgScope = UNSCOPED,
) -> dict[str, Any] | None:
    """
    Match a speaker embedding to known speakers, prioritizing profile embeddings
    for better cross-file recognition accuracy.

    Args:
        embedding: Speaker embedding vector
        user_id: User ID
        accessible_profile_ids: Optional set of profile IDs to search within
            (for shared profile scope). If None, filters by user_id.
        organization_id: Tenant scope of the file being matched (None =
            personal). Gates both the profile kNN and the individual
            speaker kNN so cross-org voiceprints never match. Required in
            effect: this call names no speaker to derive the scope from, so
            leaving it out is refused rather than read as personal.

    Returns:
        Dictionary with speaker info and confidence, or None
    """
    allowed, organization_id = self._scope_gate(user_id, organization_id)
    if not allowed:
        return None

    # First, try to match against profile embeddings for better accuracy
    profile_match = self.match_speaker_to_profiles(
        embedding,
        user_id,
        accessible_profile_ids=accessible_profile_ids,
        organization_id=organization_id,
    )

    if profile_match and profile_match["confidence"] >= ConfidenceLevel.MEDIUM:
        return profile_match

    # Fallback to individual speaker matching in OpenSearch
    match = find_matching_speaker(
        embedding.tolist(),
        user_id,
        threshold=ConfidenceLevel.MEDIUM,  # Minimum threshold
        organization_id=organization_id,
    )

    if not match:
        return None

    # Get the speaker from database
    speaker = (
        self.db.query(Speaker)
        .filter(
            Speaker.id == match["speaker_id"],
            Speaker.user_id == user_id,
            Speaker.verified.is_(True),
            Speaker.display_name.isnot(None),
        )
        .first()
    )

    if not speaker:
        return None

    confidence = match.get("confidence", 0.5)

    return {
        "speaker_id": speaker.id,
        "speaker_uuid": str(speaker.uuid),
        "suggested_name": speaker.display_name,
        "confidence": confidence,
        "confidence_level": self.get_confidence_level(confidence),
        "auto_accept": confidence >= ConfidenceLevel.HIGH,
        "source": "individual_embedding",
    }


def match_speaker_to_profiles(
    self: "SpeakerMatchingService",
    embedding: np.ndarray,
    user_id: int,
    accessible_profile_ids: set[int] | None = None,
    organization_id: OrgScope = UNSCOPED,
) -> dict[str, Any] | None:
    """
    Match a speaker embedding against consolidated profile embeddings.
    This provides better accuracy than individual speaker matching.

    Args:
        embedding: Speaker embedding vector
        user_id: User ID
        accessible_profile_ids: Optional set of profile IDs to search within
            (for shared profile scope). If None, filters by user_id.
        organization_id: Tenant scope of the file being matched (None =
            personal). Profile docs mirror ``SpeakerProfile.organization_id``
            so only same-org (or, for None, personal) profiles can match. Leaving it
            out is refused: there is no speaker here to derive the scope from.

    Returns:
        Dictionary with profile match info and confidence, or None
    """
    allowed, organization_id = self._scope_gate(user_id, organization_id)
    if not allowed:
        return None

    try:
        from app.services.profile_embedding_service import ProfileEmbeddingService

        # Get profile matches using the ProfileEmbeddingService
        profile_matches = ProfileEmbeddingService.calculate_profile_similarity(
            embedding.tolist(),
            user_id,
            threshold=ConfidenceLevel.LOW,
            accessible_profile_ids=accessible_profile_ids,
            organization_id=organization_id,
        )

        if not profile_matches:
            return None

        # Return the best match
        best_match = profile_matches[0]
        confidence = best_match["similarity"]

        # Verify profile exists and is accessible
        if accessible_profile_ids is not None:
            profile = (
                self.db.query(SpeakerProfile)
                .filter(
                    SpeakerProfile.id == best_match["profile_id"],
                    SpeakerProfile.id.in_(accessible_profile_ids),
                )
                .first()
            )
        else:
            profile = (
                self.db.query(SpeakerProfile)
                .filter(
                    SpeakerProfile.id == best_match["profile_id"],
                    SpeakerProfile.user_id == user_id,
                )
                .first()
            )

        if not profile:
            return None

        # Try to get a representative speaker from this profile if one exists
        # This is optional - the profile itself is sufficient for matching
        profile_speaker = (
            self.db.query(Speaker)
            .filter(
                Speaker.profile_id == profile.id,  # Use integer profile.id
                Speaker.user_id == user_id,
                Speaker.verified.is_(True),
                Speaker.display_name.isnot(None),
            )
            .first()
        )

        # Use profile_speaker.id if available, otherwise None
        # The system will handle None speaker_id correctly
        speaker_id = profile_speaker.id if profile_speaker else None

        return {
            "speaker_id": speaker_id,
            "suggested_name": best_match["profile_name"],
            "confidence": confidence,
            "confidence_level": self.get_confidence_level(confidence),
            "auto_accept": confidence >= ConfidenceLevel.HIGH,
            "profile_id": profile.id,  # Return integer ID for database operations
            "profile_uuid": str(profile.uuid),  # Also return UUID for API responses
            "source": "profile_embedding",
            "embedding_count": best_match["embedding_count"],
        }

    except Exception as e:
        logger.error(f"Error matching speaker to profiles: {e}")
        return None


def find_unlabeled_speaker_matches(
    self: "SpeakerMatchingService", embedding: np.ndarray, user_id: int, exclude_speaker_id: int
) -> list[dict[str, Any]]:
    """
    Find matches to unlabeled speakers across videos (for pre-labeling suggestions).

    Args:
        embedding: Speaker embedding vector
        user_id: User ID
        exclude_speaker_id: Speaker ID to exclude from results

    Returns:
        List of matched speakers with video source information
    """
    matches: list[dict[str, Any]] = []
    logger.info(f"Finding unlabeled matches for speaker {exclude_speaker_id}, user {user_id}")

    try:
        # Tenant gate follows the reference speaker's file org.
        source_speaker = self.db.query(Speaker).filter(Speaker.id == exclude_speaker_id).first()
        organization_id = self._media_file_org_id(
            int(source_speaker.media_file_id) if source_speaker else None
        )
        if not self._voiceprints_allowed(user_id, organization_id):
            return matches

        response = self._perform_opensearch_query(
            embedding, user_id, exclude_speaker_id, organization_id=organization_id, size=10
        )
        if response is None:
            return matches

        logger.info(
            f"OpenSearch response for speaker {exclude_speaker_id}: {len(response['hits']['hits'])} hits"
        )

        # Collect candidate IDs from hits, then batch-fetch
        candidate_ids: set[int] = set()
        candidate_scores: dict[int, float] = {}
        for hit in response["hits"]["hits"]:
            confidence = raw_cosine_from_opensearch_score(hit["_score"])
            if confidence >= ConfidenceLevel.HIGH:  # high-confidence matches only
                # Safely access speaker_id - skip if not present (e.g., profile documents)
                source = hit.get("_source", {})
                match_speaker_id = source.get("speaker_id")
                if not match_speaker_id:
                    continue

                candidate_ids.add(match_speaker_id)
                candidate_scores[match_speaker_id] = confidence

        # Batch-fetch all candidate speakers (avoids N+1 per-hit queries)
        if candidate_ids:
            from sqlalchemy.orm import joinedload

            candidates = (
                self.db.query(Speaker)
                .options(joinedload(Speaker.media_file))
                .filter(Speaker.id.in_(candidate_ids))
                .all()
            )
            for matched_speaker in candidates:
                if matched_speaker.media_file:
                    conf = candidate_scores[int(matched_speaker.id)]
                    matches.append(
                        {
                            "speaker_id": int(matched_speaker.id),
                            "speaker_name": matched_speaker.name,
                            "display_name": matched_speaker.display_name,
                            "media_file_id": matched_speaker.media_file_id,
                            "media_file_title": matched_speaker.media_file.title
                            or matched_speaker.media_file.filename,
                            "confidence": conf,
                            "confidence_level": self.get_confidence_level(conf),
                            "verified": matched_speaker.verified,
                            "is_cross_video_suggestion": True,
                        }
                    )

        # Sort by confidence (highest first)
        matches.sort(key=lambda x: x["confidence"], reverse=True)

    except Exception as e:
        logger.error(f"Error finding unlabeled speaker matches: {e}")

    logger.info(f"Returning {len(matches)} unlabeled matches for speaker {exclude_speaker_id}")
    return matches


def match_speaker_to_profile(
    self: "SpeakerMatchingService",
    embedding: np.ndarray,
    user_id: int,
    organization_id: OrgScope = UNSCOPED,
) -> dict[str, Any] | None:
    """
    Match a speaker embedding to an existing profile.

    Args:
        embedding: Speaker embedding vector
        user_id: User ID
        organization_id: Tenant scope of the file being matched (None =
            personal) — gates the speaker kNN. Leaving it out is refused: there
            is no speaker here to derive the scope from.

    Returns:
        Dictionary with profile info and confidence, or None
    """
    allowed, organization_id = self._scope_gate(user_id, organization_id)
    if not allowed:
        return None

    # Search for matching speaker in OpenSearch
    match = find_matching_speaker(
        embedding.tolist(),
        user_id,
        threshold=ConfidenceLevel.MEDIUM,  # Minimum threshold
        organization_id=organization_id,
    )

    if not match:
        return None

    # Check if matched speaker has a profile
    profile_id = match.get("profile_id")
    if not profile_id:
        return None

    # Get the speaker profile from database
    profile = (
        self.db.query(SpeakerProfile)
        .filter(
            SpeakerProfile.id == profile_id,
            SpeakerProfile.user_id == user_id,
        )
        .first()
    )

    if not profile:
        return None

    confidence = match.get("confidence", 0.5)

    return {
        "profile_id": profile.id,
        "profile_name": profile.name,
        "confidence": confidence,
        "confidence_level": self.get_confidence_level(confidence),
        "auto_accept": confidence >= ConfidenceLevel.HIGH,
    }
