"""Indexes on ``speaker`` that the runner builds concurrently after the upgrade (v458, #1328)."""

from __future__ import annotations

from app.db.concurrent_indexes import IndexSpec

#: Every privacy sweep filters ``identity_source`` to the machine-assigned values and looks the
#: rows up by file, so a partial index on ``media_file_id`` serves ``IN (...)`` and each ``=``.
MACHINE_IDENTITY = IndexSpec(
    "ix_speaker_machine_identity_media_file",
    "speaker",
    ("media_file_id",),
    where="identity_source IN ('voiceprint', 'voiceprint_link')",
)

SPEAKER_INDEXES = (MACHINE_IDENTITY,)
