"""Centralized application enums.

Import enums from here instead of from model files to avoid circular
imports and to provide a single source of truth.
"""

import enum


# NOT StrEnum (UP042): str(FileStatus.X) == "FileStatus.X" is load-bearing — the
# status-detail API pins it as a characterization (test_files_management.py) and
# the redaction-guard / on-demand-analytics `str(status)` comparisons would
# silently change behavior. Convert deliberately in its own change, not a codemod.
class FileStatus(str, enum.Enum):  # noqa: UP042
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
