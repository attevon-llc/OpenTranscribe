"""The ONE availability rule: may this file appear on a normal read surface?

A file is **unavailable** when it is

* **trashed** (``deleted_at`` set, issue #1101): unavailable to EVERYONE on every normal read
  surface, including a platform bypass. Only the trash routes, which resolve a file with their
  own lookup, reach a trashed file; or
* **quarantined** (taken down, issue #1253) and the platform role did not reach this row
  (``takedown_predicates.quarantine_exempt``: single-tenant admin, or a support grant covering
  the row's tenant in multi-tenant mode). Visibility is platform-only, including for the
  organization admin who placed an organization takedown.

Every read path asks this module, never the flags: ``tests/unit/test_file_availability_single_source.py``
fails on a read of ``MediaFile.is_quarantined`` / ``MediaFile.deleted_at`` anywhere else in
``app/`` that is not allowlisted with a reason (the takedown actions, the review surfaces, and
the destroy/erasure paths, which must see unavailable rows). Erasure and account deletion must
INCLUDE trashed files, so ``tests/unit/test_erasure_ignores_availability.py`` forbids them to
import this module.

  * :func:`exclude_unavailable` / :func:`available_pred` - the SQL form for lists, counts and
    facets (``include_quarantined`` = the list exemption, :func:`unavailable_list_exempt`).
  * :func:`is_unavailable_for` / :func:`is_unavailable` - the per-resource form the lookup
    choke points (``uuid_helpers.get_file_by_uuid_with_permission``) turn into a 404.
  * :func:`unavailable_among` - which of a batch of uuids are unavailable, for the post-filters
    over index-backed results (search hits, chat evidence, aggregation, persisted citations).
  * :func:`index_availability` / :func:`index_availability_by_id` / :func:`trashed_file_ids` -
    the value every OpenSearch writer stamps into a document's ``availability`` field
    (``search/availability_field``, issue #1101), so the index can exclude the trash itself.
    Index-side the field tracks the trash only: quarantine stays on its Postgres-resolved list.
  * :func:`is_notification_suppressed` / :func:`is_notification_suppressed_for_uuid` /
    :func:`filter_suppressed_file_uuids` - the WebSocket-push twin: an unavailable file's
    identity must not reach a recipient over a live event any more than through a read surface
    (``utils/websocket_notify.send_ws_event_for_file`` is the required call site, issue #908).
    A trashed file's events are dropped for everyone: its active task was cancelled at trash time.
"""

from __future__ import annotations

import logging
import uuid as uuid_pkg
from collections.abc import Iterable
from collections.abc import Sequence
from typing import TYPE_CHECKING
from typing import Any

from sqlalchemy import and_
from sqlalchemy import or_
from sqlalchemy.orm import Query
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.models.media import MediaFile
from app.services.search.availability_field import AVAILABLE
from app.services.search.availability_field import TRASHED
from app.services.takedown_predicates import is_review_admin
from app.services.takedown_predicates import quarantine_exempt
from app.utils.uuid_parse import parse_uuid_or_none

if TYPE_CHECKING:
    from app.services.platform_bypass import PlatformBypass

logger = logging.getLogger(__name__)


# --- SQL form ---------------------------------------------------------------------------------


def available_pred(*, include_quarantined: bool = False) -> ColumnElement[bool]:
    """The SQL condition "this ``MediaFile`` row may appear on a normal read surface".

    Always excludes trashed rows. Excludes quarantined rows unless ``include_quarantined``
    (the list exemption, :func:`unavailable_list_exempt`). ``is_(False)`` rather than
    ``!= True`` keeps the partial index usable; the column is NOT NULL with a false default.
    """
    live = MediaFile.deleted_at.is_(None)
    if include_quarantined:
        return live
    return and_(live, MediaFile.is_quarantined.is_(False))


def unavailable_pred(*, include_quarantined: bool = False) -> ColumnElement[bool]:
    """The complement of :func:`available_pred`, for building exclusion sets."""
    trashed = MediaFile.deleted_at.is_not(None)
    if include_quarantined:
        return trashed
    return or_(trashed, MediaFile.is_quarantined.is_(True))


# Generic so a column-projecting RowReturningQuery keeps its row type through the filter.
def exclude_unavailable[QueryT: Query[Any]](
    query: QueryT, *, include_quarantined: bool = False
) -> QueryT:
    """Drop unavailable files from a query selecting from / filtering ``MediaFile``.

    Args:
        query: A query over ``MediaFile``.
        include_quarantined: The list exemption (:func:`unavailable_list_exempt`); never
            derived from the bare admin role. Trashed rows are dropped regardless.
    """
    return query.filter(available_pred(include_quarantined=include_quarantined))


def unavailable_list_exempt(bypass: PlatformBypass) -> bool:
    """List/query form of the quarantine exemption: ``include_quarantined`` for the SQL form.

    A list has no single row to ask ``allows`` about, so it uses the bypass's own list reach
    (``sees_all_in_scope``): every admin in single mode, only an organization grant in
    multi-tenant mode. It never exempts the trash.
    """
    return bypass.sees_all_in_scope


# --- per-resource form ------------------------------------------------------------------------


def is_unavailable(file: MediaFile, *, platform_reached: bool = False) -> bool:
    """Whether ``file`` must be treated as non-existent.

    Args:
        file: The loaded media file.
        platform_reached: True only where the platform role already reached this row
            (``bypass.allows`` on the row's tenant). It exempts a quarantine, never the trash.
    """
    if getattr(file, "deleted_at", None) is not None:
        return True
    return bool(getattr(file, "is_quarantined", False)) and not platform_reached


def is_unavailable_for(bypass: PlatformBypass, file: MediaFile) -> bool:
    """:func:`is_unavailable` with the exemption decided by this request's bypass."""
    if getattr(file, "deleted_at", None) is not None:
        return True
    if not bool(getattr(file, "is_quarantined", False)):
        return False
    return not quarantine_exempt(bypass, file)


def unavailable_among(
    db: Session,
    file_uuids: Iterable[str | uuid_pkg.UUID | None],
    *,
    include_quarantined: bool = False,
) -> set[str]:
    """The subset of ``file_uuids`` whose file is unavailable (trashed or quarantined), as strings.

    One query over exactly the given ids; an empty input, or one with only falsy ids, returns
    ``set()`` without querying. A query error propagates: callers drop hits on the strength of
    this answer, so an exception must fail the read rather than pass every hit through.

    Args:
        db: Database session.
        file_uuids: Candidate file uuids, as the caller's hits carry them.
        include_quarantined: The list exemption: then only trashed files count as unavailable.
    """
    ids = {file_uuid for file_uuid in file_uuids if file_uuid}
    if not ids:
        return set()
    rows = (
        db.query(MediaFile.uuid)
        .filter(MediaFile.uuid.in_(ids), unavailable_pred(include_quarantined=include_quarantined))
        .all()
    )
    return {str(row[0]) for row in rows}


# --- index form -------------------------------------------------------------------------------


def index_availability(file: MediaFile) -> str:
    """The ``availability`` value a document of ``file`` must carry: trashed, or available."""
    return TRASHED if getattr(file, "deleted_at", None) is not None else AVAILABLE


def index_availability_by_id(db: Session, file_ids: Iterable[int]) -> dict[int, str]:
    """:func:`index_availability` for a batch of file ids, in ONE query.

    An id with no row is absent from the result (a purged file has no documents to stamp). A
    query error propagates: a writer must not guess a value it could not read.
    """
    ids = sorted({int(i) for i in file_ids if i is not None})
    if not ids:
        return {}
    rows = db.query(MediaFile.id, MediaFile.deleted_at).filter(MediaFile.id.in_(ids)).all()
    return {int(row[0]): TRASHED if row[1] is not None else AVAILABLE for row in rows}


def trashed_file_ids(db: Session) -> list[int]:
    """Every trashed file's id, ascending: the set whose documents must read ``trashed``."""
    rows = (
        db.query(MediaFile.id)
        .filter(unavailable_pred(include_quarantined=True))
        .order_by(MediaFile.id)
        .all()
    )
    return [int(row[0]) for row in rows]


# --- WebSocket notification form --------------------------------------------------------------


def _suppressed(deleted_at: Any, quarantined: bool, *, recipient_is_admin: bool) -> bool:
    if deleted_at is not None:
        return True
    return quarantined and not recipient_is_admin


def is_notification_suppressed(file_id: int, recipient_user_id: int) -> bool:
    """Whether a file-scoped task notification to ``recipient_user_id`` must be dropped.

    Consulted by ``notification_service.send_task_notification`` before it attaches file
    metadata (filename/content_type/file_size) to a WebSocket event: an unavailable file's
    identity must not reach a recipient through the notification funnel any more than through
    the read surfaces. A quarantined file's events still reach a review admin; a trashed file's
    reach no one.

    Deliberately does NOT cover the §512(g) takedown/release owner notices: those pass the file's
    identity in ``extra``, never as ``file_id``, so the owner always learns of their takedown.

    Opens its OWN short session (it runs deep inside a Celery task's notification call) and
    **fails CLOSED**: a DB error suppresses the notification. A missed event is one live-update
    frame the SPA reconciles on its next poll; a leaked one is a filename disclosure. Logged at
    WARNING so a persistent DB problem stays visible.
    """
    try:
        from app.db.session_utils import session_scope

        with session_scope() as db:
            row = (
                db.query(MediaFile.deleted_at, MediaFile.is_quarantined)
                .filter(MediaFile.id == file_id)
                .first()
            )
            if row is None:
                return False
            if row[0] is None and not row[1]:
                return False
            return _suppressed(
                row[0], bool(row[1]), recipient_is_admin=is_review_admin(db, recipient_user_id)
            )
    except Exception as e:  # noqa: BLE001 — fail CLOSED, see docstring
        logger.warning(
            f"Notification suppression check failed for file {file_id}; "
            f"suppressing as a precaution: {e}"
        )
        return True


def is_notification_suppressed_for_uuid(
    file_uuid: str | uuid_pkg.UUID, recipient_user_id: int
) -> bool:
    """UUID-keyed twin of :func:`is_notification_suppressed` (most tasks hold only a uuid).

    Fails CLOSED like the original, and further: a uuid that does not parse or resolves to no
    row is suppressed, because a uuid has no meaning by construction and a miss is at least as
    likely an upstream typo or race as a genuinely deleted row.
    """
    try:
        normalized = parse_uuid_or_none(file_uuid)
        if normalized is None:
            return True

        from app.db.session_utils import session_scope

        with session_scope() as db:
            row = (
                db.query(MediaFile.deleted_at, MediaFile.is_quarantined)
                .filter(MediaFile.uuid == normalized)
                .first()
            )
            if row is None:
                return True
            if row[0] is None and not row[1]:
                return False
            return _suppressed(
                row[0], bool(row[1]), recipient_is_admin=is_review_admin(db, recipient_user_id)
            )
    except Exception as e:  # noqa: BLE001 — fail CLOSED, see docstring
        logger.warning(
            f"Notification suppression check failed for file {file_uuid}; "
            f"suppressing as a precaution: {e}"
        )
        return True


def filter_suppressed_file_uuids(
    file_uuids: Sequence[str | uuid_pkg.UUID], recipient_user_id: int
) -> list[str]:
    """The disclosable subset of ``file_uuids`` for ``recipient_user_id``, in input order.

    The batch counterpart for a multi-file WebSocket event: ONE query for the whole list.
    Fails CLOSED (any exception returns ``[]``); a malformed or unresolvable uuid is dropped,
    because it cannot be proven available.
    """
    candidates = list(file_uuids)
    if not candidates:
        return []

    try:
        parsed: dict[str, uuid_pkg.UUID] = {}
        for raw in candidates:
            key = str(raw)
            if key in parsed:
                continue
            normalized = parse_uuid_or_none(raw)
            if normalized is not None:
                parsed[key] = normalized

        if not parsed:
            return []

        from app.db.session_utils import session_scope

        with session_scope() as db:
            rows = (
                db.query(MediaFile.uuid, MediaFile.deleted_at, MediaFile.is_quarantined)
                .filter(MediaFile.uuid.in_(parsed.values()))
                .all()
            )
            state_by_key = {str(row[0]): (row[1], bool(row[2])) for row in rows}
            recipient_is_admin = is_review_admin(db, recipient_user_id)

        visible: list[str] = []
        for raw in candidates:
            key = str(raw)
            if key not in parsed:
                continue
            state = state_by_key.get(key)
            if state is None:
                continue
            if _suppressed(*state, recipient_is_admin=recipient_is_admin):
                continue
            visible.append(key)
        return visible
    except Exception as e:  # noqa: BLE001 — fail CLOSED, see docstring
        logger.warning(
            f"Batch notification suppression check failed for {len(candidates)} file(s); "
            f"suppressing all as a precaution: {e}"
        )
        return []
