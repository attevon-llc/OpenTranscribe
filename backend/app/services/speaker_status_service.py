"""
Speaker Status Service for computing verification status and display information.

This service handles the computation of speaker verification status, confidence levels,
and display text that was previously computed on the frontend.

The service provides comprehensive status computation including:
- Speaker verification status determination
- Confidence level categorization and thresholds
- Status display text generation for user interfaces
- Resolved display name computation with fallbacks
- Batch processing for multiple speakers

All status computation is centralized here to ensure consistency across the application
and reduce frontend complexity.
"""

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models.media import Speaker

logger = logging.getLogger(__name__)


class SpeakerStatusService:
    """Service for computing speaker status and display information."""

    # Status constants
    STATUS_VERIFIED = "verified"
    STATUS_SUGGESTED = "suggested"
    STATUS_UNVERIFIED = "unverified"

    # Confidence thresholds
    HIGH_CONFIDENCE_THRESHOLD = 0.75
    MEDIUM_CONFIDENCE_THRESHOLD = 0.5

    # Status colors for UI
    STATUS_COLORS = {
        STATUS_VERIFIED: "var(--success-color)",
        STATUS_SUGGESTED: "var(--warning-color)",
        STATUS_UNVERIFIED: "var(--error-color)",
    }

    @staticmethod
    def compute_speaker_status(speaker: Speaker) -> dict[str, Optional[str]]:
        """
        Compute comprehensive status information for a speaker.

        Args:
            speaker: Speaker object

        Returns:
            Dictionary with computed status information. ``profile_name`` and
            ``profile_status`` are ``None`` when the speaker has no linked profile;
            the other values are always non-null strings.
        """
        # Determine status
        status = SpeakerStatusService._get_speaker_status(speaker)

        # Generate display text
        status_text = SpeakerStatusService._get_status_text(speaker, status)

        # Get status color
        status_color = SpeakerStatusService.STATUS_COLORS.get(
            status, SpeakerStatusService.STATUS_COLORS[SpeakerStatusService.STATUS_UNVERIFIED]
        )

        # Resolve display name
        resolved_display_name = SpeakerStatusService._resolve_display_name(speaker)

        # Linked profile info (None when the speaker has no profile)
        profile_name, profile_status = SpeakerStatusService._resolve_profile_info(speaker)

        return {
            "computed_status": status,
            "status_text": status_text,
            "status_color": status_color,
            "resolved_display_name": resolved_display_name,
            "profile_name": profile_name,
            "profile_status": profile_status,
        }

    @staticmethod
    def _resolve_profile_info(speaker: Speaker) -> tuple[Optional[str], Optional[str]]:
        """Resolve the linked SpeakerProfile's name and a coarse link status.

        Args:
            speaker: Speaker object.

        Returns:
            ``(profile_name, profile_status)`` where ``profile_status`` is ``"linked"``
            when a profile is attached and ``None`` otherwise. ``profile_name`` is the
            profile's name, or ``None`` when no profile is linked.
        """
        if speaker.profile:
            return str(speaker.profile.name), "linked"
        return None, None

    @staticmethod
    def _get_speaker_status(speaker: Speaker) -> str:
        """
        Determine the verification status of a speaker.

        Args:
            speaker: Speaker object

        Returns:
            Status string: "verified", "suggested", or "unverified"
        """
        if speaker.verified and speaker.profile:
            return SpeakerStatusService.STATUS_VERIFIED
        elif (
            speaker.confidence
            and speaker.confidence >= SpeakerStatusService.MEDIUM_CONFIDENCE_THRESHOLD
        ):
            return SpeakerStatusService.STATUS_SUGGESTED
        else:
            return SpeakerStatusService.STATUS_UNVERIFIED

    @staticmethod
    def _get_status_text(speaker: Speaker, status: str) -> str:
        """
        Generate display text for speaker status.

        Args:
            speaker: Speaker object
            status: Computed status

        Returns:
            Human-readable status text
        """
        if status == SpeakerStatusService.STATUS_VERIFIED and speaker.profile:
            return f"Verified as {speaker.profile.name}"
        elif status == SpeakerStatusService.STATUS_SUGGESTED:
            if (
                speaker.confidence
                and speaker.confidence >= SpeakerStatusService.HIGH_CONFIDENCE_THRESHOLD
            ):
                return "High confidence match - click to verify"
            else:
                return "Medium confidence match - review needed"
        else:
            return "Needs identification"

    @staticmethod
    def _resolve_display_name(speaker: Speaker) -> str:
        """
        Resolve the best display name for a speaker.

        Args:
            speaker: Speaker object

        Returns:
            Best available display name
        """
        return str(speaker.display_name or speaker.name or "Unknown Speaker")

    @staticmethod
    def compute_status_for_speakers(db: Session, speakers: list[Speaker]) -> list[dict]:
        """
        Compute status for multiple speakers efficiently.

        Args:
            db: Database session
            speakers: List of Speaker objects

        Returns:
            List of speaker data with computed status information
        """
        enriched_speakers = []

        for speaker in speakers:
            # Convert speaker to dict
            speaker_data = {
                "id": speaker.id,
                "name": speaker.name,
                "display_name": speaker.display_name,
                "verified": speaker.verified,
                "confidence": speaker.confidence,
                "media_file_id": speaker.media_file_id,
            }

            # Add profile information if available
            if speaker.profile:
                speaker_data["profile"] = {
                    "id": speaker.profile.id,
                    "name": speaker.profile.name,
                    "description": speaker.profile.description,
                }

            # Compute and add status information
            status_info = SpeakerStatusService.compute_speaker_status(speaker)
            speaker_data.update(status_info)

            enriched_speakers.append(speaker_data)

        return enriched_speakers

    @staticmethod
    def update_speaker_status_in_db(db: Session, speaker_id: int) -> bool:
        """
        Update computed status fields for a speaker in the database.

        Args:
            db: Database session
            speaker_id: ID of the speaker to update

        Returns:
            True if updated successfully, False otherwise
        """
        try:
            speaker = db.query(Speaker).filter(Speaker.id == speaker_id).first()
            if not speaker:
                logger.error(f"Speaker {speaker_id} not found")
                return False

            # Compute status
            status_info = SpeakerStatusService.compute_speaker_status(speaker)

            # Update computed fields
            speaker.computed_status = status_info["computed_status"]  # type: ignore[assignment]
            speaker.status_text = status_info["status_text"]  # type: ignore[assignment]
            speaker.status_color = status_info["status_color"]  # type: ignore[assignment]
            speaker.resolved_display_name = status_info["resolved_display_name"]  # type: ignore[assignment]

            db.commit()
            logger.info(
                f"Updated status for speaker {speaker_id}: {status_info['computed_status']}"
            )
            return True

        except Exception as e:
            logger.error(f"Error updating speaker status for {speaker_id}: {e}")
            db.rollback()
            return False

    @staticmethod
    def refresh_all_speaker_statuses(db: Session, media_file_id: Optional[int] = None) -> int:
        """
        Refresh computed status for all speakers or speakers in a specific file.

        Args:
            db: Database session
            media_file_id: Optional file ID to limit updates to specific file

        Returns:
            Number of speakers updated
        """
        try:
            query = db.query(Speaker)
            if media_file_id:
                query = query.filter(Speaker.media_file_id == media_file_id)

            speakers = query.all()
            updated_count = 0

            for speaker in speakers:
                if SpeakerStatusService.update_speaker_status_in_db(db, int(speaker.id)):
                    updated_count += 1

            logger.info(f"Refreshed status for {updated_count} speakers")
            return updated_count

        except Exception as e:
            logger.error(f"Error refreshing speaker statuses: {e}")
            return 0

    @staticmethod
    def add_computed_status(speaker: Speaker) -> None:
        """
        Add computed status fields to a speaker object in-place.

        Args:
            speaker: Speaker object to enhance
        """
        status_info = SpeakerStatusService.compute_speaker_status(speaker)

        # Add computed fields to the speaker object
        speaker.computed_status = status_info["computed_status"]  # type: ignore[assignment]
        speaker.status_text = status_info["status_text"]  # type: ignore[assignment]
        speaker.status_color = status_info["status_color"]  # type: ignore[assignment]
        speaker.resolved_display_name = status_info["resolved_display_name"]  # type: ignore[assignment]
        speaker.profile_name = status_info["profile_name"]  # type: ignore[assignment]
        speaker.profile_status = status_info["profile_status"]  # type: ignore[assignment]
