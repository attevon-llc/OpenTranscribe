"""The durable list of cluster-centroid documents a purge still owes (v461, issue #1334 item 2).

A pass deletes cluster ROWS before their ``cluster_<uuid>`` documents, so after the relational
commit only this list names those documents. Lifecycle of one ``privacy_purge_pending`` row:

1. :func:`record` adds the pass's centroid ids inside the relational transaction, before its
   commit: the cluster rows and the record of their documents commit together or not at all.
2. Every pass reads :func:`cluster_uuids` back as the ids it must delete and measure, then
   :func:`settle` narrows the row to what it could not confirm gone; the final pass deletes the row
   when nothing is left.
3. :func:`reverify` (the ``privacy.purge_reverify`` beat task) takes rows older than
   :data:`REVERIFY_GRACE_SECONDS`: deletes and measures their documents, drops the row once they
   are gone, audits what it did, and marks a purge whose final pass never ended as failed
   (``reason: interrupted``) instead of leaving it in progress, or letting a later purge of the
   same scope measure clean while the documents survive.

Never raises from :func:`settle`: a failure there leaves a wider list, which only means the next
pass or the re-verify deletes an already-deleted document again.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Any

from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.privacy_purge_pending import PrivacyPurgePending

if TYPE_CHECKING:
    from app.services.privacy.purge import PurgeScope

logger = logging.getLogger(__name__)

#: A row this old belongs to a purge whose next pass should long have run: pass 2 starts
#: ``purge.SECOND_PASS_DELAY_SECONDS`` (60 s) after pass 1 ends. An hour also covers a slow pass
#: over a very large scope; re-verifying a live purge is harmless (deleting a deleted document is a
#: no-op, and the purge's own final status overwrites the re-verify's).
REVERIFY_GRACE_SECONDS = 3600
#: Rows per re-verify run; the rest wait for the next tick.
REVERIFY_BATCH = 100
#: The status-record and audit reason of a purge whose final pass never ended.
INTERRUPTED = "interrupted"

_ACTIVE_TASK_STATUSES = ("pending", "in_progress")


def record(db: Session, task_id: str, scope: PurgeScope, cluster_uuids: Iterable[str]) -> None:
    """Add ``cluster_uuids`` to the purge's row in the caller's transaction. Does not commit.

    Creates the row (even with no ids: its existence says a pass committed and the final pass has
    not ended), or merges the ids into it.
    """
    ids = list(dict.fromkeys(str(u) for u in cluster_uuids))
    existing = db.get(PrivacyPurgePending, task_id, with_for_update=True)
    if existing is not None:
        existing.cluster_uuids = list(dict.fromkeys([*existing.cluster_uuids, *ids]))
        return
    db.execute(
        insert(PrivacyPurgePending)
        .values(task_id=task_id, scope=scope.to_payload(), cluster_uuids=ids)
        .on_conflict_do_nothing(index_elements=["task_id"])
    )


def cluster_uuids(db: Session, task_id: str) -> list[str]:
    """The ids the purge's row still lists ([] when there is no row)."""
    row = db.get(PrivacyPurgePending, task_id)
    return list(row.cluster_uuids) if row is not None else []


def settle(
    session_factory: Callable[[], Session],
    task_id: str,
    unfinished: Iterable[str],
    *,
    final: bool,
) -> None:
    """After a pass: keep only ``unfinished``; on the final pass, drop the row if nothing is."""
    remaining = list(dict.fromkeys(str(u) for u in unfinished))
    db = session_factory()
    try:
        if final and not remaining:
            db.execute(delete(PrivacyPurgePending).where(PrivacyPurgePending.task_id == task_id))
        else:
            row = db.get(PrivacyPurgePending, task_id, with_for_update=True)
            if row is not None:
                row.cluster_uuids = remaining
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("privacy purge %s: could not settle the pending centroid list", task_id)
    finally:
        db.close()


@dataclass
class _Due:
    task_id: str
    scope: dict[str, Any]
    cluster_uuids: list[str]
    task_active: bool


def _due_rows(db: Session, now: datetime, grace_seconds: float) -> list[_Due]:
    from app.models.media import Task

    cutoff = now - timedelta(seconds=grace_seconds)
    rows = db.execute(
        select(PrivacyPurgePending, Task.status)
        .outerjoin(Task, Task.id == PrivacyPurgePending.task_id)
        .where(PrivacyPurgePending.created_at <= cutoff)
        .where(
            (PrivacyPurgePending.last_reverify_at.is_(None))
            | (PrivacyPurgePending.last_reverify_at <= cutoff)
        )
        .order_by(PrivacyPurgePending.created_at)
        .limit(REVERIFY_BATCH)
    ).all()
    return [
        _Due(
            task_id=row.task_id,
            scope=dict(row.scope),
            cluster_uuids=list(row.cluster_uuids),
            task_active=status in _ACTIVE_TASK_STATUSES,
        )
        for row, status in rows
    ]


def reverify(
    session_factory: Callable[[], Session],
    *,
    grace_seconds: float = REVERIFY_GRACE_SECONDS,
    now: datetime | None = None,
) -> dict[str, int]:
    """Finish or report every purge whose pending row is older than ``grace_seconds``.

    For each row: delete its centroid documents and count what is left (after a refresh); drop the
    row when nothing is, else record the attempt. A purge whose task row is still pending or in
    progress lost its final pass with its worker: it is marked failed (``interrupted``) whatever
    the documents' state, because that pass is also the one that catches a voiceprint written while
    the switch flipped. One audit event per row, under the purge's own task id and scope.

    Returns:
        ``{"rows", "finished", "remaining", "interrupted"}`` counts for the log.
    """
    from app.auth.audit_events import AuditOutcome
    from app.services.gdpr_voiceprint_erasure import count_speaker_docs_by_ids
    from app.services.gdpr_voiceprint_erasure import delete_speaker_docs_by_ids
    from app.services.privacy import purge
    from app.services.privacy.measure import Residual

    now = now or datetime.now(UTC)
    db = session_factory()
    try:
        due = _due_rows(db, now, grace_seconds)
    finally:
        db.close()

    summary = {"rows": len(due), "finished": 0, "remaining": 0, "interrupted": 0}
    for item in due:
        errors: list[dict[str, Any]] = []
        doc_ids = [f"cluster_{u}" for u in item.cluster_uuids]
        deleted = (
            delete_speaker_docs_by_ids(doc_ids, errors=errors, label="purge re-verify")
            if doc_ids
            else 0
        )
        left = count_speaker_docs_by_ids(doc_ids, errors=errors) if doc_ids else 0

        db = session_factory()
        try:
            if left == 0:
                db.execute(
                    delete(PrivacyPurgePending).where(PrivacyPurgePending.task_id == item.task_id)
                )
            else:
                row = db.get(PrivacyPurgePending, item.task_id, with_for_update=True)
                if row is not None:
                    row.reverify_attempts = int(row.reverify_attempts or 0) + 1
                    row.last_reverify_at = now
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("privacy purge re-verify %s: could not update its row", item.task_id)
        finally:
            db.close()

        summary["finished" if left == 0 else "remaining"] += 1
        if item.task_active:
            summary["interrupted"] += 1
            _report_interrupted(item.task_id, session_factory)

        residual = Residual()
        if left is None:
            residual.unverified.append("cluster_docs")
        else:
            residual.counts["cluster_docs"] = left
        outcome = purge.TargetOutcome(residual=residual)
        outcome.counts[purge.OPENSEARCH]["cluster_docs_deleted"] = deleted
        if item.task_active:
            reason: str | None = INTERRUPTED
        else:
            reason = None if residual.ok else "residual"
        purge.audit_purge(
            scope=purge.PurgeScope.from_payload(item.scope),
            targets=[purge.VOICEPRINTS],
            phase="reverify",
            actor_user_id=None,
            actor_username=None,
            outcome=AuditOutcome.FAILURE if reason else AuditOutcome.SUCCESS,
            task_id=item.task_id,
            outcomes={purge.VOICEPRINTS: outcome},
            reason=reason,
        )
    return summary


def _report_interrupted(task_id: str, session_factory: Callable[[], Session]) -> None:
    """Fail a purge whose final pass never ended, and say why in its status record."""
    from app.services.privacy import purge
    from app.utils.task_utils import TASK_STATUS_FAILED
    from app.utils.task_utils import update_task_status

    db = session_factory()
    try:
        update_task_status(
            db, task_id, TASK_STATUS_FAILED, error_message=INTERRUPTED, completed=True
        )
    except Exception:
        db.rollback()
        logger.exception("privacy purge re-verify %s: could not fail the task row", task_id)
    finally:
        db.close()
    previous = purge.read_record(task_id) or {}
    residual = previous.get("residual")
    purge.write_record(
        task_id,
        previous.get("user_id"),
        previous.get("counts") or {},
        targets=previous.get("targets"),
        residual=residual if isinstance(residual, dict) else None,
        residual_ok=previous.get("residual_ok"),
        residual_unverified=previous.get("residual_unverified"),
        reason=INTERRUPTED,
    )
