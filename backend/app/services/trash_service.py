"""The trash: every delete a person makes is reversible for a window (issue #1101).

Trash is a lifecycle state on the ``media_file`` row (``deleted_at``, ``deleted_by_user_id``,
``purge_after``, ``trash_origin``; v463), never a ``status`` value and never a second table. A
trashed file is unavailable on every normal read surface (``services/file_availability``);
only the trash routes reach it, through ``services/trash_access``.

State changes live here, one transaction each, with their governance audit events staged in
that same transaction (``content_audit.stage_content_event``, the v459 outbox):

* :func:`trash_files` - refuses a held file (``hold_guard``; a hold blocks deletion and the
  trash is the deletion entry point). A quarantined file may be trashed; the takedown stays.
* :func:`restore_files` - one conditional ``UPDATE`` (below).
* :func:`mark_for_immediate_purge` - "delete forever": makes the rows due now; the purge task
  (``services/trash_purge``) destroys them. Refuses held and quarantined rows.

**The restore/purge race is closed by one invariant on ``purge_after``, not by a lock held
across storage calls** (``purge_media_file`` commits before its storage phase). Purge only
takes rows with ``purge_after <= clock_timestamp()``; restore only updates rows with
``purge_after > clock_timestamp()`` (or held/quarantined rows, which purge never takes), inside
one ``UPDATE ... WHERE``. Time only moves forward, so a due row can never be restored and a
restorable row is never due. Under READ COMMITTED a concurrent UPDATE of the same row waits
for the lock and re-evaluates its WHERE against the committed row (PostgreSQL docs, "UPDATE"
and "Row-Level Locks"), which is what makes the conditional UPDATE race-safe.

``clock_timestamp()``, never ``now()``: ``now()`` is the start of the TRANSACTION (PostgreSQL
docs, "Current Date/Time"), so a restore whose request transaction began a moment before
``purge_after`` would still read the row as not due after the purge had taken it, and bring
back a row whose objects were already being destroyed. The wall clock at evaluation has no
such window.

Search, chat retrieval and speaker matching learn the new state through the ``availability``
field on the file's OpenSearch documents (``search/availability_stamp``), rewritten in place
right after each trash/restore commit. A failed stamp never undoes the commit: the Postgres
post-filter keeps hits out meanwhile and ``search.availability_reconcile`` converges the index.

Retention expiry does **not** come through here: it keeps hard-deleting at N days (owner
decision OD-1 B), so ``trash_origin`` is only ever ``'user'`` from this module.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Any

from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.auth.audit_events import AuditEventType
from app.core.constants import NOTIFICATION_TYPE_FILE_RESTORED
from app.core.constants import NOTIFICATION_TYPE_FILE_TRASHED
from app.models.media import MediaFile
from app.services import audit_outbox
from app.services.content_audit import AuditedResource
from app.services.content_audit import stage_content_event
from app.services.hold_guard import assert_not_held
from app.services.hold_guard import held_pred
from app.services.hold_guard import not_held_pred

if TYPE_CHECKING:
    from app.models.user import User
    from app.services.content_audit import AuditRequest as Request
    from app.services.platform_bypass import PlatformBypass

logger = logging.getLogger(__name__)

TRASH_ORIGIN_USER = "user"

#: 409 ``error`` codes of the trash routes.
NOT_RESTORABLE = "NOT_RESTORABLE"
FILE_NOT_IN_TRASH = "FILE_NOT_IN_TRASH"
FILE_UNDER_TAKEDOWN = "FILE_UNDER_TAKEDOWN"


def trash_retention_days(db: Session) -> int:
    """The admin-set trash window in days (``files.trash_retention_days``)."""
    from app.services.system_settings_service import get_trash_retention_days

    return get_trash_retention_days(db)


def _db_now(db: Session) -> Any:
    """The database's wall clock, so a stamp and every later SQL comparison share one clock."""
    return db.execute(select(func.clock_timestamp())).scalar_one()


def _stage(
    db: Session,
    request: Request | None,
    event: AuditEventType,
    actor: User,
    file: MediaFile,
    bypass: PlatformBypass | None,
    details: dict[str, Any],
) -> None:
    stage_content_event(
        db,
        request,
        event=event,
        actor=actor,
        resource=AuditedResource.file(file),
        details=details,
        bypass=bypass,
    )


def trash_files(
    db: Session,
    files: Sequence[MediaFile],
    *,
    actor: User,
    request: Request | None,
    bypass: PlatformBypass | None,
) -> list[MediaFile]:
    """Move ``files`` to the trash in ONE transaction; return the files this call trashed.

    The rows are locked ``FOR UPDATE`` before the hold check, so a hold placed concurrently
    (an organization hold's ``content_hold_file`` insert takes ``FOR KEY SHARE`` on the row,
    a platform hold updates it) either lands first and is seen, or waits for this commit. A
    file already in the trash is left as it is (its window is not restarted).

    Raises:
        HoldRefusedError: Any of the files is held; nothing is trashed.
    """
    ids = sorted({int(f.id) for f in files})
    if not ids:
        return []
    locked = (
        db.query(MediaFile).filter(MediaFile.id.in_(ids)).order_by(MediaFile.id).with_for_update()
    ).all()
    live = [f for f in locked if f.deleted_at is None]
    # Raises HoldRefusedError before anything is written; the caller's transaction (and with
    # it the row locks) ends with the request.
    assert_not_held(db, [int(f.id) for f in live])
    if not live:
        db.commit()
        return []

    now = _db_now(db)
    purge_after = now + timedelta(days=trash_retention_days(db))
    for f in live:
        f.deleted_at = now
        f.deleted_by_user_id = int(actor.id)
        f.purge_after = purge_after
        f.trash_origin = TRASH_ORIGIN_USER
        _stage(
            db,
            request,
            AuditEventType.FILE_TRASH,
            actor,
            f,
            bypass,
            {"file_id": int(f.id), "origin": TRASH_ORIGIN_USER},
        )
    db.commit()
    audit_outbox.deliver_staged(db)
    _after_trash(db, live, actor)
    return live


def _after_trash(db: Session, trashed: Sequence[MediaFile], actor: User) -> None:
    """Best-effort side effects of a committed trash: none of them can undo it."""
    from app.utils.task_cancel import cancel_active_task

    for f in trashed:
        if f.active_task_id:
            # Trash is reversible, so an in-flight task is cancelled, not refused.
            try:
                cancel_active_task(db, int(f.id))
            except Exception:  # noqa: BLE001 - the file is trashed either way
                logger.warning("trash: could not cancel the task of file %s", f.uuid, exc_info=True)
    _after_state_change(db, trashed, actor, NOTIFICATION_TYPE_FILE_TRASHED)


def restore_files(
    db: Session,
    files: Sequence[MediaFile],
    *,
    actor: User,
    request: Request | None,
    bypass: PlatformBypass | None,
) -> tuple[list[MediaFile], list[MediaFile]]:
    """Bring ``files`` back from the trash; return ``(restored, not_restorable)``.

    Restorable = trashed and either not yet due, or held / quarantined (which the purge never
    takes, so a hold freezes a file in the trash and it stays restorable). A file past its
    ``purge_after`` is the purge's, and is reported ``not_restorable``. A file comes back with
    the processing ``status`` it had: trash never touched it.
    """
    by_id = {int(f.id): f for f in files}
    if not by_id:
        return [], []
    restored_ids = set(
        db.execute(
            update(MediaFile)
            .where(
                MediaFile.id.in_(sorted(by_id)),
                MediaFile.deleted_at.is_not(None),
                or_(
                    MediaFile.purge_after > func.clock_timestamp(),
                    held_pred(),
                    MediaFile.is_quarantined.is_(True),
                ),
            )
            .values(deleted_at=None, deleted_by_user_id=None, purge_after=None, trash_origin=None)
            .returning(MediaFile.id)
            .execution_options(synchronize_session=False)
        ).scalars()
    )
    restored = [by_id[i] for i in sorted(restored_ids)]
    for f in restored:
        _stage(db, request, AuditEventType.FILE_RESTORE, actor, f, bypass, {"file_id": int(f.id)})
    db.commit()
    for f in restored:
        db.refresh(f)
    audit_outbox.deliver_staged(db)
    if restored:
        _after_state_change(db, restored, actor, NOTIFICATION_TYPE_FILE_RESTORED)
    return restored, [f for i, f in sorted(by_id.items()) if i not in restored_ids]


def _mark_due(
    db: Session,
    files: Sequence[MediaFile],
    *,
    actor: User,
    request: Request | None,
    bypass: PlatformBypass | None,
) -> tuple[list[MediaFile], list[MediaFile]]:
    """Stage "delete forever" for ``files`` in the open transaction; the caller commits."""
    by_id = {int(f.id): f for f in files}
    if not by_id:
        return [], []
    marked_ids = set(
        db.execute(
            update(MediaFile)
            .where(
                MediaFile.id.in_(sorted(by_id)),
                MediaFile.deleted_at.is_not(None),
                not_held_pred(),
                MediaFile.is_quarantined.is_(False),
            )
            .values(purge_after=func.least(MediaFile.purge_after, func.clock_timestamp()))
            .returning(MediaFile.id)
            .execution_options(synchronize_session=False)
        ).scalars()
    )
    marked = [by_id[i] for i in sorted(marked_ids)]
    for f in marked:
        _stage(
            db,
            request,
            AuditEventType.FILE_PURGE_REQUESTED,
            actor,
            f,
            bypass,
            {"file_id": int(f.id)},
        )
    return marked, [f for i, f in sorted(by_id.items()) if i not in marked_ids]


def mark_for_immediate_purge(
    db: Session,
    files: Sequence[MediaFile],
    *,
    actor: User,
    request: Request | None,
    bypass: PlatformBypass | None,
) -> tuple[list[MediaFile], list[MediaFile]]:
    """Make trashed files due now ("delete forever"); return ``(marked, refused)``.

    Only trashed rows that nobody holds and that are not quarantined are marked: those two are
    never purged, so marking them would only make them unrestorable for nothing. The caller
    dispatches the purge task for the marked ids (:func:`dispatch_purge`) after this commit.
    """
    marked, refused = _mark_due(db, files, actor=actor, request=request, bypass=bypass)
    db.commit()
    audit_outbox.deliver_staged(db)
    return marked, refused


def empty_trash(
    db: Session,
    files: Sequence[MediaFile],
    *,
    actor: User,
    request: Request | None,
    bypass: PlatformBypass | None,
    org_id: int | None,
) -> list[MediaFile]:
    """Empty the trash: :func:`mark_for_immediate_purge` for the caller's purgeable trash.

    Plus one ``file.trash.empty`` event with the counts, in the same transaction.
    """
    marked, refused = _mark_due(db, files, actor=actor, request=request, bypass=bypass)
    stage_content_event(
        db,
        request,
        event=AuditEventType.TRASH_EMPTY,
        actor=actor,
        resource=_trash_resource(db, actor, org_id),
        details={"marked": len(marked), "kept": len(refused)},
        bypass=bypass,
    )
    db.commit()
    audit_outbox.deliver_staged(db)
    return marked


def _trash_resource(db: Session, actor: User, org_id: int | None) -> AuditedResource:
    """The trash being emptied: the organization's, or the actor's personal one."""
    if org_id is None:
        return AuditedResource("trash", str(actor.uuid), None, int(actor.id))
    from app.models.organization import Organization

    org = db.get(Organization, org_id)
    return AuditedResource("trash", str(org.uuid) if org else "", org_id, None)


def dispatch_purge(file_ids: Sequence[int]) -> None:
    """Queue the purge of files already made due. The hourly beat tick is the guarantee."""
    if not file_ids:
        return
    from app.tasks.trash import purge_trash_now

    try:
        purge_trash_now.delay(file_ids=[int(i) for i in file_ids])
    except Exception:  # noqa: BLE001 - the rows are due; the beat tick purges them
        logger.warning("trash: purge dispatch failed; the hourly tick will purge", exc_info=True)


def _after_state_change(
    db: Session,
    files: Sequence[MediaFile],
    actor: User,
    notification_type: str,
) -> None:
    """Stamp the index, invalidate the caches a trash or restore changes, announce it.

    The stamp comes first so the corpus-version bump below makes chat re-retrieve against an
    index that already agrees.
    """
    # From each row, so restoring a quarantined file from the trash stamps it quarantined.
    from app.services.search.availability_stamp import restamp_rows

    restamp_rows(files)
    try:
        from app.services.chat.retrieval_cache import bump_corpus_version

        bump_corpus_version()
    except Exception:  # noqa: BLE001 - a stale cache entry expires on its own
        logger.debug("trash: corpus version bump failed", exc_info=True)
    try:
        from app.services.redis_cache_service import redis_cache

        for owner_id in {int(f.user_id) for f in files}:
            redis_cache.invalidate_user_files(owner_id)
    except Exception:  # noqa: BLE001 - the gallery reconciles on its next fetch
        logger.debug("trash: file-list cache invalidation failed", exc_info=True)
    _notify(db, files, actor, notification_type)


def _notify(db: Session, files: Sequence[MediaFile], actor: User, notification_type: str) -> None:
    """The trash WebSocket event, to the owner, the actor and the organization's admins.

    Plain ``send_ws_event`` on purpose: this event announces the trash state itself, so it
    must not go through the availability-aware sender that drops a trashed file's events.
    """
    from app.models.organization import OrganizationMembership
    from app.utils.websocket_notify import send_ws_event

    try:
        org_ids = {int(f.organization_id) for f in files if f.organization_id is not None}
        admins: dict[int, set[int]] = {}
        if org_ids:
            for org_id, user_id in db.query(
                OrganizationMembership.organization_id, OrganizationMembership.user_id
            ).filter(
                OrganizationMembership.organization_id.in_(org_ids),
                OrganizationMembership.role == "org:admin",
            ):
                admins.setdefault(int(org_id), set()).add(int(user_id))
        for f in files:
            recipients = {int(f.user_id), int(actor.id)}
            if f.organization_id is not None:
                recipients |= admins.get(int(f.organization_id), set())
            data = {
                "file_id": str(f.uuid),
                "deleted_at": f.deleted_at.isoformat() if f.deleted_at else None,
                "purge_after": f.purge_after.isoformat() if f.purge_after else None,
            }
            for user_id in recipients:
                send_ws_event(user_id, notification_type, data)
    except Exception:  # noqa: BLE001 - a missed event is reconciled on the next fetch
        logger.warning("trash: WebSocket notification failed", exc_info=True)


__all__ = [
    "TRASH_ORIGIN_USER",
    "FILE_NOT_IN_TRASH",
    "FILE_UNDER_TAKEDOWN",
    "NOT_RESTORABLE",
    "dispatch_purge",
    "empty_trash",
    "mark_for_immediate_purge",
    "restore_files",
    "trash_files",
    "trash_retention_days",
]
