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

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm.attributes import set_committed_value

from app.models.speaker import Speaker
from app.utils.speaker_labels import canonical_speaker_label_for_row

logger = logging.getLogger(__name__)

# Mapped ``Speaker`` columns that hold a computed status. Only
# nothing in production writes them; reads decorate without dirtying.
_PERSISTED_STATUS_FIELDS = (
    "computed_status",
    "status_text",
    "status_color",
    "resolved_display_name",
)


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
    def compute_speaker_status(
        speaker: Speaker, *, voiceprint_names: bool | None = None
    ) -> dict[str, str | None]:
        """
        Compute comprehensive status information for a speaker.

        Args:
            speaker: Speaker object
            voiceprint_names: Passed to ``canonical_speaker_label_for_row`` for
                ``resolved_display_name``. ``None`` (a response) asks the privacy policy;
                ``True`` describes the stored data, which is what a PERSISTED status must
                hold (#1280 D5: hiding is decided per read, never written down).

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
        resolved_display_name = SpeakerStatusService._resolve_display_name(
            speaker, voiceprint_names=voiceprint_names
        )

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
    def _resolve_profile_info(speaker: Speaker) -> tuple[str | None, str | None]:
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
    def _resolve_display_name(speaker: Speaker, *, voiceprint_names: bool | None = None) -> str:
        """
        Resolve the best display name for a speaker.

        Delegates to :func:`app.utils.speaker_labels.canonical_speaker_label` — the single
        home for this resolution. Previously this method ignored suggestions entirely
        (unlike ``transcript_builders.get_speaker_name``), which was the live disagreement
        between the two planes: the same speaker could read one way in the speaker
        management UI and another in a summary.

        Args:
            speaker: Speaker object
            voiceprint_names: See :meth:`compute_speaker_status`.

        Returns:
            Best available display name
        """
        return canonical_speaker_label_for_row(speaker, voiceprint_names=voiceprint_names)

    @staticmethod
    def add_computed_status(speaker: Speaker, *, voiceprint_names: bool | None = None) -> None:
        """
        Add computed status fields to a speaker object in-place, for the response only.

        The four status fields are mapped columns, but here they are response
        decoration: they are set with ``set_committed_value``, so the session does not
        see a change and never flushes an ``UPDATE speaker``. Assigning them normally
        made every read that later committed write each speaker row, and a read polling
        a file mid-processing then deadlocked with the worker writing the same rows
        (#1152). Nothing in production persists them.

        Args:
            speaker: Speaker object to enhance
            voiceprint_names: The file's voiceprint decision when the caller already holds
                it (a list resolves it once for every row); ``None`` asks the policy per row,
                which costs several queries for a voiceprint-named speaker.
        """
        status_info = SpeakerStatusService.compute_speaker_status(
            speaker, voiceprint_names=voiceprint_names
        )

        mapped = sa_inspect(speaker, raiseerr=False) is not None
        for field in _PERSISTED_STATUS_FIELDS:
            if mapped:
                set_committed_value(speaker, field, status_info[field])
            else:  # a plain object standing in for a row: nothing to dirty
                setattr(speaker, field, status_info[field])
        # Not columns at all: plain response attributes.
        speaker.profile_name = status_info["profile_name"]
        speaker.profile_status = status_info["profile_status"]
