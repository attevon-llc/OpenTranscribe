"""What a normal pipeline run leaves on a file, written for a seeded one (issue #628).

``inject_meeting`` builds rows without entering the transcription pipeline, so a seeded
file had no ``processing_provenance`` (File details read "Not recorded (processed before
this was tracked)"), no ``transcription`` task row (File status listed only the
post-processing tasks) and no thumbnail. These helpers write the same records through the
production writers, not a second implementation of them.

The bundle's transcripts are this app's own output, produced once offline on a GPU machine
(``bundle`` module docstring), so the transcription step is recorded as the local engine on
this server. Which model ran is not in the bundle, so none is claimed.
"""

from __future__ import annotations

import uuid
from typing import Any


def record_import_processing(db: Any, media_file_id: int, user_id: int) -> None:
    """Record the transcription step and a completed ``transcription`` task for the file."""
    from app.services.file_provenance import record_stage
    from app.services.file_provenance import transcription_facts
    from app.utils.task_utils import TASK_STATUS_COMPLETED
    from app.utils.task_utils import create_task_record
    from app.utils.task_utils import update_task_status

    record_stage(
        db,
        media_file_id,
        "transcription",
        new_run=True,
        **transcription_facts("local", None),
    )
    task_id = str(uuid.uuid4())
    create_task_record(db, task_id, user_id, media_file_id, "transcription")
    update_task_status(db, task_id, TASK_STATUS_COMPLETED, progress=1.0, completed=True)
    db.commit()


def generate_import_thumbnail(
    media_file_id: int, user_id: int, storage_path: str, content_type: str
) -> bool:
    """Run the production thumbnail task in this process; ``False`` when it made none.

    Audio has no frame to show, so it is skipped (as at upload). The task is the one
    ``upload_dispatch.dispatch_thumbnail_for_video`` and ``backfill_missing_thumbnails``
    dispatch; ``apply`` runs it here because the seed container exits when the command does.
    """
    if not content_type.startswith("video/"):
        return True
    from app.tasks.thumbnail import generate_thumbnail_task

    result = generate_thumbnail_task.apply(
        kwargs={"file_id": media_file_id, "user_id": user_id, "storage_path": storage_path}
    ).result
    return bool(isinstance(result, dict) and result.get("success"))
