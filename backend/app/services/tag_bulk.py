"""Bulk tag attach/detach for a gallery selection — the per-file mutation half.

The rail that drives this lives in ``app/api/endpoints/files/management.py``
(``POST /api/files/management/bulk-action``); the bodies live here because that
module is already large and every hazard below is a *service* concern.

Three things a naive per-file loop gets wrong, and what this module does instead:

* **A no-op is not a failure.** Adding a tag to a selection where some files
  already carry it must end with every file carrying it and the rest reported as
  *unchanged* — the bulk envelope's ``success | message | error`` could not
  express that, so :class:`BulkTagOutcome` distinguishes a change from a no-op.
* **Read access is not write access.** The rail resolves files through
  ``get_media_file_by_uuid``, which admits *any* non-None permission — and
  ``viewer`` is one. A read-only member of a shared collection could otherwise
  retag every file in it. Every mutation here first demands ``editor`` via
  :meth:`PermissionService.check_file_access`, and reports the refusal as that
  file's outcome instead of 403-ing the whole batch.
* **One aborted statement must not poison the batch.** Every file in a request
  runs on the *same* ``Depends(get_db)`` session, and the rail's per-file
  ``except`` appends a result and continues **without a rollback**. After the
  first database-level error psycopg2 leaves the transaction aborted, so every
  later file dies with ``InFailedSqlTransaction: current transaction is
  aborted`` — a failure attributable to nothing in particular. Each mutation
  therefore runs inside its own SAVEPOINT (``db.begin_nested()``) and commits
  per file: the failing file rolls back to its savepoint, the session stays
  usable, and the files processed before it keep their change. The other bulk
  handlers get their durability from helpers that commit internally; these have
  to do it explicitly.

The search refresh is deliberately **one** task for the whole batch
(:func:`refresh_tagged_files`), not one per file — ``on_tags_changed`` enqueues a
partial update over a file-id list.
"""

from __future__ import annotations

import logging
import uuid as uuid_pkg
from dataclasses import dataclass
from enum import StrEnum

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.constants import TAG_SOURCE_MANUAL
from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.media import Tag
from app.services.permission_service import PermissionService
from app.services.tag_service import InvalidTagNameError
from app.services.tag_service import clean_tag_name
from app.services.tag_service import lookup_existing_tag
from app.services.tag_service import normalize_tag_name
from app.services.tag_service import on_tags_changed
from app.services.tag_service import resolve_or_create_tag

logger = logging.getLogger(__name__)

#: Bulk actions this module owns, as they appear in ``BulkActionRequest.action``.
ADD_TAG_ACTION = "add_tag"
REMOVE_TAG_ACTION = "remove_tag"
TAG_ACTIONS = frozenset({ADD_TAG_ACTION, REMOVE_TAG_ACTION})

#: Minimum permission a caller needs on a file to change its tags.
REQUIRED_PERMISSION = "editor"


class BulkTagOutcome(StrEnum):
    """What a bulk tag action did to one file.

    ``ALREADY_PRESENT`` / ``NOT_PRESENT`` are *successful* no-ops: the file ends
    in the requested state, it simply did not have to change to get there. Only
    ``FAILED`` means the file did not reach that state.
    """

    ADDED = "added"
    ALREADY_PRESENT = "already_present"
    REMOVED = "removed"
    NOT_PRESENT = "not_present"
    FAILED = "failed"


#: Outcomes that changed a file's tag set, and therefore its search document.
CHANGED_OUTCOMES = frozenset({BulkTagOutcome.ADDED, BulkTagOutcome.REMOVED})

PERMISSION_ERROR = "PERMISSION_DENIED"
MUTATION_ERROR = "TAG_UPDATE_FAILED"


@dataclass(frozen=True)
class BulkTagResult:
    """One file's outcome, before it is mapped onto the bulk response envelope."""

    outcome: BulkTagOutcome
    message: str
    error: str | None = None

    @property
    def success(self) -> bool:
        """True for every outcome except :attr:`BulkTagOutcome.FAILED`."""
        return self.outcome is not BulkTagOutcome.FAILED


def resolve_bulk_tag(db: Session, action: str, tag_name: str | None, *, user_id: int) -> Tag | None:
    """Resolve the batch's tag **once**, before the per-file loop.

    Add resolves through :func:`app.services.tag_service.resolve_or_create_tag`
    (normalized-exact, so ``Q3 Review`` and ``q3-review`` are one tag) and
    **commits**: the row has to survive a later per-file rollback, or every
    remaining file would insert an association pointing at a tag that no longer
    exists. Remove only *looks the tag up* — creating a tag in order to detach it
    would leave a new row behind on every typo.

    Args:
        db: Database session. Committed when a tag is created.
        action: ``add_tag`` or ``remove_tag``.
        tag_name: Supplied name, trimmed and clamped like every other tag path.
        user_id: The acting user. Owns a tag this creates, and scopes the
            lookup — resolving unscoped would let a bulk action attach or
            detach another account's identically-named row.

    Returns:
        The tag to apply, or None when a remove names a tag nothing carries —
        which is a per-file ``NOT_PRESENT``, not an error.

    Raises:
        InvalidTagNameError: The name is empty once normalized.
    """
    cleaned = clean_tag_name(tag_name or "")
    normalized = normalize_tag_name(cleaned)
    if not normalized:
        raise InvalidTagNameError(f"Tag name is empty after normalization: {tag_name!r}")

    if action == REMOVE_TAG_ACTION:
        return lookup_existing_tag(db, normalized, cleaned, user_id)

    tag = resolve_or_create_tag(db, cleaned, user_id=user_id, source=TAG_SOURCE_MANUAL)
    db.commit()
    return tag


def _require_editor(
    db: Session, file_id: int, user_id: int, is_admin: bool
) -> BulkTagResult | None:
    """Return a failure result unless the caller may *write* to this file."""
    if is_admin:
        return None
    try:
        PermissionService.check_file_access(
            db, file_id, user_id, min_permission=REQUIRED_PERMISSION
        )
    except HTTPException as exc:
        return BulkTagResult(BulkTagOutcome.FAILED, str(exc.detail), PERMISSION_ERROR)
    return None


def apply_tag_to_file(
    db: Session,
    *,
    file_id: int,
    tag: Tag | None,
    add: bool,
    user_id: int,
    is_admin: bool = False,
) -> BulkTagResult:
    """Attach or detach ``tag`` on one file, isolated from the rest of the batch.

    The mutation runs inside its own SAVEPOINT and commits on success, so a
    database error here rolls back to that savepoint and leaves the session
    usable for the next file — see the module docstring for why that is
    load-bearing rather than defensive.

    Args:
        db: Database session shared by the whole batch. Committed on a change.
        file_id: Internal id of the file, already resolved and tenant-gated by
            the rail.
        tag: The batch's tag, or None when a remove named an unknown tag.
        add: True to attach, False to detach.
        user_id: The acting user, checked for ``editor`` on this file.
        is_admin: Admins bypass the permission gate, as everywhere else.

    Returns:
        This file's outcome. Never raises for an expected condition — a refusal
        or a database error comes back as :attr:`BulkTagOutcome.FAILED`.
    """
    refusal = _require_editor(db, file_id, user_id, is_admin)
    if refusal is not None:
        return refusal

    if tag is None:
        # Remove named a tag no row exists for: nothing carries it, by definition.
        return BulkTagResult(BulkTagOutcome.NOT_PRESENT, "No such tag; nothing to remove")

    savepoint = db.begin_nested()
    try:
        link = (
            db.query(FileTag)
            .filter(FileTag.media_file_id == file_id, FileTag.tag_id == tag.id)
            .first()
        )
        if add:
            if link is not None:
                savepoint.rollback()
                return BulkTagResult(
                    BulkTagOutcome.ALREADY_PRESENT, f"File already carried tag '{tag.name}'"
                )
            db.add(FileTag(media_file_id=file_id, tag_id=tag.id, source=TAG_SOURCE_MANUAL))
            outcome = BulkTagOutcome.ADDED
            message = f"Tag '{tag.name}' added"
        else:
            if link is None:
                savepoint.rollback()
                return BulkTagResult(
                    BulkTagOutcome.NOT_PRESENT, f"File did not carry tag '{tag.name}'"
                )
            db.delete(link)
            outcome = BulkTagOutcome.REMOVED
            message = f"Tag '{tag.name}' removed"
        db.flush()
    except Exception as exc:
        # ROLLBACK TO SAVEPOINT: undoes only this file and clears the aborted
        # transaction state, so the files after it are still processable.
        savepoint.rollback()
        logger.warning("Bulk tag change failed for file %s: %s", file_id, exc)
        return BulkTagResult(BulkTagOutcome.FAILED, f"Tag change failed: {exc}", MUTATION_ERROR)

    try:
        savepoint.commit()
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("Bulk tag commit failed for file %s: %s", file_id, exc)
        return BulkTagResult(BulkTagOutcome.FAILED, f"Tag change failed: {exc}", MUTATION_ERROR)

    return BulkTagResult(outcome, message)


def refresh_tagged_files(db: Session, changed_file_uuids: list[str], *, user_id: int) -> list[int]:
    """Bust tag caches and enqueue ONE search refresh for the whole batch.

    Called once after the loop, never per file: ``on_tags_changed`` fires a
    single partial-update task carrying every file id. Only files that actually
    changed are listed — a file that already carried the tag has a correct
    indexed tag array, so reindexing it is work with no effect. The call happens
    even when nothing changed, because the cache-bust half still has to run for
    a tag the batch created.

    Args:
        db: Database session, used to resolve the public uuids to internal ids.
        changed_file_uuids: Uuids of the files whose tag set changed.
        user_id: The acting user, whose cached listings are busted.

    Returns:
        The file ids a refresh was enqueued for.
    """
    file_ids: list[int] = []
    if changed_file_uuids:
        parsed = [uuid_pkg.UUID(str(value)) for value in changed_file_uuids]
        file_ids = [
            int(file_id)
            for (file_id,) in db.query(MediaFile.id).filter(MediaFile.uuid.in_(parsed)).all()
        ]
    return on_tags_changed(db, file_ids, user_id=user_id)
