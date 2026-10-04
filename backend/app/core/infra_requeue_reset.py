"""Clear a file's infrastructure-requeue count when its status becomes terminal (issue #1162).

``task_liveness.INFRA_REQUEUES_KEY`` counts how often a dead worker's run of a file was put
back. The count has to go when the run ends, or ``transcription_files_infra_requeued`` keeps
reporting the file and the poison-loop alert never clears; a later re-run of the file would
also start with a spent budget.

Terminal status writes are scattered: the success path, ``transcription_retry._fail_file``,
``on_pipeline_error``, the recovery sweeps, ``finish_cancelled``, ``reconcile_cancellation``,
the unarmed-cancel fallback (which assigns ``status`` directly). Calling the clear from each was
how two of them were missed. Every one of them is an ORM write of ``MediaFile.status``, so the
clear hangs off the session instead:

* ``after_flush`` records the uuid of every file whose ``status`` was just written to a terminal
  value;
* ``after_commit`` clears those files' counters, so a write that is rolled back never resets the
  poison budget;
* ``after_rollback`` forgets them.

A retry never writes a terminal status between attempts (``_retry_file`` writes ``PENDING`` and
a worker-loss requeue does not touch the status), so the budget still accumulates across a
poison loop. A terminal write by a non-transcription task (rediarize, a URL import) also clears
the entry, which is harmless: no transcription run of that file is in flight then.
"""

from __future__ import annotations

import logging

from sqlalchemy import event
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.core.enums import FileStatus

logger = logging.getLogger(__name__)

#: A tuple, not a set: ``FileStatus`` hashes by member name, so a raw ``"error"`` string would
#: miss a set lookup while ``==`` (which tuple membership uses) matches it.
TERMINAL_FILE_STATUSES = (FileStatus.COMPLETED, FileStatus.ERROR, FileStatus.CANCELLED)

_PENDING_KEY = "infra_requeue_clear_file_uuids"

_registered = False


def _terminal_status_written(obj) -> bool:
    added = inspect(obj).attrs.status.history.added
    return any(value in TERMINAL_FILE_STATUSES for value in added)


def _collect(session: Session, _flush_context) -> None:
    from app.models.media import MediaFile

    for obj in list(session.new) + list(session.dirty):
        if not isinstance(obj, MediaFile):
            continue
        try:
            if _terminal_status_written(obj) and obj.uuid is not None:
                session.info.setdefault(_PENDING_KEY, set()).add(str(obj.uuid))
        except Exception as e:  # noqa: BLE001 - bookkeeping must never fail a status write
            logger.debug("Could not inspect a file status write: %s", e)


def _clear(session: Session) -> None:
    file_uuids = session.info.pop(_PENDING_KEY, None)
    if not file_uuids:
        return
    from app.core.task_liveness import clear_infra_requeues

    for file_uuid in file_uuids:
        clear_infra_requeues(file_uuid)  # never raises: logs and moves on


def _forget(session: Session) -> None:
    session.info.pop(_PENDING_KEY, None)


def register() -> None:
    """Attach the listeners to every ``Session`` in the process (idempotent)."""
    global _registered
    if _registered:
        return
    event.listen(Session, "after_flush", _collect)
    event.listen(Session, "after_commit", _clear)
    event.listen(Session, "after_rollback", _forget)
    _registered = True
