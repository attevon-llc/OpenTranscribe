"""Destroy trashed files whose window has ended (issue #1101).

The body of the hourly ``files.purge_trash`` beat task and of ``files.purge_trash_now`` (the
"delete forever" dispatch). Modelled on the retention sweep (``tasks/cleanup``): phase 1 is one
short session that selects ``(id, uuid)``; phase 2 is one session PER FILE that re-loads the
row ``FOR UPDATE`` under the same predicate (a restore or a new hold may have landed between
the two phases) and hands it to the canonical destroy, ``purge_media_file``, with that predicate
as ``eligible``. That lock does NOT cover the destroy: ``purge_media_file`` commits before its
storage/OpenSearch phase, which releases it. ``purge_media_file`` therefore re-locks the row and
re-checks the hold and ``eligible`` right before the row delete, and keeps the row (reported
``aborted_after_destroy``, audited ``partial`` here) when a hold or quarantine landed meanwhile.

The predicate (:func:`_due`) is the whole safety argument: trashed, ``purge_after <=
clock_timestamp()``,
held by nobody, not quarantined. A restore only ever updates rows with ``purge_after > clock_timestamp()``
(``trash_service``), so a row this selects can no longer be restored, and running the purge
twice is harmless: the second run finds no row. ``purge_media_file`` refuses a held file as
well, as the fail-closed backstop.

The ``file.purge`` audit event is written right AFTER the destroy, through the outbox in its
own short transaction: ``purge_media_file`` commits internally, so the event cannot share its
transaction, and a destroy that happened must be reported, never rolled back.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from sqlalchemy import and_
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql.elements import ColumnElement

from app.auth.audit_events import TENANT_SCOPE_ORG
from app.auth.audit_events import TENANT_SCOPE_PERSONAL
from app.auth.audit_events import AuditEventType
from app.auth.audit_events import AuditOutcome
from app.core.constants import TRASH_PURGE_BATCH_SIZE
from app.db.session_utils import session_scope
from app.models.media import MediaFile
from app.services.hold_guard import not_held_pred

logger = logging.getLogger(__name__)


@dataclass
class PurgeSummary:
    """What one purge pass did. ``failed`` files stay due and are retried on the next tick."""

    selected: int = 0
    purged: int = 0
    skipped: int = 0
    failed: int = 0
    residual_errors: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "selected": self.selected,
            "purged": self.purged,
            "skipped": self.skipped,
            "failed": self.failed,
            "residual_errors": len(self.residual_errors),
        }


def _due() -> ColumnElement[bool]:
    """Trashed, due, held by nobody, not quarantined (takedown evidence is kept)."""
    return and_(
        MediaFile.deleted_at.is_not(None),
        # The wall clock, never the transaction's now(): see trash_service's docstring.
        MediaFile.purge_after <= func.clock_timestamp(),
        not_held_pred(),
        MediaFile.is_quarantined.is_(False),
    )


def select_due(limit: int, file_ids: Sequence[int] | None = None) -> list[tuple[int, str]]:
    """Phase 1: ``(id, uuid)`` of due files, oldest due first, at most ``limit``."""
    with session_scope() as db:
        query = db.query(MediaFile.id, MediaFile.uuid).filter(_due())
        if file_ids is not None:
            query = query.filter(MediaFile.id.in_([int(i) for i in file_ids]))
        rows = query.order_by(MediaFile.purge_after, MediaFile.id).limit(limit).all()
        return [(int(r[0]), str(r[1])) for r in rows]


def _record_purge(audit: dict[str, Any]) -> None:
    """Stage and commit the ``file.purge`` event; never raises (the destroy already happened)."""
    from app.services import audit_outbox

    try:
        with session_scope() as db:
            audit_outbox.stage(db, **audit)
            db.commit()
            audit_outbox.deliver_staged(db)
    except (SQLAlchemyError, OSError):  # see the module docstring
        logger.error("trash purge: could not record file.purge for %s", audit, exc_info=True)


def purge_one(
    file_id: int, *, trigger: str, defer_citation_scrub: list[str] | None = None
) -> tuple[str, list[dict[str, Any]]]:
    """Phase 2 for one file: ``("purged" | "skipped" | "failed", residual_errors)``.

    ``defer_citation_scrub`` is forwarded to ``purge_media_file`` so a batch can scrub once.
    """
    from sqlalchemy.orm import selectinload

    from app.services.file_cleanup_service import purge_media_file

    with session_scope() as db:
        media_file = (
            db.query(MediaFile)
            .options(selectinload(MediaFile.speakers))
            .filter(MediaFile.id == file_id, _due())
            .with_for_update(of=MediaFile)
            .first()
        )
        if media_file is None:
            return "skipped", []
        org_id = media_file.organization_id
        details: dict[str, Any] = {
            "file_id": int(media_file.id),
            "origin": media_file.trash_origin,
            "trigger": trigger,
        }
        audit: dict[str, Any] = {
            "event_type": AuditEventType.FILE_PURGE,
            "outcome": AuditOutcome.SUCCESS,
            "user_id": None,
            "username": "system",
            "organization_id": int(org_id) if org_id is not None else None,
            "target_user_id": int(media_file.user_id),
            "resource_type": "media_file",
            "resource_uuid": str(media_file.uuid),
            "tenant_scope": TENANT_SCOPE_ORG if org_id is not None else TENANT_SCOPE_PERSONAL,
            "details": details,
        }
        result = purge_media_file(
            db, media_file, defer_citation_scrub=defer_citation_scrub, eligible=_due()
        )
    if result.get("already_deleted"):
        # An overlapping purge finished this file first and audited it; the row is gone, so
        # nothing was kept and there is no partial purge to record.
        return "skipped", []
    if not result.get("deleted"):
        logger.error("trash purge: file %s not purged: %s", file_id, result.get("error"))
        if result.get("aborted_after_destroy"):
            # Its external copies were destroyed before a hold (or quarantine) kept the row:
            # that destroy happened and is reported, as a partial purge.
            details["aborted_after_destroy"] = True
            details["kept_by"] = "legal_hold" if result.get("refused_legal_hold") else "selection"
            _record_purge({**audit, "outcome": AuditOutcome.PARTIAL})
        return "failed", []
    residual = list(result.get("residual_errors") or [])
    if residual:
        details["residual_errors"] = len(residual)
    _record_purge(audit)
    return "purged", residual


def purge_due(
    *, limit: int = TRASH_PURGE_BATCH_SIZE, file_ids: Sequence[int] | None = None
) -> PurgeSummary:
    """Purge every due trashed file (or only ``file_ids`` among them), one session per file."""
    summary = PurgeSummary()
    trigger = "due" if file_ids is None else "requested"
    candidates = select_due(limit, file_ids)
    summary.selected = len(candidates)
    from app.services.file_cleanup_service import scrub_deferred_citations

    destroyed: list[str] = []
    try:
        _purge_candidates(candidates, trigger, summary, destroyed)
    finally:
        scrub_deferred_citations(destroyed)
    return summary


def _purge_candidates(
    candidates: Sequence[tuple[int, str]],
    trigger: str,
    summary: PurgeSummary,
    destroyed: list[str],
) -> None:
    for file_id, file_uuid in candidates:
        try:
            outcome, residual = purge_one(file_id, trigger=trigger, defer_citation_scrub=destroyed)
        except Exception:  # one bad file must not stop the pass  # boundary: per-item isolation, counted in summary.failed
            logger.exception("trash purge: failed on file %s (%s)", file_id, file_uuid)
            summary.failed += 1
            continue
        if outcome == "purged":
            summary.purged += 1
            summary.residual_errors.extend(residual)
        elif outcome == "skipped":
            summary.skipped += 1
        else:
            summary.failed += 1


__all__ = ["PurgeSummary", "purge_due", "purge_one", "select_due"]
