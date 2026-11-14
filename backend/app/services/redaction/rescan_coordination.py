"""Hand a file between segment edits and the redaction worker's full re-scan.

``media_file.redaction_status`` is the only record of whether a file's cached spans can be
trusted, and two writers race on it: a segment edit (API process) and ``detect_and_store``
(redaction worker). The scan's terminal write is therefore conditional: :func:`finish_scan`
writes ``done`` / ``failed`` only ``WHERE redaction_status = 'processing'``. Under READ
COMMITTED a blocked ``UPDATE`` re-checks its ``WHERE`` against the row the edit committed
(PostgreSQL docs, "Transaction Isolation" -> "Read Committed Isolation Level"), so an edit
that landed while the scan was reading the OLD text makes the write lose, and the scan
re-queues itself (``RedactionService.queue_rescan``) instead of caching old-text spans
under ``done``.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core import redaction_defaults
from app.models.media import MediaFile

logger = logging.getLogger(__name__)


def finish_scan(db: Session, file_id: int, values: dict[str, Any]) -> bool:
    """Write a scan's terminal ``values`` and commit, unless an edit superseded the scan.

    Commits either way: the segment spans the scan computed are flushed in the same commit.
    When an edit superseded it, those spans address the old text, but the file stays
    ``pending``, so no reader trusts them and the re-scan replaces them.

    Returns:
        True when the terminal status was written, False when the scan was superseded.
    """
    result = db.execute(
        update(MediaFile)
        .where(
            MediaFile.id == file_id,
            MediaFile.redaction_status == redaction_defaults.REDACTION_STATUS_PROCESSING,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return bool(getattr(result, "rowcount", 0))
