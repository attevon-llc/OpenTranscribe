"""Attach or detach one tag across a gallery selection (the bulk-action rail).

The rail resolves the tag once per batch (:func:`resolve_bulk_tag`), applies it
file by file (:func:`apply_tag_to_file`), and refreshes caches and the search
index once for the files that changed (:func:`refresh_tagged_files`). Each file
gets its own outcome: a refusal or a database error on one file never fails the
batch, and earlier files keep their change.

A tag the batch itself inserted is committed up front, so a later per-file
rollback cannot take it away, and its id is noted on the session. The closing
:func:`refresh_tagged_files` deletes it again when no file ended up carrying
it, so a batch where every file failed leaves no unused tag behind.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from sqlalchemy import exists
from sqlalchemy.orm import Session

from app.core.constants import TAG_SOURCE_MANUAL
from app.core.exceptions import AccessDeniedError
from app.models.media import MediaFile
from app.models.sharing import TagShare
from app.models.tag import FileTag
from app.models.tag import Tag
from app.services.permission_service import PermissionService
from app.services.platform_bypass import PlatformBypass
from app.services.tag_files import add_file_tag
from app.services.tag_files import remove_file_tag
from app.services.tag_scope import is_system_tag
from app.services.tag_service import InvalidTagNameError
from app.services.tag_service import clean_tag_name
from app.services.tag_service import insert_tag_or_reread
from app.services.tag_service import lookup_existing_tag
from app.services.tag_service import normalize_tag_name
from app.services.tag_service import on_tags_changed

logger = logging.getLogger(__name__)

ADD_TAG_ACTION: Final = "add_tag"
REMOVE_TAG_ACTION: Final = "remove_tag"
TAG_ACTIONS: Final = frozenset({ADD_TAG_ACTION, REMOVE_TAG_ACTION})

#: The permission a caller needs on a file to change its tags.
REQUIRED_PERMISSION: Final = "editor"

PERMISSION_ERROR: Final = "PERMISSION_DENIED"
MUTATION_ERROR: Final = "TAG_UPDATE_FAILED"
TENANT_MISMATCH_ERROR: Final = "TAG_TENANT_MISMATCH"
TENANT_MISMATCH_MESSAGE: Final = "This tag belongs to a different organization than the file"

#: ``Session.info`` key holding the id of the tag the current batch created.
_CREATED_TAG_KEY: Final = "tag_bulk.created_tag_id"


class BulkTagOutcome(StrEnum):
    """What happened to one file of the selection."""

    ADDED = "added"
    ALREADY_PRESENT = "already_present"
    REMOVED = "removed"
    NOT_PRESENT = "not_present"
    FAILED = "failed"


#: Outcomes after which the file's tag set differs, so it needs a reindex.
CHANGED_OUTCOMES: Final = frozenset({BulkTagOutcome.ADDED, BulkTagOutcome.REMOVED})


@dataclass(frozen=True)
class BulkTagResult:
    """The per-file result the rail maps onto its response row."""

    outcome: BulkTagOutcome
    message: str
    error: str | None = None

    @property
    def success(self) -> bool:
        """Every outcome but a failure counts as success (a no-op included)."""
        return self.outcome is not BulkTagOutcome.FAILED


def resolve_bulk_tag(
    db: Session,
    action: str,
    tag_name: str | None,
    *,
    user_id: int,
    organization_id: int | None,
) -> Tag | None:
    """Resolve the batch's tag once, in the request tenant.

    Removing only looks the name up, so a typo leaves no new row and may yield
    None. Adding resolves or creates, then commits so the row survives a later
    per-file rollback; a row this request inserted is noted for
    :func:`refresh_tagged_files`.

    Raises:
        InvalidTagNameError: The name is missing or empty once normalized.
    """
    cleaned = clean_tag_name(tag_name or "")
    normalized = normalize_tag_name(cleaned)
    if not normalized:
        raise InvalidTagNameError("Tag name is required")
    existing = lookup_existing_tag(db, normalized, cleaned, user_id, organization_id)
    if action == REMOVE_TAG_ACTION:
        return existing
    inserted = False
    tag = existing
    if tag is None:
        tag, inserted = insert_tag_or_reread(
            db,
            cleaned,
            normalized,
            user_id=user_id,
            organization_id=organization_id,
            source=TAG_SOURCE_MANUAL,
        )
    db.commit()
    # Only a row this request inserted; a race winner belongs to the request that won.
    if inserted:
        db.info[_CREATED_TAG_KEY] = tag.id
    return tag


def _require_editor(
    db: Session, file_id: int, user_id: int, bypass: PlatformBypass
) -> BulkTagResult | None:
    """None when the caller may edit the file's tags, else the refusal row."""
    if bypass.user_is_admin:
        media_file = db.get(MediaFile, file_id)
        if media_file is not None and bypass.allows(
            org_id=media_file.organization_id,
            owner_id=media_file.user_id,
            need="write",
            resource_type="media_file",
            resource_uuid=str(media_file.uuid),
        ):
            return None
    try:
        PermissionService.check_file_access(
            db, file_id, user_id, min_permission=REQUIRED_PERMISSION
        )
    except AccessDeniedError as exc:
        return BulkTagResult(BulkTagOutcome.FAILED, message=str(exc.detail), error=PERMISSION_ERROR)
    return None


def _crosses_tenant(db: Session, tag: Tag, file_id: int) -> bool:
    """Whether attaching ``tag`` would put another tenant's tag on the file."""
    if is_system_tag(tag):
        return False
    file_org: int | None = (
        db.query(MediaFile.organization_id).filter(MediaFile.id == file_id).scalar()
    )
    return tag.organization_id != file_org


def _change_association(db: Session, file_id: int, tag: Tag, add: bool) -> BulkTagResult:
    if add:
        if add_file_tag(db, file_id, tag.id):
            return BulkTagResult(BulkTagOutcome.ADDED, f"Tag '{tag.name}' added")
        return BulkTagResult(
            BulkTagOutcome.ALREADY_PRESENT, f"File already carried tag '{tag.name}'"
        )
    if remove_file_tag(db, file_id, tag.id):
        return BulkTagResult(BulkTagOutcome.REMOVED, f"Tag '{tag.name}' removed")
    return BulkTagResult(BulkTagOutcome.NOT_PRESENT, f"File did not carry tag '{tag.name}'")


def _failed(exception_class: str) -> BulkTagResult:
    # The class name only: exception text can carry SQL, paths or connection strings.
    return BulkTagResult(
        BulkTagOutcome.FAILED,
        message=f"Tag change failed ({exception_class})",
        error=MUTATION_ERROR,
    )


def apply_tag_to_file(
    db: Session,
    *,
    file_id: int,
    tag: Tag | None,
    add: bool,
    user_id: int,
    bypass: PlatformBypass = PlatformBypass.none(),
) -> BulkTagResult:
    """Attach or detach ``tag`` on one file and commit that file's change.

    Args:
        db: Session; it stays usable for the next file whatever happens here.
        file_id: The file.
        tag: The batch's tag; None for a removal of a name that does not exist.
        add: Attach when True, detach when False.
        user_id: The caller, who needs editor rights on the file.
        bypass: The request's platform bypass.

    Returns:
        The file's outcome. Adding another organization's tag is refused;
        detaching is never refused on tenant grounds, so a stray association
        can still be removed.
    """
    refusal = _require_editor(db, file_id, user_id, bypass)
    if refusal is not None:
        return refusal
    if tag is None:
        return BulkTagResult(BulkTagOutcome.NOT_PRESENT, "No such tag; nothing to remove")
    if add and _crosses_tenant(db, tag, file_id):
        return BulkTagResult(
            BulkTagOutcome.FAILED, message=TENANT_MISMATCH_MESSAGE, error=TENANT_MISMATCH_ERROR
        )

    tag_name = tag.name
    savepoint = db.begin_nested()
    try:
        result = _change_association(db, file_id, tag, add)
        db.flush()
        savepoint.commit()
    except Exception as exc:
        # Any failure, a poisoned transaction included: undo only this file.
        savepoint.rollback()
        logger.exception(f"Bulk tag change failed on file {file_id} for tag '{tag_name}'")
        return _failed(type(exc).__name__)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.exception(f"Commit failed after a bulk tag change on file {file_id}")
        return _failed(type(exc).__name__)
    return result


def _discard_unattached_created_tag(db: Session) -> None:
    """Delete the tag this batch created if no file carries it and nobody shares it."""
    tag_id = db.info.pop(_CREATED_TAG_KEY, None)
    if tag_id is None:
        return
    unattached = (
        db.query(Tag)
        .filter(
            Tag.id == tag_id,
            ~exists().where(FileTag.tag_id == Tag.id),
            ~exists().where(TagShare.tag_id == Tag.id),
        )
        .delete(synchronize_session="fetch")
    )
    db.commit()
    if unattached:
        logger.info(f"Bulk tag batch attached tag {tag_id} to no file; removed it")


def refresh_tagged_files(db: Session, changed_file_uuids: list[str], *, user_id: int) -> list[int]:
    """Close the batch: drop an unused created tag, then run the change hook once.

    Called even with no changed files: a tag created for the batch still needs
    the caller's cached tag list dropped. Unknown uuids are ignored.

    Returns:
        The internal ids handed to the reindex.
    """
    _discard_unattached_created_tag(db)
    ids: list[int] = []
    if changed_file_uuids:
        rows = db.query(MediaFile.uuid, MediaFile.id).filter(MediaFile.uuid.in_(changed_file_uuids))
        by_uuid = {str(file_uuid): int(file_id) for file_uuid, file_id in rows}
        ids = [by_uuid[key] for key in map(str, changed_file_uuids) if key in by_uuid]
    return on_tags_changed(db, ids, user_id=user_id)
