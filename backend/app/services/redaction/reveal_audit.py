"""The ``transcript.view_unredacted`` compliance event, written by ONE implementation.

It lived inline in ``files/crud.py::_resolve_redaction_for_request`` while
``files/subtitles.py``, the path that writes the revealed transcript to a **file on the user's
disk**, wrote no audit event at all (issue #85). Two copies of a compliance trail is how one of
them silently stops matching the other. It lives here, in a service, so a Celery task (the
DOCX/PDF export worker) can call it too: a worker cannot import an API module.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.media import MediaFile
    from app.models.user import User

logger = logging.getLogger(__name__)


def audit_unredacted_reveal(
    db_file: MediaFile, current_user: User, reveal: set[str], *, surface: str
) -> None:
    """Write the ``transcript.view_unredacted`` compliance event, if anything revealed.

    Args:
        db_file: The file whose transcript was revealed.
        current_user: Who revealed it.
        reveal: Categories actually revealed. Empty means nothing was revealed, so no event.
        surface: Which read surface (``transcript`` | ``subtitle_export`` | ``transcript_export``
            | ``transcript_document_export``). Admin-forced categories never appear here; they
            cannot be revealed on any surface.
    """
    if not reveal:
        return
    try:
        from app.auth.audit import AuditEventType
        from app.auth.audit import AuditOutcome
        from app.auth.audit import audit_logger

        audit_logger.log(
            event_type=AuditEventType.TRANSCRIPT_VIEW_UNREDACTED,
            outcome=AuditOutcome.SUCCESS,
            user_id=current_user.id,
            username=str(current_user.email),
            # Org attribution (issue #262a): the reveal concerns this file's
            # tenant, so its org admins see it in the org audit read.
            organization_id=int(db_file.organization_id) if db_file.organization_id else None,
            details={
                "file_id": db_file.id,
                "file_uuid": str(db_file.uuid),
                "revealed_categories": sorted(reveal),
                "surface": surface,
            },
        )
    except Exception as e:
        # Audit failure must never turn a permitted read into an error; it is logged loudly.
        logger.warning("Audit log for an unredacted reveal failed: %s", type(e).__name__)
