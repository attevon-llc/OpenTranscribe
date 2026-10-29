"""Delivery of GPU-migration progress/completion events to the admin who started the run."""

import logging

from app.utils.websocket_notify import send_ws_event

logger = logging.getLogger(__name__)


def notify_migration_admin(user_id: int | None, event_type: str, payload: dict) -> None:
    """Send a migration WS event to the requesting admin, or log-only.

    ``user_id`` is None either for a deployment-wide migration with no specific
    admin watching, or for a batch task whose orchestrator itself had none.
    Falling back to account id 1 (issue #908, finding B) misdirected admin-only
    migration progress/completion events — which include ``failed_files``, a
    list of file UUIDs — to whichever account happens to hold that id, which
    need not be an admin. Matches the pattern already fixed for this exact bug
    in ``opensearch_integrity_task.py``'s ``_notify``.
    """
    if user_id is None:
        logger.debug(f"{event_type}: no requesting admin for this run, logging only")
        return
    send_ws_event(user_id, event_type, payload)
