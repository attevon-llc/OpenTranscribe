"""Place, release and maintain content holds (issue #1276; platform holds v467).

A hold is a row in ``content_hold`` plus the files it covers in ``content_hold_file``. WHETHER a
file is held is answered by ``hold_guard`` and nowhere else; this module only changes the facts
that answer reads.

Two authorities share every function here: an ORGANIZATION hold (``org_id`` set, the holding
organization's files and collections only) and a PLATFORM hold (``org_id=None``, placed by an
instance administrator on any tenant's file or collection, without a takedown). Only the
"belongs to the organization" backstops differ; the reason bounds, the file cap, the outbox
audit, the object lock and the erasure nudge are the same code.

**Writes never commit.** Every function here adds rows and flushes; the endpoint commits once.
The audit event is staged in that same transaction by :func:`record_hold_change` (the
transactional outbox, v459, issue #1329), so it commits or rolls back with the hold. What must
happen only AFTER the commit (the storage object lock, the "a hold was lifted" nudge to the
erasure sweep, and handing the staged event to the audit sink) is returned in a
:class:`HoldChange` and performed by :func:`finish_hold_change`.

A hold's ``reason`` and ``matter_ref`` are administrator text. They are stored, shown to the
administrators of the hold's authority, and nothing else: not in an audit event, not over a socket, not in
any file or collection response.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.audit_events import AuditEventType
from app.models.collection import Collection
from app.models.collection import CollectionMember
from app.models.content_hold import AUTHORITY_ORGANIZATION
from app.models.content_hold import HOLD_AUTHORITIES
from app.models.content_hold import REASON_MAX_LENGTH
from app.models.content_hold import REASON_MIN_LENGTH
from app.models.content_hold import SCOPE_COLLECTION
from app.models.content_hold import SCOPE_FILE
from app.models.content_hold import ContentHold
from app.models.content_hold import ContentHoldFile
from app.models.media import MediaFile
from app.services import audit_outbox
from app.services.content_audit import AuditedResource
from app.services.content_audit import record_content_event
from app.services.content_audit import stage_content_event
from app.services.hold_guard import HoldState
from app.services.hold_guard import hold_states

if TYPE_CHECKING:
    from fastapi import Request

    from app.models.user import User

#: Most files one file-scoped hold may name in a single request.
MAX_FILES_PER_HOLD = 500


class HoldRequestError(Exception):
    """A hold request that cannot be honoured. ``code`` is stable for the endpoint to map."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


#: Same cap as a collection membership event (``collection.membership.change``).
MAX_AUDITED_FILE_UUIDS = 100


@dataclass
class HoldChange:
    """What a hold write changed, for :func:`finish_hold_change` to do after the commit."""

    hold: ContentHold
    event: AuditEventType
    file_count: int = 0
    lock_paths: list[str] = field(default_factory=list)
    unlock_paths: list[str] = field(default_factory=list)
    unheld_files: list[MediaFile] = field(default_factory=list)
    #: The files the hold covers, for the audit event (ids only, capped like a membership event).
    file_uuids: list[str] = field(default_factory=list)


def clean_reason(reason: str | None, *, what: str = "reason") -> str:
    """The stripped reason, or :class:`HoldRequestError` when its length is out of bounds."""
    text = (reason or "").strip()
    if not REASON_MIN_LENGTH <= len(text) <= REASON_MAX_LENGTH:
        raise HoldRequestError(
            "reason_length",
            f"The {what} must be {REASON_MIN_LENGTH} to {REASON_MAX_LENGTH} characters.",
        )
    return text


def _paths(files: Iterable[MediaFile]) -> list[str]:
    return [str(f.storage_path) for f in files if f.storage_path]


def _check_authority(authority: str, org_id: int | None) -> None:
    """An organization hold names its organization and a platform hold never does.

    A caller bug, not a request error: the routes fix the authority, so this never reaches a
    response (the database's ``ck_content_hold_authority_org`` would refuse it too).
    """
    if authority not in HOLD_AUTHORITIES or (authority == AUTHORITY_ORGANIZATION) != (
        org_id is not None
    ):
        raise ValueError(f"hold authority {authority!r} does not match the organization given")


def place_file_hold(
    db: Session,
    *,
    org_id: int | None,
    actor: User,
    files: list[MediaFile],
    reason: str,
    matter_ref: str | None = None,
    authority: str = AUTHORITY_ORGANIZATION,
) -> HoldChange:
    """Hold ``files``: all of ``org_id``'s for an organization hold, any tenant's for a platform one."""
    _check_authority(authority, org_id)
    text = clean_reason(reason)
    unique = {int(f.id): f for f in files}
    if not unique:
        raise HoldRequestError("no_files", "Name at least one file to hold.")
    if len(unique) > MAX_FILES_PER_HOLD:
        raise HoldRequestError(
            "too_many_files", f"A hold may name at most {MAX_FILES_PER_HOLD} files."
        )
    if org_id is not None and any(f.organization_id != org_id for f in unique.values()):
        # The caller resolves files tenant-scoped, so this is a backstop, not a path.
        raise HoldRequestError("not_org_file", "Only your organization's files can be held.")
    hold = ContentHold(
        authority=authority,
        organization_id=org_id,
        scope=SCOPE_FILE,
        reason=text,
        matter_ref=(matter_ref or "").strip() or None,
        placed_by_user_id=int(actor.id),
    )
    db.add(hold)
    db.flush()
    db.add_all(ContentHoldFile(hold_id=hold.id, media_file_id=fid) for fid in unique)
    db.flush()
    return HoldChange(
        hold=hold,
        event=AuditEventType.CONTENT_HOLD_PLACE,
        file_count=len(unique),
        lock_paths=_paths(unique.values()),
        file_uuids=[str(f.uuid) for f in unique.values()],
    )


def place_collection_hold(
    db: Session,
    *,
    org_id: int | None,
    actor: User,
    collection: Collection,
    reason: str,
    matter_ref: str | None = None,
    authority: str = AUTHORITY_ORGANIZATION,
) -> HoldChange:
    """Hold a collection: its files now, and every file added later.

    An organization hold takes only that organization's collections; a platform hold any.
    """
    _check_authority(authority, org_id)
    text = clean_reason(reason)
    if org_id is not None and collection.organization_id != org_id:
        raise HoldRequestError("not_org_collection", "Only your organization's collections.")
    members = list(
        db.execute(
            select(MediaFile)
            .join(CollectionMember, CollectionMember.media_file_id == MediaFile.id)
            .where(CollectionMember.collection_id == collection.id)
        ).scalars()
    )
    hold = ContentHold(
        authority=authority,
        organization_id=org_id,
        scope=SCOPE_COLLECTION,
        collection_id=int(collection.id),
        reason=text,
        matter_ref=(matter_ref or "").strip() or None,
        placed_by_user_id=int(actor.id),
    )
    db.add(hold)
    db.flush()
    db.add_all(ContentHoldFile(hold_id=hold.id, media_file_id=int(f.id)) for f in members)
    db.flush()
    return HoldChange(
        hold=hold,
        event=AuditEventType.CONTENT_HOLD_PLACE,
        file_count=len(members),
        lock_paths=_paths(members),
        file_uuids=[str(f.uuid) for f in members],
    )


def release_hold(
    db: Session,
    *,
    org_id: int | None,
    actor: User,
    hold: ContentHold,
    reason: str,
    authority: str = AUTHORITY_ORGANIZATION,
) -> HoldChange:
    """Release one hold. A file another hold (or the platform) still covers stays held.

    Only the hold's own authority releases it: an organization never releases a platform hold,
    and a platform administrator never releases an organization's.
    """
    _check_authority(authority, org_id)
    if hold.authority != authority or hold.organization_id != org_id:
        raise HoldRequestError("not_found", "Hold not found.")
    if hold.released_at is not None:
        raise HoldRequestError("already_released", "This hold was already released.")
    release_reason = clean_reason(reason, what="release reason")  # before any write
    hold.released_at = datetime.now(UTC)
    hold.released_by_user_id = int(actor.id)
    hold.release_reason = release_reason
    db.flush()

    covered = [
        int(i)
        for i in db.execute(
            select(ContentHoldFile.media_file_id).where(ContentHoldFile.hold_id == hold.id)
        ).scalars()
    ]
    still_held = hold_states(db, covered)
    freed = (
        list(
            db.execute(
                select(MediaFile).where(
                    MediaFile.id.in_([i for i in covered if i not in still_held])
                )
            ).scalars()
        )
        if covered
        else []
    )
    return HoldChange(
        hold=hold,
        event=AuditEventType.CONTENT_HOLD_RELEASE,
        file_count=len(covered),
        unlock_paths=_paths(freed),
        unheld_files=freed,
        file_uuids=[
            str(u)
            for u in db.execute(select(MediaFile.uuid).where(MediaFile.id.in_(covered))).scalars()
        ]
        if covered
        else [],
    )


def record_hold_change(
    db: Session, request: Request | None, actor: User, change: HoldChange
) -> None:
    """Stage the hold's audit event in the caller's transaction. Call it BEFORE the commit.

    Raises like any write of that transaction: a hold whose record cannot be kept is not placed.
    """
    details: dict[str, Any] = {
        "authority": change.hold.authority,
        "scope": change.hold.scope,
        "file_count": change.file_count,
    }
    # Name the files: the hold's file links cascade away with a deleted file, so once a held file
    # is gone this event is the only record of what the hold covered. Ids only, never a name.
    if change.file_uuids:
        details["file_uuids"] = sorted(change.file_uuids)[:MAX_AUDITED_FILE_UUIDS]
    if change.hold.collection_id is not None and change.hold.collection is not None:
        details["collection_uuid"] = str(change.hold.collection.uuid)
    stage_content_event(
        db,
        request,
        event=change.event,
        actor=actor,
        resource=AuditedResource.of("content_hold", change.hold),
        details=details,
    )


def finish_hold_change(
    db: Session, request: Request | None, actor: User, change: HoldChange
) -> None:
    """After the commit: storage locks, the erasure-sweep nudge and delivery of the audit event.

    Never raises. The object lock is advisory (the database is the source of truth); a lifted
    hold nudges the erasure sweep for every file that is no longer held, the same hook the
    platform's release uses (``takedown_service.release_file``). The event itself was committed
    with the hold (:func:`record_hold_change`); a sink outage only delays it.
    """
    from app.services.minio_service import set_object_legal_hold

    for path in change.lock_paths:
        set_object_legal_hold(path, True)
    for path in change.unlock_paths:
        set_object_legal_hold(path, False)
    if change.unheld_files:
        from app.tasks.erasure_reconciliation import notify_hold_released

        for freed in change.unheld_files:
            notify_hold_released(freed)
    audit_outbox.deliver_staged(db)


def record_hold_annotation(
    db: Session,
    request: Request | None,
    actor: User,
    file: MediaFile,
    comment_uuid: str,
    *,
    bypass: Any = None,
) -> bool:
    """Stage ``content.hold.annotation`` for a new comment on a held file; nothing when free.

    A hold freezes the file's content, but a new comment is an annotation beside it, not a
    change to it, so it is allowed and put on the record. Call it BEFORE the comment commits:
    the event is staged in that transaction (the outbox), so the comment and its record commit
    or roll back together. Raises like any write of that transaction.

    Returns:
        Whether an event was staged.
    """
    state = hold_states(db, [int(file.id)]).get(int(file.id))
    if state is None or not state.held:
        return False
    return stage_content_event(
        db,
        request,
        event=AuditEventType.CONTENT_HOLD_ANNOTATION,
        actor=actor,
        resource=AuditedResource.file(file),
        details={"comment_uuid": comment_uuid, "held_by": state.held_by},
        bypass=bypass,
    )


def record_hold_refusal(
    db: Session,
    request: Request | None,
    actor: User,
    resource: AuditedResource,
    *,
    action: str,
    held_by: list[str],
) -> None:
    """Audit that ``actor`` tried ``action`` on a held file or collection and was refused.

    ``details`` names the action and who holds it; never the hold's reason. Never raises.
    """
    record_content_event(
        db,
        request,
        event=AuditEventType.CONTENT_HOLD_REFUSED,
        actor=actor,
        resource=resource,
        details={"action": action, "held_by": held_by},
        dedup=False,
    )


def record_file_hold_refusals(
    db: Session,
    request: Request | None,
    actor: User,
    states: dict[int, HoldState],
    *,
    action: str,
) -> None:
    """:func:`record_hold_refusal` for each held file in ``states``.

    The write of each event is best-effort (:func:`record_hold_refusal`). The files are read
    without autoflush: a refusal can come after a service staged changes it then abandoned, and
    those must not be flushed by the audit's own read.
    """
    with db.no_autoflush:
        rows = db.query(MediaFile).filter(MediaFile.id.in_(sorted(states))).all()
    for row in rows:
        record_hold_refusal(
            db,
            request,
            actor,
            AuditedResource.file(row),
            action=action,
            held_by=states[int(row.id)].held_by,
        )
