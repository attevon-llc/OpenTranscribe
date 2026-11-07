"""Background work skips a frozen file (legal-hold freeze, docs/design/legal_hold_ux_freeze_plan.md §3.1).

A file is frozen when it is held AND its initial processing run has finished. A run already in
progress when the hold lands finishes (the hold preserves the content as of its end); anything
dispatched afterwards (a summary, topics, a voice match, an auto-label, a rescan) is a separate
write and is skipped, writing nothing. ``hold_guard`` decides; this module only opens the
session a task needs to ask.
"""

from __future__ import annotations

import logging
from typing import Any

from app.db import session_utils
from app.models.media import MediaFile
from app.services.hold_guard import hold_state
from app.services.hold_guard import is_frozen

logger = logging.getLogger(__name__)

#: ``status`` of a task result that did nothing because the file is frozen.
SKIPPED_LEGAL_HOLD = "skipped_legal_hold"


def _file_id(db: Any, file_id: int | None, file_uuid: str | None) -> int | None:
    if file_id is not None:
        return int(file_id)
    if file_uuid is None:
        return None
    row = db.query(MediaFile.id).filter(MediaFile.uuid == str(file_uuid)).first()
    return None if row is None else int(row[0])


def file_is_frozen(*, file_id: int | None = None, file_uuid: str | None = None) -> bool:
    """Whether the file (by id or uuid) is frozen. A file that no longer exists is not."""
    with session_utils.session_scope() as db:
        resolved = _file_id(db, file_id, file_uuid)
        return resolved is not None and is_frozen(db, resolved)


def file_is_held(*, file_id: int | None = None, file_uuid: str | None = None) -> bool:
    """Whether the file is held at all, whatever its status: for work that REPLACES content
    (a manual re-run), which a hold refuses even while a run is in flight."""
    with session_utils.session_scope() as db:
        resolved = _file_id(db, file_id, file_uuid)
        return resolved is not None and hold_state(db, resolved).held


def skipped_for_hold(task: str, **ref: Any) -> dict[str, Any]:
    """The result of a task that skipped a frozen file (and the log line saying so)."""
    logger.info("%s skipped: the file is under a legal hold (%s)", task, ref)
    return {"status": SKIPPED_LEGAL_HOLD, **ref}
