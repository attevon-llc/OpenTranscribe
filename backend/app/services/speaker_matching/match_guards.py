"""What a found match is allowed to do: user corrections that block it, and the record it leaves."""

import logging
from typing import TYPE_CHECKING
from typing import Any

from sqlalchemy import func

from app.models.media import Speaker
from app.services import embedding_mode_service
from app.services.speaker_matching.thresholds import MATCHING_ORIGIN_TASK
from app.services.speaker_matching.thresholds import ConfidenceLevel
from app.utils.speaker_identity_detail import ORIGIN_PROFILE_MATCH
from app.utils.speaker_identity_detail import ORIGIN_VOICE_MATCH
from app.utils.speaker_identity_detail import voiceprint_detail
from app.utils.speaker_identity_source import is_person_verified
from app.utils.speaker_identity_source import record_suggestion

if TYPE_CHECKING:
    from app.services.speaker_matching_service import SpeakerMatchingService

logger = logging.getLogger(__name__)


def linked_speaker_count(self: "SpeakerMatchingService", profile_id: int) -> int:
    """Count the speakers currently assigned to *profile_id*.

    This is the profile's embedding population, and therefore the weight its
    stored centroid carries. It is queried rather than remembered so the
    incremental update cannot drift from the number a full recalculation
    would produce.

    Args:
        profile_id: Profile whose members to count.

    Returns:
        Number of ``Speaker`` rows pointing at the profile.
    """
    return (
        self.db.query(func.count(Speaker.id)).filter(Speaker.profile_id == profile_id).scalar() or 0
    )


def refusals(self: "SpeakerMatchingService", speaker_id: int) -> tuple[set[str], set[int]]:
    """The names and the speakers a user's correction rules out for this speaker."""
    from app.services.speaker_cleanup import blocked_profile_names
    from app.services.speaker_cleanup import cannot_link_partners

    return (
        blocked_profile_names(self.db, [speaker_id])[speaker_id],
        cannot_link_partners(self.db, [speaker_id])[speaker_id],
    )


def match_is_blocked(
    self: "SpeakerMatchingService", speaker: Speaker, match: dict[str, Any]
) -> bool:
    """Whether a correction forbids giving this speaker the matched identity."""
    from app.services.speaker_cleanup import blocked_profile_names
    from app.services.speaker_cleanup import speaker_blocked_from_profile

    profile_id = match.get("profile_id")
    if profile_id is not None and speaker_blocked_from_profile(
        self.db, int(speaker.id), int(profile_id)
    ):
        return True
    name = str(match.get("suggested_name") or "").casefold()
    return bool(name) and name in blocked_profile_names(self.db, [int(speaker.id)])[int(speaker.id)]


def match_detail(self: "SpeakerMatchingService", match: dict[str, Any]) -> dict[str, Any]:
    """The origin record of a match: what matched, how well, and with which embedding model."""
    return voiceprint_detail(
        ORIGIN_PROFILE_MATCH if match.get("profile_id") else ORIGIN_VOICE_MATCH,
        confidence=match.get("confidence"),
        threshold=ConfidenceLevel.HIGH,
        matched_speaker_uuid=match.get("speaker_uuid"),
        matched_profile_uuid=match.get("profile_uuid"),
        embedding_model=embedding_mode_service.voiceprint_model_id(),
        origin_task=MATCHING_ORIGIN_TASK,
    )


def record_match_suggestion(
    speaker: Speaker, match: dict[str, Any], match_detail: dict[str, Any]
) -> None:
    """Store a found match's confidence and, if high enough, its suggestion.

    A speaker a person verified gets neither the suggestion nor the score of a voice
    comparison, unless the auto-accept that follows makes the voiceprint's the identity of
    the row.
    """
    if match["auto_accept"] or not is_person_verified(speaker):
        speaker.confidence = match["confidence"]
    if match["confidence"] >= ConfidenceLevel.HIGH:  # Only suggest if >= 75% confidence
        record_suggestion(
            speaker,
            source="profile_match" if match.get("profile_id") else "voice_match",
            name=match["suggested_name"],
            confidence=match["confidence"],
            detail=match_detail,
            identity_follows=bool(match["auto_accept"]),
        )
