"""Confidence bands and the origin tag shared by every matching module."""

from app.core.constants import SPEAKER_CONFIDENCE_HIGH
from app.core.constants import SPEAKER_CONFIDENCE_LOW
from app.core.constants import SPEAKER_CONFIDENCE_MEDIUM

#: ``detail.origin_task`` of everything the matcher writes: it runs inside the upload pipeline
#: and the retroactive pass, with no person to attribute it to.
MATCHING_ORIGIN_TASK = "speaker_matching"


class ConfidenceLevel:
    """Confidence level thresholds for speaker matching"""

    HIGH = SPEAKER_CONFIDENCE_HIGH  # Auto-accept (green)
    MEDIUM = SPEAKER_CONFIDENCE_MEDIUM  # Requires validation (yellow)
    LOW = SPEAKER_CONFIDENCE_LOW  # Requires user input (red)
