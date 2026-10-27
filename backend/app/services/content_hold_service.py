"""Place, release and maintain organization content holds (issue #1276).

A hold is a row in ``content_hold`` plus the files it covers in ``content_hold_file``. WHETHER a
file is held is answered by ``hold_guard`` and nowhere else; this module only changes the facts
that answer reads.

**Writes never commit.** Every function here adds rows and flushes; the endpoint commits once.
What must happen only AFTER the commit (the storage object lock, the "a hold was lifted" nudge to
the erasure sweep, and the audit event, which would otherwise describe a change that then rolled
back) is returned in a :class:`HoldChange` and performed by :func:`finish_hold_change`.

A hold's ``reason`` and ``matter_ref`` are administrator text. They are stored, shown to the
organization's administrators, and nothing else: not in an audit event, not over a socket, not in
any file or collection response.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.audit import AuditEventType
from app.models.content_hold import REASON_MAX_LENGTH
from app.models.content_hold import REASON_MIN_LENGTH
from app.models.content_hold import SCOPE_COLLECTION
from app.models.content_hold import SCOPE_FILE
from app.models.content_hold import ContentHold
from app.models.content_hold import ContentHoldFile
from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import MediaFile
from app.services.content_audit import AuditedResource
from app.services.content_audit import record_content_event
from app.services.hold_guard import hold_states

if TYPE_CHECKING:
    from fastapi import Request

    from app.models.user import User

logger = logging.getLogger(__name__)

#: Most files one file-scoped hold may name in a single request.
MAX_FILES_PER_HOLD = 500


class HoldRequestError(Exception):
    """A hold request that cannot be honoured. ``code`` is stable for the endpoint to map."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class HoldChange:
    """What a hold write changed, for :func:`finish_hold_change` to do after the commit."""

    hold: ContentHold
    event: AuditEventType
    file_count: int = 0
    lock_paths: list[str] = field(default_factory=list)
    unlock_paths: list[str] = field(default_factory=list)
    unheld_files: list[MediaFile] = field(default_factory=list)


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


def place_file_hold(
    db: Session,
    *,
    org_id: int,
    actor: User,
    files: list[MediaFile],
    reason: str,
    matter_ref: str | None = None,
) -> HoldChange:
    """Hold ``files``, all of which must belong to ``org_id``."""
    text = clean_reason(reason)
    unique = {int(f.id): f for f in files}
    if not unique:
        raise HoldRequestError("no_files", "Name at least one file to hold.")
    if len(unique) > MAX_FILES_PER_HOLD:
        raise HoldRequestError(
            "too_many_files", f"A hold may name at most {MAX_FILES_PER_HOLD} files."
        )
    if any(f.organization_id != org_id for f in unique.values()):
        # The caller resolves files tenant-scoped, so this is a backstop, not a path.
        raise HoldRequestError("not_org_file", "Only your organization's files can be held.")
    hold = ContentHold(
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
    )


def place_collection_hold(
    db: Session,
    *,
    org_id: int,
    actor: User,
    collection: Collection,
    reason: str,
    matter_ref: str | None = None,
) -> HoldChange:
    """Hold an organization collection: its files now, and every file added later."""
    text = clean_reason(reason)
    if collection.organization_id != org_id:
        raise HoldRequestError("not_org_collection", "Only your organization's collections.")
    members = list(
        db.execute(
            select(MediaFile)
            .join(CollectionMember, CollectionMember.media_file_id == MediaFile.id)
            .where(CollectionMember.collection_id == collection.id)
        ).scalars()
    )
    hold = ContentHold(
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
    )


def release_hold(
    db: Session, *, org_id: int, actor: User, hold: ContentHold, reason: str
) -> HoldChange:
    """Release one hold. A file another hold (or the platform) still covers stays held."""
    if hold.organization_id != org_id:
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
    )


def finish_hold_change(
    db: Session, request: Request | None, actor: User, change: HoldChange
) -> None:
    """After the commit: storage locks, the erasure-sweep nudge and the audit event.

    Never raises. The object lock is advisory (the database is the source of truth); a lifted
    hold nudges the erasure sweep for every file that is no longer held, the same hook the
    platform's release uses (``takedown_service.release_file``).
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
    details: dict[str, Any] = {"scope": change.hold.scope, "file_count": change.file_count}
    if change.hold.collection_id is not None and change.hold.collection is not None:
        details["collection_uuid"] = str(change.hold.collection.uuid)
    record_content_event(
        db,
        request,
        event=change.event,
        actor=actor,
        resource=AuditedResource.of("content_hold", change.hold),
        details=details,
        dedup=False,
    )


def record_edit_on_held_file(
    db: Session,
    request: Request | None,
    actor: User,
    file: MediaFile,
    *,
    kind: str,
    segment_uuid: str | None = None,
    fields: Iterable[str] = (),
    speaker_uuids: Iterable[str] = (),
    bypass: Any = None,
) -> bool:
    """Audit a transcript edit made while ``file`` is under a legal hold (issue #1307).

    A hold blocks destruction, not correction, so the edit goes ahead; what the hold changes
    is that the edit is now on the record. Call it AFTER the edit committed, so the event never
    describes a change that rolled back. A file nobody holds writes nothing (the ordinary edit
    is not a governance event), and the event is not opt-in: held files are few and this is the
    trail the hold exists to keep. It is best-effort by construction, though: the edit has
    already committed when this runs, so a failed audit write cannot undo or refuse it. A
    failure is logged at ERROR with the file's uuid (never its content), so a missing event is
    visible to an operator instead of silent.

    Its own reads run in a SAVEPOINT: a database error here must not leave the caller's
    transaction aborted for the writes that follow the call.

    Args:
        kind: ``segment_edit`` (text or times), ``segment_speaker`` (reassignment) or
            ``speaker_merge`` (every segment of one speaker moved onto another).
        segment_uuid: The segment's uuid, for the single-segment kinds. An id, never its text.
        fields: Names of the columns changed (``text``, ``start_time``, ``end_time``). Never
            their values.
        speaker_uuids: The speakers a merge involved, source and target. Ids, never names.
        bypass: The request's platform bypass, so a support-access grant is recorded.

    Returns:
        Whether an event was written. Never raises.
    """
    try:
        with db.begin_nested():
            state = hold_states(db, [int(file.id)]).get(int(file.id))
            if state is None or not state.held:
                return False
            details: dict[str, Any] = {
                "kind": kind,
                "fields": sorted(fields),
                "held_by": state.held_by,
            }
            if segment_uuid is not None:
                details["segment_uuid"] = segment_uuid
            speakers = sorted(speaker_uuids)
            if speakers:
                details["speaker_uuids"] = speakers
            return record_content_event(
                db,
                request,
                event=AuditEventType.CONTENT_TRANSCRIPT_EDIT,
                actor=actor,
                resource=AuditedResource.file(file),
                details=details,
                dedup=False,
                bypass=bypass,
            )
    except Exception:  # noqa: BLE001 - the edit is already committed; it cannot be un-made here
        logger.error(
            "held-file edit audit could NOT be recorded for file %s (the edit was committed)",
            file.uuid,
            exc_info=True,
        )
        return False


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
