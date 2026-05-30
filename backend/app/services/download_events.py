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
    try:
        get_redis().publish(download_events_channel(file_uuid), json.dumps(payload))
    except Exception as e:  # pragma: no cover - best-effort notification
        logger.error(f"Failed to publish download event for {file_uuid}: {e}")
