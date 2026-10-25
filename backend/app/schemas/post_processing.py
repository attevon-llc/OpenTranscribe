"""Wire shape of a file's post-processing task states.

Served by ``GET /api/files/{uuid}/post-processing`` and identical, per entry, to the
``tasks`` of the ``post_processing_status`` WebSocket frame, so a client merges a REST
snapshot exactly as it merges a plan frame. Mirrored by hand in
``frontend/src/lib/notifications/postProcessing.ts``.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.core.enums import PostProcessingState
from app.core.enums import PostProcessingTask


class PostProcessingEntry(BaseModel):
    """One task's latest state. Times are server epoch seconds."""

    state: PostProcessingState
    at: float
    started_at: float | None = None


class PostProcessingStatusResponse(BaseModel):
    """Every task reported for the file's current run.

    ``run_at`` is None when no run is known (never reported, or expired): the client must
    then say it cannot confirm the final steps, never that they finished.
    """

    file_id: str
    run_at: float | None
    tasks: dict[PostProcessingTask, PostProcessingEntry]
