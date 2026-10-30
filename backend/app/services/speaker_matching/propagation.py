"""Propagating a profile assignment to other, similar speakers."""

import logging
from typing import TYPE_CHECKING

from app.core.constants import SPEAKER_CONFIDENCE_HIGH
from app.models.media import Speaker
from app.models.media import SpeakerProfile
from app.services import embedding_mode_service
from app.services.speaker_matching.thresholds import MATCHING_ORIGIN_TASK
from app.utils.speaker_identity_detail import ORIGIN_PROFILE_PROPAGATION
from app.utils.speaker_identity_detail import voiceprint_detail
from app.utils.speaker_identity_source import record_machine_link

if TYPE_CHECKING:
    from app.services.speaker_matching_service import SpeakerMatchingService

logger = logging.getLogger(__name__)


def get_speaker_embedding_for_propagation(
    self: "SpeakerMatchingService", matched_speaker_id: int
) -> tuple[list[float] | None, str | None]:
    """
    Get speaker embedding for profile propagation.

    Args:
        matched_speaker_id: ID of the speaker to get embedding for

    Returns:
        Tuple of (embedding, speaker_uuid) or (None, None) if not found
    """
    from app.db.base import get_db
    from app.services.opensearch_service import get_speaker_embedding

    db = next(get_db())
    try:
        matched_speaker = db.query(Speaker).filter(Speaker.id == matched_speaker_id).first()
        if not matched_speaker:
            logger.warning(f"Speaker {matched_speaker_id} not found, skipping propagation")
            return None, None

        embedding = get_speaker_embedding(str(matched_speaker.uuid))
        if not embedding:
            logger.warning(
                f"No embedding found for speaker {matched_speaker.uuid}, skipping propagation"
            )
            return None, None

        return embedding, str(matched_speaker.uuid)
    finally:
        db.close()


def should_propagate_to_speaker(self: "SpeakerMatchingService", speaker: Speaker) -> bool:
    """
    Check if a speaker should receive profile propagation.

    Args:
        speaker: Speaker to check

    Returns:
        True if speaker should be updated with profile
    """
    if speaker.profile_id:
        return False
    if not speaker.display_name:
        return False
    return not speaker.display_name.startswith("SPEAKER_")


def update_speakers_in_opensearch(
    self: "SpeakerMatchingService", updated_speakers: list[Speaker], profile_id: int
) -> None:
    """
    Update speakers in OpenSearch after profile propagation.

    Args:
        updated_speakers: List of speakers to update
        profile_id: Profile ID assigned to speakers
    """
    from app.services.opensearch_service import update_speaker_profile

    profile_uuid = None
    if profile_id:
        profile = self.db.query(SpeakerProfile).filter(SpeakerProfile.id == profile_id).first()
        if profile:
            profile_uuid = str(profile.uuid)

    for speaker in updated_speakers:
        update_speaker_profile(
            speaker_uuid=str(speaker.uuid),
            profile_id=int(speaker.profile_id) if speaker.profile_id else None,
            profile_uuid=profile_uuid,
            verified=bool(speaker.verified),
        )


def propagate_profile_assignment(
    self: "SpeakerMatchingService", matched_speaker_id: int, profile_id: int, user_id: int
):
    """
    When a speaker is assigned to a profile, find and update other similar speakers
    with the same profile assignment.

    Args:
        matched_speaker_id: ID of the speaker that was just assigned to a profile
        profile_id: ID of the profile assigned
        user_id: User ID for scoping
    """
    try:
        embedding, _ = self._get_speaker_embedding_for_propagation(matched_speaker_id)
        if not embedding:
            return

        from app.services.similarity_service import SimilarityService

        # Propagation stays within the profile's tenant: an org profile is
        # only propagated to that org's speaker docs, a personal profile
        # only to personal (org-less) docs.
        profile_row = (
            self.db.query(SpeakerProfile.organization_id, SpeakerProfile.uuid)
            .filter(SpeakerProfile.id == profile_id)
            .first()
        )
        profile_org_id = int(profile_row[0]) if profile_row and profile_row[0] is not None else None
        profile_uuid = str(profile_row[1]) if profile_row else None
        matched_speaker_uuid = (
            self.db.query(Speaker.uuid).filter(Speaker.id == matched_speaker_id).scalar()
        )
        if not self._voiceprints_allowed(user_id, profile_org_id):
            return

        similar_matches = SimilarityService.opensearch_similarity_search(
            embedding=embedding,
            user_id=user_id,
            index_name="speakers",
            # Auto-assignment writes `verified=True`, so the gate is the
            # auto-accept band and nothing weaker. Stated in raw cosine; the
            # search converts it to OpenSearch score space (issue #674).
            min_raw_cosine=SPEAKER_CONFIDENCE_HIGH,
            max_results=20,
            exclude_ids=[matched_speaker_id],
            organization_id=profile_org_id,
        )

        # Batch-fetch all candidate speakers (avoids N+1 per-match queries)
        match_by_id = {
            m["speaker_id"]: m["similarity"] for m in similar_matches if m.get("speaker_id")
        }
        candidate_speakers = (
            self.db.query(Speaker).filter(Speaker.id.in_(match_by_id.keys())).all()
            if match_by_id
            else []
        )

        from app.services.speaker_cleanup import SpeakerImpact
        from app.services.speaker_cleanup import clusters_holding_speakers
        from app.services.speaker_cleanup import has_other_identity
        from app.services.speaker_cleanup import reconcile_speaker_impact
        from app.services.speaker_cleanup import speaker_blocked_from_profile

        updated_speakers = []
        for speaker in candidate_speakers:
            if not self._should_propagate_to_speaker(speaker):
                continue
            # A speaker the user unassigned from this profile, or said is not one
            # of its speakers, is never re-linked by a propagation; nor is one the user
            # named somebody else — a look-alike voice does not rename it (#1233).
            if speaker_blocked_from_profile(
                self.db, int(speaker.id), profile_id
            ) or has_other_identity(self.db, speaker, profile_id):
                continue

            similarity = match_by_id[int(speaker.id)]
            speaker.profile_id = profile_id  # type: ignore[assignment]
            speaker.verified = True  # type: ignore[assignment]
            # the name was already there; only the link is new
            record_machine_link(
                speaker,
                detail=voiceprint_detail(
                    ORIGIN_PROFILE_PROPAGATION,
                    confidence=similarity,
                    threshold=SPEAKER_CONFIDENCE_HIGH,
                    matched_speaker_uuid=matched_speaker_uuid,
                    matched_profile_uuid=profile_uuid,
                    embedding_model=embedding_mode_service.voiceprint_model_id(),
                    origin_task=MATCHING_ORIGIN_TASK,
                ),
            )
            speaker.confidence = similarity
            updated_speakers.append(speaker)

            logger.info(
                f"Propagated profile {profile_id} to similar speaker {speaker.id} "
                f"(confidence: {similarity:.3f})"
            )

        if not updated_speakers:
            return

        updated_ids = {int(s.id) for s in updated_speakers}
        impact = SpeakerImpact(
            owner_ids={user_id},
            profile_ids={profile_id},
            cluster_uuids=clusters_holding_speakers(self.db, updated_ids),
        )
        self.db.commit()
        self._update_speakers_in_opensearch(updated_speakers, profile_id)
        # The profile gained voices and their clusters re-weight them (profile-linked
        # members count double); both rebuilt from the committed rows. Logged, not
        # raised: this runs inside upload-time matching.
        reconcile_speaker_impact(self.db, impact)
        logger.info(f"Propagated profile {profile_id} to {len(updated_speakers)} similar speakers")

    except Exception as e:
        logger.error(f"Error propagating profile assignment: {e}")
        self.db.rollback()
