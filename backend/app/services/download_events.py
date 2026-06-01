"""Dedicated pub/sub channel for media-download preparation events.

The download worker publishes progress / ready / error events for a specific file
here; the SSE endpoint (``GET /files/{uuid}/download-stream``) subscribes and pushes
them to the browser. Kept separate from the global ``websocket_notifications`` channel
so each short-lived SSE connection only receives events for its own download instead
of the whole app's notification firehose.
"""

import json
import logging

from app.core.redis import get_redis

logger = logging.getLogger(__name__)


def download_events_channel(file_uuid: str) -> str:
    """Redis pub/sub channel carrying download events for one file."""
    return f"download_events:{file_uuid}"


def bulk_export_channel(job_id: str) -> str:
    """Redis pub/sub channel carrying bulk-export events for one job."""
    return f"download_events:bulk:{job_id}"


def _publish(channel: str, payload: dict) -> None:
    """Fire-and-forget publish to a Redis channel. Failures are logged, never raised."""
    try:
        get_redis().publish(channel, json.dumps(payload))
    except Exception as e:  # pragma: no cover - best-effort notification
        logger.error(f"Failed to publish to {channel}: {e}")


def publish_download_event(
    file_uuid: str,
    *,
    status: str,
    mode: str,
    message: str = "",
    progress: int = 0,
    url: str | None = None,
    filename: str | None = None,
) -> None:
    """Publish a download event (``processing`` | ``completed`` | ``error``).

    ``completed`` events carry the presigned ``url`` + ``filename`` the browser uses
    to download the prepared asset. Failures are logged, never raised.
    """
    payload: dict[str, object] = {
        "status": status,
        "mode": mode,
        "message": message,
        "progress": progress,
    }
    if url:
        payload["url"] = url
    if filename:
        payload["filename"] = filename
    _publish(download_events_channel(file_uuid), payload)


def publish_bulk_event(
    job_id: str,
    *,
    status: str,
    message: str = "",
    progress: int = 0,
    url: str | None = None,
    filename: str | None = None,
    exported: int = 0,
    skipped: int = 0,
) -> None:
    """Publish a bulk subtitle-export event (``processing`` | ``completed`` | ``error``).

    ``completed`` events carry the presigned ZIP ``url`` + ``filename`` and the
    ``exported``/``skipped`` counts the browser surfaces to the user.
    """
    payload: dict[str, object] = {
        "status": status,
        "message": message,
        "progress": progress,
        "exported": exported,
        "skipped": skipped,
    }
    if url:
        payload["url"] = url
    if filename:
        payload["filename"] = filename
    _publish(bulk_export_channel(job_id), payload)
