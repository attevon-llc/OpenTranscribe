"""Per-user hide of media files (issue #1102): the one place ``user_hidden_media`` is read or written.

Hide is a VIEW preference of one user, not access control:

* it never changes what anyone else sees (every predicate here is keyed by the caller's id);
* it never blocks a direct link, the file detail, or a file a person explicitly picked (a chat
  file selection, a ``file_uuid``-scoped search): those surfaces simply do not ask this module;
* it is not folded into ``file_availability``: availability (trash, quarantine) is evaluated
  first and always wins. A hidden and trashed file is just unavailable, and its hide row is kept
  so a restored file is still hidden for whoever hid it.

Who may hide: anyone who can read the file. The routes resolve the file through the permission
choke point (``get_file_by_uuid_with_permission``, default ``viewer``) before calling
:func:`hide`, which is also what keeps a file of another tenant out of reach.

The per-user cap (``HIDDEN_MEDIA_PER_USER_CAP``) is enforced here at write time, under a per-user
transaction advisory lock so two concurrent hides cannot both take the last slots. It bounds
the OpenSearch ``terms`` exclusion :func:`hidden_uuids` feeds.
"""

from __future__ import annotations

from collections.abc import Collection as AbstractCollection
from collections.abc import Iterable
from typing import Any

from sqlalchemy import exists
from sqlalchemy import func
from sqlalchemy import not_
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Query
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from app.core.constants import HIDDEN_MEDIA_PER_USER_CAP
from app.core.exceptions import ConflictError
from app.models.hidden_media import UserHiddenMedia
from app.models.media import MediaFile

#: The ``hidden`` query parameter of every list: ``exclude`` (the default), ``include`` (hidden
#: files listed beside the rest), ``only`` (just the hidden ones, the way back to unhide one).
HIDDEN_MODE_PATTERN = "^(exclude|include|only)$"

HIDDEN_LIMIT_ERROR_CODE = "HIDDEN_LIMIT"

#: First key of the two-key advisory lock that serialises one user's hides (the issue number).
_LOCK_NAMESPACE = 1102


class HiddenLimitError(ConflictError):
    """409: hiding these files would take the caller past the per-user cap."""

    def __init__(self) -> None:
        super().__init__(
            {
                "error": HIDDEN_LIMIT_ERROR_CODE,
                "message": (
                    f"You can hide at most {HIDDEN_MEDIA_PER_USER_CAP} files. "
                    "Unhide some files first."
                ),
                "limit": HIDDEN_MEDIA_PER_USER_CAP,
            }
        )


# --- SQL form ---------------------------------------------------------------------------------


def is_hidden_pred(user_id: int) -> ColumnElement[bool]:
    """``EXISTS`` "this ``MediaFile`` row is hidden by ``user_id``" (an anti-join when negated)."""
    return exists().where(
        UserHiddenMedia.user_id == user_id, UserHiddenMedia.media_file_id == MediaFile.id
    )


def hidden_mode_pred(user_id: int, mode: str) -> ColumnElement[bool] | None:
    """The filter for ``mode``, or None when every file passes (``include``).

    Raises:
        ValueError: An unknown mode; the routes validate with :data:`HIDDEN_MODE_PATTERN`.
    """
    if mode == "exclude":
        return not_(is_hidden_pred(user_id))
    if mode == "only":
        return is_hidden_pred(user_id)
    if mode == "include":
        return None
    raise ValueError(f"unknown hidden mode {mode!r}")


def apply_hidden_mode[QueryT: Query[Any]](query: QueryT, user_id: int, mode: str) -> QueryT:
    """Filter a query over ``MediaFile`` by the caller's hide rows."""
    pred = hidden_mode_pred(user_id, mode)
    return query if pred is None else query.filter(pred)


def exclude_hidden_for[QueryT: Query[Any]](query: QueryT, user_id: int) -> QueryT:
    """Drop the files ``user_id`` has hidden (the default on every list)."""
    return apply_hidden_mode(query, user_id, "exclude")


# --- reads ------------------------------------------------------------------------------------


def hidden_uuids(db: Session, user_id: int) -> list[str]:
    """Every file uuid ``user_id`` has hidden, for an OpenSearch ``must_not terms`` clause.

    Bounded by ``HIDDEN_MEDIA_PER_USER_CAP``, which is what keeps the clause legal.
    """
    rows = (
        db.query(MediaFile.uuid)
        .join(UserHiddenMedia, UserHiddenMedia.media_file_id == MediaFile.id)
        .filter(UserHiddenMedia.user_id == user_id)
        .all()
    )
    return sorted(str(r[0]) for r in rows)


def hidden_file_ids_among(db: Session, user_id: int, file_ids: Iterable[int]) -> set[int]:
    """Which of ``file_ids`` ``user_id`` has hidden (one query; for marking a list page)."""
    ids = {int(i) for i in file_ids}
    if not ids:
        return set()
    rows = (
        db.query(UserHiddenMedia.media_file_id)
        .filter(UserHiddenMedia.user_id == user_id, UserHiddenMedia.media_file_id.in_(ids))
        .all()
    )
    return {int(r[0]) for r in rows}


# --- writes -----------------------------------------------------------------------------------


def hide(db: Session, user_id: int, file_ids: AbstractCollection[int]) -> int:
    """Hide ``file_ids`` for ``user_id``. Already-hidden files are a no-op.

    The caller has already resolved each file through the permission choke point, and commits.
    All or nothing against the cap: a request that would pass it hides none of its files.

    Returns:
        How many files became hidden.

    Raises:
        HiddenLimitError: The request would take ``user_id`` past ``HIDDEN_MEDIA_PER_USER_CAP``.
    """
    ids = {int(i) for i in file_ids}
    if not ids:
        return 0
    db.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :uid)"),
        {"ns": _LOCK_NAMESPACE, "uid": user_id},
    )
    already = hidden_file_ids_among(db, user_id, ids)
    new = ids - already
    if not new:
        return 0
    count = (
        db.query(func.count())
        .select_from(UserHiddenMedia)
        .filter(UserHiddenMedia.user_id == user_id)
        .scalar()
    ) or 0
    if count + len(new) > HIDDEN_MEDIA_PER_USER_CAP:
        raise HiddenLimitError()
    stmt = (
        pg_insert(UserHiddenMedia)
        .values([{"user_id": user_id, "media_file_id": fid} for fid in sorted(new)])
        .on_conflict_do_nothing(index_elements=["user_id", "media_file_id"])
    )
    db.execute(stmt)
    return len(new)


def unhide(db: Session, user_id: int, file_ids: AbstractCollection[int]) -> int:
    """Unhide ``file_ids`` for ``user_id``; the caller commits. Returns how many were hidden."""
    ids = {int(i) for i in file_ids}
    if not ids:
        return 0
    return int(
        db.query(UserHiddenMedia)
        .filter(UserHiddenMedia.user_id == user_id, UserHiddenMedia.media_file_id.in_(ids))
        .delete(synchronize_session=False)
    )
