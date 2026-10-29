"""WebSocket completion signal for retroactive speaker matching."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def send_bulk_update_notification(
    trigger: dict[str, Any], auto_applied_count: int, suggested_count: int
) -> None:
    """Send WebSocket notification about bulk speaker updates.

    Takes the labeled speaker as plain data: the caller has committed by this point,
    and reading an attribute off an expired ORM instance would open a fresh
    transaction just to render a notification.

    ``speakers_bulk_updated`` has no dedicated frontend handler (issue #603) — it is
    absent from ``NotificationType`` in ``frontend/src/stores/websocket.ts``, so it
    falls through the generic unmatched-type path and is stored in
    ``$ws.notifications`` without a targeted UI reaction. Harmless (the per-speaker
    ``speaker_processing_complete``/``speaker_updated`` events already drive the
    file-detail page's live update), but intentionally unhandled rather than an
    oversight — leave it emitting until a UI surface actually consumes it.
    """
    if auto_applied_count == 0:
        return

    try:
        import asyncio

        from app.api.websockets import publish_notification

        coro = publish_notification(
            user_id=trigger["user_id"],
            notification_type="speakers_bulk_updated",
            data={
                "trigger_speaker_id": trigger["id"],
                "display_name": str(trigger["display_name"]),
                "auto_applied_count": auto_applied_count,
                "suggested_count": suggested_count,
                "message": f"Auto-applied '{trigger['display_name']}' to {auto_applied_count} additional speakers",
            },
        )

        # This sync function runs in a threadpool, so schedule on the main event loop
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(coro)
        except RuntimeError:
            # No running loop in this thread - use run() to create one
            asyncio.run(coro)

        logger.info(
            f"Sent WebSocket notification for bulk speaker update: {auto_applied_count} speakers"
        )
    except Exception as e:
        logger.warning(f"Failed to send WebSocket notification for bulk speaker update: {e}")
