"""Centralized application enums.

Import enums from here instead of from model files to avoid circular
imports and to provide a single source of truth.
"""

import enum


class FileStatus(str, enum.Enum):
    """Processing status for media files."""

    PENDING = "pending"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    COMPLETED = "completed"
    ERROR = "error"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    ORPHANED = "orphaned"
    # Abuse / DMCA / safe-harbor takedown. Distinct from the processing lifecycle
    # above: a file in ANY prior state can be quarantined. The authoritative
    # takedown flag is the dedicated ``MediaFile.is_quarantined`` column (so a
    # completed file's processing status survives a takedown and is restored on
    # release); this enum value is the surfaced display status while held.
    QUARANTINED = "quarantined"
