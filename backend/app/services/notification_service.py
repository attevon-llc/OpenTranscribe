"""Unified WebSocket notification service for Celery tasks.

All task notification functions delegate to ``send_ws_event`` from
``utils/websocket_notify.py``. This module eliminates the per-task
notification wrappers that were duplicated across 10+ task files.
"""

import logging
from typing import Any

from app.utils.websocket_notify import send_ws_event

logger = logging.getLogger(__name__)


def _get_file_metadata_safe(file_id: int) -> dict[str, Any]:
    """Fetch file metadata for notifications, returning empty dict on failure."""
    try:
        from app.tasks.transcription.notifications import get_file_metadata

        return get_file_metadata(file_id)
    except Exception as e:
        logger.debug("Could not fetch file metadata for notification: %s", e)
        return {}


def send_task_notification(
    user_id: int,
    event_type: str,
    *,
    status: str = "",
    message: str = "",
    file_id: int | None = None,
    progress: int | float = 0,
    extra: dict[str, Any] | None = None,
) -> bool:
    """Send a task status notification via WebSocket.

    This is the single entry point for all task→frontend notifications.

    Args:
        user_id: Target user ID.
        event_type: WebSocket event type (e.g. ``summarization_status``).
        status: Status string (processing, completed, failed, etc.).
        message: Human-readable status message.
        file_id: Optional database file ID — if provided, file metadata
            (filename, content_type, file_size, file_uuid) is auto-attached.
        progress: Progress percentage (0-100).
        extra: Additional key-value pairs merged into the notification payload.

    Returns:
        True on success, False on failure.
    """
    data: dict[str, Any] = {}

    if file_id is not None:
        # Abuse/DMCA (issue #817): a quarantined file's filename must not reach
        # a non-admin recipient through this funnel any more than it reaches
        # them through a list/search/detail read. This is the SHARED chokepoint
        # for 10+ task files, so the check lives here rather than at each
        # caller. `takedown_service._notify_owner_takedown`/`_notify_owner_release`
        # (the DMCA §512(g) owner notices) never pass `file_id` — they carry the
        # file's identity in `extra` instead — so they are never suppressed by
        # this branch and the owner always learns about their own takedown.
        from app.services.takedown_service import is_notification_suppressed

        if is_notification_suppressed(file_id, user_id):
            return False

        metadata = _get_file_metadata_safe(file_id)
        if metadata:
            data["file_id"] = metadata.get("file_uuid")
            data["filename"] = metadata.get("filename", "")
            data["content_type"] = metadata.get("content_type", "")
            data["file_size"] = metadata.get("file_size", 0)

    if status:
        data["status"] = status
    if message:
        data["message"] = message
    if progress:
        data["progress"] = progress

    if extra:
        data.update(extra)

    return send_ws_event(user_id, event_type, data)


def send_file_cache_invalidation(user_id: int, file_uuid: str) -> bool:
    """Send a per-file cache invalidation notification via WebSocket.

    Tells the frontend to refresh file details when tags/collections are
    applied to a specific file (e.g. auto-labeling).
    """
    return send_ws_event(
        user_id,
        "cache_invalidate",
        {"scope": "files", "file_id": file_uuid},
    )
