"""Utilities for sending WebSocket notifications via Redis pub/sub.

``send_ws_event`` is the raw primitive for publishing a notification from any
synchronous code (API endpoints, Celery tasks, services): it knows nothing about
quarantine and will happily name a taken-down file to a non-admin recipient. Use it
only for events that carry no ``MediaFile`` identity (a corpus-wide counter, an
admin-migration progress tick with no file list, a DMCA §512(g) owner notice that
is deliberately exempt — see ``takedown_service``'s module docstring).

``send_ws_event_for_file`` is **required** for anything that names a ``MediaFile``
(filename, title, thumbnail, a speaker tied to a recording, a digest/transcript
excerpt): it consults the same quarantine-suppression predicates the read
surfaces (``exclude_quarantined``/``is_hidden_for``) already enforce, so a live WS
push cannot disclose what a taken-down file's 404 already hides (issue #908).

The Redis subscriber in ``app.api.websockets`` picks up the published message and
forwards it to the user's active WebSocket connections.
"""

import json
import logging
import uuid as uuid_pkg
from collections.abc import Sequence

from app.core.redis import get_redis

logger = logging.getLogger(__name__)


def send_ws_event(user_id: int, notification_type: str, data: dict) -> bool:
    """Publish a WebSocket notification to a specific user via Redis pub/sub.

    Args:
        user_id: Internal user ID of the target recipient.
        notification_type: One of the ``NOTIFICATION_TYPE_*`` constants.
        data: Arbitrary payload dict forwarded to the frontend handler.

    Returns:
        True on success, False on failure (errors are logged, never raised).
    """
    try:
        client = get_redis()
        notification = {
            "user_id": user_id,
            "type": notification_type,
            "data": data,
        }
        client.publish("websocket_notifications", json.dumps(notification))
        logger.info(
            "Published WS notification for user %s: %s",
            user_id,
            notification_type,
        )
        return True
    except Exception as e:
        logger.error(
            "Failed to publish WS notification for user %s (%s): %s",
            user_id,
            notification_type,
            e,
        )
        return False


def send_ws_event_for_file(
    user_id: int,
    notification_type: str,
    data: dict,
    *,
    file_id: int | None = None,
    file_uuid: str | uuid_pkg.UUID | None = None,
    file_uuids: Sequence[str | uuid_pkg.UUID] | None = None,
) -> bool:
    """Quarantine-aware twin of :func:`send_ws_event` for a file-scoped event.

    Use this for anything that can identify a ``MediaFile`` — filename, title,
    transcript excerpt, thumbnail, or a speaker label tied to a recording. It
    routes through ``takedown_service``'s suppression predicates before ever
    calling the real :func:`send_ws_event`, so a quarantined file's identity
    cannot reach a non-admin recipient over a live WS push even though the
    same information already 404s on every read surface.

    Exactly ONE of ``file_id``, ``file_uuid``, ``file_uuids`` is required —
    passing zero or more than one raises ``TypeError`` so a file-scoped call
    site with no selector fails LOUD rather than silently degrading into an
    unguarded :func:`send_ws_event` (the entire point of this wrapper).

    ``file_uuids`` mode is for a single event naming MULTIPLE files (e.g. a
    bulk speaker-rename propagation): the caller MUST have already filtered
    the list — and any payload field mirroring it (a ``file_uuids`` array, a
    recomputed count) — through
    ``takedown_service.filter_suppressed_file_uuids``. If ANY uuid passed here
    is still suppressed for this recipient, the WHOLE event is dropped rather
    than partially rewritten — there is no way for this function to know
    which payload fields would need to change to remove just the hidden file.

    Args:
        user_id: Internal user ID of the target recipient.
        notification_type: One of the ``NOTIFICATION_TYPE_*`` constants.
        data: Arbitrary payload dict forwarded to the frontend handler.
        file_id: The MediaFile's Postgres integer id, when that's what the
            caller holds.
        file_uuid: The MediaFile's UUID, when that's what the caller holds.
        file_uuids: Multiple MediaFile UUIDs this one event names; see above.

    Returns:
        True if published, False if suppressed OR the underlying publish
        failed. The two are deliberately indistinguishable here — a caller
        that needs to tell them apart (e.g. to decide whether to retry)
        should consult ``takedown_service.is_notification_suppressed`` (or
        its UUID/batch siblings) itself, rather than retry a suppression.

    Raises:
        TypeError: Zero or more than one of the three selectors was passed.
    """
    selectors_given = sum(x is not None for x in (file_id, file_uuid, file_uuids))
    if selectors_given != 1:
        raise TypeError(
            "send_ws_event_for_file requires exactly one of file_id, file_uuid, "
            f"file_uuids (got {selectors_given}) — a file-scoped WS event must be "
            "able to name a resolved MediaFile"
        )

    # Imported inside the function body (not at module scope) for two reasons:
    # it avoids an import cycle with takedown_service (which itself reaches back
    # into notification_service for the DMCA owner notices), and it keeps this
    # seam monkeypatchable per-call in tests, matching the existing pattern in
    # test_thumbnail_task_quarantine.py.
    from app.services import takedown_service

    if file_id is not None:
        suppressed = takedown_service.is_notification_suppressed(file_id, user_id)
    elif file_uuid is not None:
        suppressed = takedown_service.is_notification_suppressed_for_uuid(file_uuid, user_id)
    else:
        assert file_uuids is not None  # narrowed by the selector-count check above
        visible = takedown_service.filter_suppressed_file_uuids(file_uuids, user_id)
        suppressed = len(visible) != len(list(file_uuids))

    if suppressed:
        # Never log the filename/title/excerpt — only that a send was withheld.
        logger.debug(
            "Suppressing quarantine-gated WS notification for user %s: %s",
            user_id,
            notification_type,
        )
        return False

    return send_ws_event(user_id, notification_type, data)
