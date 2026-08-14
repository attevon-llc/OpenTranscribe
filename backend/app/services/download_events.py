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


def download_prep_guard_key(file_id: int, mode: str, variant: str = "") -> str:
    """Redis NX key that collapses duplicate prepare dispatches for one (file, mode).

    Lives here, beside the channel helpers, because **two** places must agree on it:
    the endpoint sets it (``_ensure_prepare_enqueued``) and the worker task clears it
    when it finishes (``release_download_prep_guard``). While it was an inline f-string
    in the endpoint only, nothing released it — see that function for what that cost.

    ``variant`` distinguishes derived assets that differ in CONTENT rather than mode —
    today only the redaction fingerprint of a burned-in-subtitle render (issue #85).
    Without it, two readers whose policies mask differently collapse onto one build and
    the second receives the first one's video. It is empty for every audio mode and for
    any deployment that masks nothing, so those keys are unchanged.
    """
    suffix = f":{variant}" if variant else ""
    return f"download:prep:{file_id}:{mode}{suffix}"


def release_download_prep_guard(file_id: int, mode: str, variant: str = "") -> None:
    """Clear the prepare-dispatch guard so the next request can re-dispatch.

    MUST run on every exit path of the prepare task, success or failure. The guard has
    a 900 s expiry as a backstop, but relying on it made a completed-but-unresolvable
    download **permanently unrecoverable for 15 minutes**: the readiness check finds
    nothing, ``NX`` refuses to dispatch because the guard is still set, so the SSE
    stream waits on an event that will never be published and the browser hangs until
    it gives up. Reachable in production precisely because derived assets are
    deliberately short-lived — one expiring inside the guard window is enough.

    Measured while confirming it (issue #431): the same request took 90 s and failed
    with the stale guard held, and 6.35 s once it expired.

    Best-effort. A failure here costs at most one redundant prepare task later, so it
    must never propagate into the task's result.
    """
    try:
        get_redis().delete(download_prep_guard_key(file_id, mode, variant))
    except Exception as e:  # pragma: no cover - guard release is advisory
        logger.warning(f"Could not release download-prep guard for file {file_id}/{mode}: {e}")


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
    variant: str = "",
    message: str = "",
    progress: int = 0,
    url: str | None = None,
    filename: str | None = None,
) -> None:
    """Publish a download event (``processing`` | ``completed`` | ``error``).

    ``completed`` events carry the presigned ``url`` + ``filename`` the browser uses
    to download the prepared asset. Failures are logged, never raised.

    The channel is per FILE, so every reader of that file sees every event on it — the
    subscriber filters on ``(mode, variant)``. ``variant`` is what keeps a burned-in
    video masked under one reader's policy from being handed to a reader with another
    (issue #85); it is always ``""`` for audio modes and unmasked deployments.
    """
    payload: dict[str, object] = {
        "status": status,
        "mode": mode,
        "variant": variant,
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
