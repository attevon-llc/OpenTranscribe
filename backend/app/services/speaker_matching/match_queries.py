"""Read-only match lookups: a voiceprint against known speakers and profiles."""

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
