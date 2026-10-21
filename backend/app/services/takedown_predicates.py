"""Read-side takedown predicates: who may see a quarantined file, and who may be told.

Split out of ``takedown_service`` (issue #1253) so the actions module can grow an
authority model without becoming a 750-line file that mixes five concerns. Nothing
here mutates takedown state. ``takedown_service`` re-exports every public name, so
existing ``from app.services.takedown_service import exclude_quarantined`` imports
keep working.

  * :func:`exclude_quarantined` — the SQL predicate that drops quarantined rows
    from list/gallery/search queries (skipped for admin "see all").
  * :func:`is_hidden_for` — the per-request access gate used by the resource
    lookup choke-point (``get_file_by_uuid_with_permission``): a quarantined
    file 404s for non-admins, stays visible to platform admins for review.
  * :func:`is_notification_suppressed` / :func:`is_notification_suppressed_for_uuid` /
    :func:`filter_suppressed_file_uuids` — the WebSocket-push-side twin of
    ``exclude_quarantined``/``is_hidden_for``: a quarantined file's identity must
    not reach a non-admin over a live WS event any more than through a read
    surface. ``app/utils/websocket_notify.py:send_ws_event_for_file`` is the
    required call site for anything naming a ``MediaFile`` over WebSocket
    (issue #908).

Visibility is **platform-admin only**, including for the organization admin who
placed an organization takedown: an org admin reads a quarantined file exactly as
its owner does (not at all) and manages it from metadata (issue #1253).
"""

import logging
import uuid as uuid_pkg
from collections.abc import Sequence
from typing import Any

from sqlalchemy.orm import Query
from sqlalchemy.orm import Session

from app.models.media import MediaFile
from app.models.user import User

logger = logging.getLogger(__name__)


# Generic so a column-projecting RowReturningQuery keeps its row type through the
# optional filter instead of being widened to a plain Query.
def exclude_quarantined[QueryT: Query[Any]](
    query: QueryT, *, include_quarantined: bool = False
) -> QueryT:
    """Drop quarantined (taken-down) files from a ``MediaFile`` query.

    The list/gallery/search read paths call this so a taken-down file never
    appears for a normal user. ``include_quarantined=True`` (used by the admin
    "see all" branch and the dedicated review list) leaves the query untouched.

    Args:
        query: A query selecting from / filtering ``MediaFile``.
        include_quarantined: When True, do not add the exclusion (admin view).

    Returns:
        The query, optionally with ``is_quarantined IS NOT TRUE`` applied.
    """
    if include_quarantined:
        return query
    # ``is_(False)`` (rather than ``!= True``) keeps the partial index usable and
    # is correct because the column is NOT NULL with a false default.
    return query.filter(MediaFile.is_quarantined.is_(False))


def is_hidden_for(file: MediaFile, *, is_admin: bool) -> bool:
    """Whether ``file`` must be treated as non-existent for this caller.

    A quarantined file is hidden from everyone EXCEPT admins (who review/release
    it). The per-resource lookup helper turns a True here into a 404 so a
    taken-down file is indistinguishable from a missing one for normal users.

    Args:
        file: The resolved media file.
        is_admin: Whether the caller has admin (review) privileges.

    Returns:
        True if the file is quarantined and the caller is not an admin.
    """
    return bool(getattr(file, "is_quarantined", False)) and not is_admin


def is_notification_suppressed(file_id: int, recipient_user_id: int) -> bool:
    """Whether a file-scoped task notification to ``recipient_user_id`` must be dropped.

    Consulted by ``notification_service.send_task_notification`` before it
    auto-attaches file metadata (filename/content_type/file_size) to a
    WebSocket event: a quarantined file's identity must not reach a non-admin
    recipient through the Celery/WebSocket notification funnel any more than
    through the read surfaces ``exclude_quarantined``/``is_hidden_for`` already
    gate — a "file_updated" toast naming a taken-down file would otherwise leak
    exactly what the quarantine hides. Admins are exempt, matching every other
    review-visibility rule in this module.

    Deliberately does NOT cover ``_notify_owner_takedown``/``_notify_owner_release``
    (the DMCA §512(g) notices): those pass the file's identity in ``extra``, never
    as this function's ``file_id`` kwarg, so this predicate is never consulted for
    them — the owner must always learn about their own takedown/release.

    Opens its OWN short session — this runs deep inside a Celery task's
    notification call, never with a session already open — and **fails CLOSED**:
    a DB error suppresses the notification rather than risking a leak. The
    tradeoff is asymmetric and deliberate: the missed event is one live-update
    frame the SPA reconciles on its next poll/reload, while a leaked
    notification for a taken-down file is a filename disclosure that cannot be
    undone. Logged at WARNING so a persistent DB problem stays visible instead
    of silently dropping every notification forever.

    Args:
        file_id: The MediaFile's database id (not uuid).
        recipient_user_id: The user the notification would be sent to.

    Returns:
        True if the notification must be suppressed.
    """
    try:
        from app.db.session_utils import session_scope

        with session_scope() as db:
            file = db.query(MediaFile).filter(MediaFile.id == file_id).first()
            if file is None or not bool(getattr(file, "is_quarantined", False)):
                return False
            return not is_review_admin(db, recipient_user_id)
    except Exception as e:  # noqa: BLE001 — fail CLOSED, see docstring
        logger.warning(
            f"Notification suppression check failed for file {file_id}; "
            f"suppressing as a precaution: {e}"
        )
        return True


def is_notification_suppressed_for_uuid(
    file_uuid: str | uuid_pkg.UUID, recipient_user_id: int
) -> bool:
    """UUID-keyed twin of :func:`is_notification_suppressed`.

    Most Celery tasks in the pipeline hold only a file's UUID (never its Postgres
    integer id) at the point they are ready to fire a WebSocket notification, so a
    caller that only has a UUID had no suppression check to call before this —
    issue #908 found 20 such call sites reaching ``send_ws_event`` directly.

    Same contract as the id-keyed original: fails CLOSED. A UUID that does not
    parse, or that does not resolve to any row, is treated as suppressed — the
    caller cannot *prove* the file is not quarantined, and the asymmetry (a
    missed notification vs. a filename disclosure) is the same one
    :func:`is_notification_suppressed` accepts. A DB error is likewise
    suppressed rather than risking a leak.

    Args:
        file_uuid: The MediaFile's UUID (string or ``UUID``).
        recipient_user_id: The user the notification would be sent to.

    Returns:
        True if the notification must be suppressed.
    """
    try:
        normalized = _coerce_uuid(file_uuid)
        if normalized is None:
            return True  # malformed/unknown — cannot prove it's safe to send

        from app.db.session_utils import session_scope

        with session_scope() as db:
            file = db.query(MediaFile).filter(MediaFile.uuid == normalized).first()
            if file is None:
                # Unlike the id-keyed original, an unresolvable UUID here is
                # NOT treated as "clearly nothing to leak" — the docstring's
                # contract is fail-closed on "cannot prove it's safe", and a
                # UUID (unlike an internal auto-increment id) has no meaning
                # by construction, so a miss is at least as likely to be an
                # upstream typo/race as a genuinely deleted row.
                return True
            if not bool(getattr(file, "is_quarantined", False)):
                return False
            return not is_review_admin(db, recipient_user_id)
    except Exception as e:  # noqa: BLE001 — fail CLOSED, see docstring
        logger.warning(
            f"Notification suppression check failed for file {file_uuid}; "
            f"suppressing as a precaution: {e}"
        )
        return True


def filter_suppressed_file_uuids(
    file_uuids: Sequence[str | uuid_pkg.UUID], recipient_user_id: int
) -> list[str]:
    """The visible subset of ``file_uuids`` for ``recipient_user_id``, in input order.

    For a multi-file WebSocket event (e.g. a bulk speaker-rename propagation) that
    names every touched file's UUID in its payload — the single-file predicates
    above cannot answer "which of these"; this is the batch counterpart. ONE
    query for the whole list, not N per-uuid round trips.

    Fails CLOSED: any exception returns ``[]`` (nothing is provably safe to
    disclose), matching the asymmetry the single-file checks accept. Admins get
    the full (de-duplication-preserving) list back, same rule as everywhere else
    in this module. A malformed or unresolvable UUID is dropped — it cannot be
    proven un-quarantined, so it is treated the same as "quarantined" for
    disclosure purposes.

    Args:
        file_uuids: The candidate UUIDs (string or ``UUID``), as they appear in
            the caller's payload.
        recipient_user_id: The user the notification would be sent to.

    Returns:
        The stringified UUIDs that are safe to disclose to this recipient,
        preserving the input order (and any duplicates).
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
            normalized = _coerce_uuid(raw)
            if normalized is not None:
                parsed[key] = normalized

        if not parsed:
            return []

        from app.db.session_utils import session_scope

        with session_scope() as db:
            rows = (
                db.query(MediaFile.uuid, MediaFile.is_quarantined)
                .filter(MediaFile.uuid.in_(parsed.values()))
                .all()
            )
            quarantined_by_key = {str(row[0]): bool(row[1]) for row in rows}
            recipient_is_admin = is_review_admin(db, recipient_user_id)

        visible: list[str] = []
        for raw in candidates:
            key = str(raw)
            if key not in parsed:
                continue  # malformed — cannot prove un-quarantined
            is_quarantined = quarantined_by_key.get(key)
            if is_quarantined is None:
                continue  # unknown uuid — cannot prove un-quarantined
            if is_quarantined and not recipient_is_admin:
                continue
            visible.append(key)
        return visible
    except Exception as e:  # noqa: BLE001 — fail CLOSED, see docstring
        logger.warning(
            f"Batch notification suppression check failed for {len(candidates)} file(s); "
            f"suppressing all as a precaution: {e}"
        )
        return []


def _coerce_uuid(value: str | uuid_pkg.UUID) -> uuid_pkg.UUID | None:
    """Best-effort parse to a ``UUID``; ``None`` on anything that doesn't parse."""
    if isinstance(value, uuid_pkg.UUID):
        return value
    try:
        return uuid_pkg.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def is_review_admin(db: Session, user_id: int | None) -> bool:
    """Whether user_id holds the admin review visibility every predicate here is relative to.

    Public because the ASYNC plane needs it: a Celery task holds a user id, never a
    User, and must re-resolve the role at RUN time rather than trust a flag captured
    at dispatch. None is never an admin.
    """
    if user_id is None:
        return False
    recipient = db.query(User).filter(User.id == int(user_id)).first()
    return bool(recipient and recipient.is_admin)
