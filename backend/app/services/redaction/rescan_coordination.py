"""Hand a file between segment edits and the redaction worker's full re-scan.

``media_file.redaction_status`` is the only record of whether a file's cached spans can be
trusted, and two writers race on it: a segment edit (API process) and ``detect_and_store``
(redaction worker). Three rules keep every interleaving fail-closed:

1. **The edit decides under a row lock.** :func:`lock_redaction_status` re-reads the status
   with ``SELECT ... FOR UPDATE`` just before the edit's own commit (PostgreSQL docs,
   "Explicit Locking" -> "Row-Level Locks"). Without it, an edit could read ``processing``,
   the scan could then commit ``done``, and the edit would commit ``pending`` with no scan
   queued: withheld forever.
2. **The scan's terminal write is conditional.** :func:`finish_scan` writes ``done`` /
   ``failed`` only ``WHERE redaction_status = 'processing'``. Under READ COMMITTED a blocked
   ``UPDATE`` re-checks its ``WHERE`` against the row the edit committed (PostgreSQL docs,
   "Transaction Isolation" -> "Read Committed Isolation Level"), so an edit that landed while
   the scan was reading the OLD text makes the write lose, and the scan re-queues itself
   instead of caching old-text spans under ``done`` (``RedactionService.queue_rescan``).
3. **The status is the debounce claim.** A file already ``pending`` has a scan queued that has
   not read the text yet, so a later edit rides it. The scan is queued with a ``countdown``
   (Celery docs, "Calling Tasks" -> "ETA and Countdown"), so a burst of edits within the
   window queues one full re-scan rather than one per edit. A Redis claim, as
   ``analytics_dispatch`` uses, would be a second claim beside the durable one the readers
   already honour.

Both writers lock ``media_file`` before ``transcript_segment`` (the edit's FOR UPDATE, the
scan's conditional UPDATE, then each one's segment flush at commit), so they cannot deadlock
(PostgreSQL docs, "Explicit Locking" -> "Deadlocks": acquire locks in a consistent order).
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core import redaction_defaults
from app.models.media import MediaFile

logger = logging.getLogger(__name__)

# A scan already queued (not started) or running covers an edit; see rules 2 and 3 above.
SCAN_COMING = frozenset(
    {redaction_defaults.REDACTION_STATUS_PENDING, redaction_defaults.REDACTION_STATUS_PROCESSING}
)


def lock_redaction_status(db: Session, media_file: MediaFile) -> str | None:
    """Re-read ``media_file.redaction_status`` under a row lock held until the caller commits."""
    db.refresh(media_file, attribute_names=["redaction_status"], with_for_update=True)
    return media_file.redaction_status


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
