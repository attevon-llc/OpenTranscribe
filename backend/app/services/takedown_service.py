"""Abuse / DMCA / safe-harbor takedown service — the one place quarantine lives.

This is the enforcement mechanism behind the abuse-intake / DMCA policy (see
``docs/abuse-and-takedown.md``). A **quarantine** (takedown) hides a ``MediaFile``
from every read surface for non-admins while keeping the row, the media, and the
transcript intact (so the action is reversible and leaves an audit + appeal
trail). It is deliberately INDEPENDENT of the processing ``status`` so a fully
``COMPLETED`` file can be taken down and later released back to exactly its prior
state. Note: toxicity/PII redaction masks *text* — it is NOT a takedown; this
service is the takedown.

Five things live here so they can never drift apart:
  * :func:`exclude_quarantined` — the SQL predicate that drops quarantined rows
    from list/gallery/search queries (skipped for admin "see all").
  * :func:`is_hidden_for` — the per-request access gate used by the resource
    lookup choke-point (``get_file_by_uuid_with_permission``): a quarantined
    file 404s for non-admins, stays visible to admins for review.
  * :func:`quarantine_file` / :func:`release_file` — the admin actions, each of
    which writes the DB state, sets the S3 legal-hold (best-effort), and audits.
  * The DMCA §512(g) owner notices (:func:`_notify_owner_takedown` /
    :func:`_notify_owner_release`) — since the file stays hidden (404) from its
    owner while quarantined, a persistent in-app notification is the owner's
    only surface for learning about the takedown and how to counter-notice it.
  * :func:`apply_processing_status` / :func:`stamp_completion_time` — the two
    helpers every PIPELINE writer (never the admin actions above) must route a
    ``status``/``completed_at`` write through, so a file mid-transcription when
    it gets taken down is neither stranded at a stale status nor has a held
    file's retention clock restarted (issue #824).
  * :func:`is_notification_suppressed` / :func:`is_notification_suppressed_for_uuid` /
    :func:`filter_suppressed_file_uuids` — the WebSocket-push-side twin of
    ``exclude_quarantined``/``is_hidden_for``: a quarantined file's identity must
    not reach a non-admin over a live WS event any more than through a read
    surface. ``app/utils/websocket_notify.py:send_ws_event_for_file`` is the
    required call site for anything naming a ``MediaFile`` over WebSocket
    (issue #908); ``send_ws_event`` itself has no notion of quarantine at all.

Community-edition invariance: nothing quarantines automatically, the columns
default to the not-quarantined state, and ``exclude_quarantined``/``is_hidden_for``
are behavior-preserving no-ops for files that were never taken down.
"""

import logging
import time
import uuid as uuid_pkg
from collections.abc import Sequence
from datetime import UTC
from datetime import datetime

from sqlalchemy.orm import Query
from sqlalchemy.orm import Session

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.user import User

logger = logging.getLogger(__name__)

# Retry budget for clearing the presigned-URL-revocation tag on release (issue #907).
# This is the dangerous edge of the two directions: a newly-minted presigned URL for a
# STILL-TAGGED object is ALSO 403 (measured), so a failed untag leaves a file released
# in the DB but permanently unplayable until a retry succeeds.
_PRESIGN_UNTAG_RETRIES = 3
_PRESIGN_UNTAG_BACKOFF_SECONDS = 0.5

# WebSocket event types consumed by the frontend notification store (owner notices).
OWNER_TAKEDOWN_EVENT = "file_takedown"
OWNER_RELEASE_EVENT = "file_takedown_released"


def exclude_quarantined(query: Query, *, include_quarantined: bool = False) -> Query:
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


def apply_processing_status(file: MediaFile, new_status: FileStatus) -> None:
    """Record a PIPELINE status transition on a file that may be under takedown.

    Every writer of ``MediaFile.status`` that runs as part of the transcription
    pipeline (not an admin takedown/release action) must route the write through
    here instead of assigning ``file.status`` directly. A file that is quarantined
    -- or whose ``status`` is already ``QUARANTINED`` -- must keep displaying
    ``QUARANTINED`` for as long as the takedown is in effect; the pipeline's real
    verdict is instead recorded into ``pre_quarantine_status``, which is exactly
    the column :func:`release_file` reads to restore the file's true state on
    release. Skipping this and writing ``file.status`` directly was issue #824's
    bug: a second, unguarded writer (``app/utils/task_utils.py``) clobbered
    ``QUARANTINED`` with ``PROCESSING`` or ``COMPLETED`` seconds after the first
    guard declined to write, and because ``release_file`` only restores from
    ``pre_quarantine_status`` when ``status == QUARANTINED``, a file caught that
    way was stranded -- not just display-wrong, permanently unreachable by the
    one release path that exists.

    Args:
        file: The media file the pipeline just reached a new status for.
        new_status: The status the pipeline would write if the file were not
            under takedown.
    """
    if file.is_quarantined or file.status == FileStatus.QUARANTINED:
        file.pre_quarantine_status = new_status.value
        return
    file.status = new_status


def stamp_completion_time(file: MediaFile, when: datetime) -> None:
    """Set ``completed_at`` without restarting a held file's retention clock.

    ``completed_at`` is the column the retention sweep (``cleanup._select_expired_files``,
    issue #664) measures its window from -- but that predicate already excludes any row
    with ``legal_hold`` or ``is_quarantined`` set, so writing this column while a file is
    held is inert until release, not a live hazard. The one real hazard is narrower: a
    file that ALREADY completed before takedown (so ``completed_at`` already holds a real
    timestamp) getting that timestamp MOVED FORWARD by a re-process while held, which
    would extend retention after release. So the rule is: write a currently-NULL
    ``completed_at`` unconditionally (a file must be able to finish and be released with a
    real timestamp -- see :func:`apply_processing_status`'s docstring for what happens
    when it can't), but never move an EXISTING timestamp forward while the file is held.

    Args:
        file: The media file to stamp.
        when: The real completion time (``datetime.now(UTC)`` at the call site).
    """
    if file.completed_at is None or not (file.is_quarantined or file.legal_hold):
        file.completed_at = when


def _untag_quarantine_with_retry(object_name: str) -> bool:
    """Clear the presigned-URL-revocation tag, retrying before giving up.

    ``set_object_quarantine_tag`` never raises (it is itself best-effort), so a
    "failure" here is a ``False`` return — the object still carries the tag, or the
    call couldn't reach storage. Retried up to :data:`_PRESIGN_UNTAG_RETRIES` times
    with a short backoff, because an object left tagged after release 403s a
    BRAND NEW presigned URL too (measured) — this is not "the same URL keeps
    working a bit longer", it is "the file is unplayable until this succeeds".

    Args:
        object_name: Object key to untag.

    Returns:
        True once the tag is confirmed cleared, False if every attempt failed.
    """
    from app.services.minio_service import set_object_quarantine_tag

    for attempt in range(1, _PRESIGN_UNTAG_RETRIES + 1):
        if set_object_quarantine_tag(object_name, False):
            return True
        if attempt < _PRESIGN_UNTAG_RETRIES:
            time.sleep(_PRESIGN_UNTAG_BACKOFF_SECONDS)
    return False


def quarantine_file(
    db: Session,
    file: MediaFile,
    *,
    admin: User,
    reason: str,
    legal_hold: bool = True,
    source_ip: str = "",
    user_agent: str = "",
) -> MediaFile:
    """Take a file down: mark it quarantined, set the legal-hold, and audit.

    Idempotent — re-quarantining an already-held file just refreshes the reason
    and timestamp. The processing ``status`` is overwritten with
    ``QUARANTINED`` for display, but the authoritative ``is_quarantined`` flag is
    what gates access; ``release_file`` restores the file to a servable state.
    The owner gets a best-effort §512(g) notice (:func:`_notify_owner_takedown`).

    **Presigned-URL revocation (issue #907, FIXED):** browser-facing GET presigns
    (``GET /api/files/{uuid}/stream-url`` and the thumbnail/download equivalents) are
    signed by a dedicated, least-privilege MinIO service-account identity
    (``storage_presign_identity``) whose policy Denies ``s3:GetObject`` on any object
    carrying the ``STORAGE_QUARANTINE_TAG_KEY`` tag this function sets. A URL minted
    BEFORE this call 403s the instant the tag lands — same URL, no re-mint needed.
    This is MinIO-only (no admin API on native S3) and best-effort/fail-open: if the
    restricted identity couldn't be provisioned, presigning silently falls back to the
    root client (today's pre-#907 behavior) and ``file.presign_revoked`` reports
    ``False``. It also cannot un-download bytes a client already fetched before the
    tag landed. See ``docs/abuse-and-takedown.md`` for the full writeup, the
    admin-review 403 consequence, and the S3-operator bucket-policy equivalent.

    Args:
        db: Database session.
        file: The media file to take down.
        admin: The admin performing the takedown (for ``quarantined_by`` + audit).
        reason: Free-text takedown reason (DMCA notice ref, AUP clause, etc.).
        legal_hold: Also place a legal-hold on the row + S3 object (default True).
        source_ip / user_agent: Request metadata for the audit event.

    Returns:
        The updated (committed) media file. Carries a non-persisted
        ``presign_revoked: bool`` attribute (not a DB column — a storage-plane side
        effect, not app state) reporting whether the revocation tag was applied.
    """
    file.is_quarantined = True
    file.quarantine_reason = reason
    file.quarantined_at = datetime.now(UTC)
    file.quarantined_by = admin.id
    # Preserve the true prior status ONCE (re-quarantine must not overwrite it
    # with QUARANTINED) so release can restore the file's actual state.
    if file.status != FileStatus.QUARANTINED:
        file.pre_quarantine_status = (
            file.status.value if hasattr(file.status, "value") else str(file.status)
        )
    file.status = FileStatus.QUARANTINED
    if legal_hold:
        file.legal_hold = True

    db.commit()
    db.refresh(file)

    # A takedown changes what the searchable/chat-retrievable corpus may serve —
    # exactly the event `bump_corpus_version` exists for (its own docstring names
    # this scenario). The per-request post-filters (`exclude_quarantined`,
    # `_drop_quarantined_search_hits`, chat's quarantine visibility rule) are the
    # actual enforcement; this only stops a cached page/retrieval from outliving
    # them for the rest of its TTL. Never raises by construction — no try/except.
    from app.services.chat.retrieval_cache import bump_corpus_version

    bump_corpus_version()

    # Best-effort storage legal-hold (DB flag is the source of truth).
    if legal_hold and file.storage_path:
        try:
            from app.services.minio_service import set_object_legal_hold

            set_object_legal_hold(str(file.storage_path), True)
        except Exception as e:  # noqa: BLE001 — advisory; never break the takedown
            logger.warning(f"Storage legal-hold enable failed for file {file.id}: {e}")

    # Best-effort presigned-URL revocation (issue #907) — independent of `legal_hold`,
    # applies to every quarantine. Each object is tagged in its own try/except so a
    # thumbnail-tag failure can never be blamed on (or block) the primary-object tag.
    tagged = False
    if file.storage_path:
        try:
            from app.services.minio_service import set_object_quarantine_tag

            tagged = set_object_quarantine_tag(str(file.storage_path), True)
        except Exception as e:  # noqa: BLE001 — advisory; never break the takedown
            logger.warning(f"Presigned-URL revocation tag failed for file {file.id}: {e}")
    if file.thumbnail_path:
        try:
            from app.services.minio_service import set_object_quarantine_tag

            set_object_quarantine_tag(str(file.thumbnail_path), True)
        except Exception as e:  # noqa: BLE001 — advisory; never break the takedown
            logger.warning(
                f"Presigned-URL revocation tag failed for file {file.id}'s thumbnail: {e}"
            )

    # Non-persisted (not a DB column — see the TYPE_CHECKING-only declaration on
    # MediaFile): the admin quarantine endpoint reads this off the returned object
    # to report the storage-plane outcome to the caller.
    file.presign_revoked = tagged

    _audit(
        AuditEventType.ADMIN_FILE_QUARANTINE,
        admin=admin,
        file=file,
        source_ip=source_ip,
        user_agent=user_agent,
        extra={"reason": reason, "legal_hold": bool(legal_hold), "presign_revoked": bool(tagged)},
    )
    logger.info(f"File {file.id} ({file.uuid}) quarantined by admin {admin.id}: {reason}")
    _notify_owner_takedown(file, reason=reason)
    return file


def release_file(
    db: Session,
    file: MediaFile,
    *,
    admin: User,
    restore_status: FileStatus | None = None,
    clear_legal_hold: bool = True,
    source_ip: str = "",
    user_agent: str = "",
) -> MediaFile:
    """Release a quarantined file: clear the flag, optionally lift the hold, audit.

    Restores access for normal users. The display status goes back to the
    file's recorded ``pre_quarantine_status`` (captured at takedown time);
    ``restore_status`` overrides it when the caller knows better, and
    ``COMPLETED`` is the last-resort fallback for rows quarantined before the
    prior status was recorded (pre-v371). The owner gets a best-effort
    access-restored notice (:func:`_notify_owner_release`).

    **Presigned-URL revocation (issue #907):** this is the dangerous edge of that
    mechanism. Clearing the ``STORAGE_QUARANTINE_TAG_KEY`` tag is what restores the
    file's ALREADY-MINTED presigned URL to working — but also what a BRAND NEW
    presigned URL needs: a still-tagged object 403s a freshly signed URL too
    (measured). So the untag is retried (:func:`_untag_quarantine_with_retry`) before
    giving up, and a final failure logs at ERROR (not the legal-hold sibling's
    WARNING) and is surfaced as ``file.presign_tag_cleared = False`` — a file released
    in the DB but left permanently unplayable until the tag is cleared.

    Args:
        db: Database session.
        file: The quarantined media file.
        admin: The admin performing the release (for the audit trail).
        restore_status: Explicit status override (default: the recorded prior status).
        clear_legal_hold: Also lift the DB + S3 legal-hold (default True).
        source_ip / user_agent: Request metadata for the audit event.

    Returns:
        The updated (committed) media file. Carries a non-persisted
        ``presign_tag_cleared: bool`` attribute (not a DB column) reporting whether
        the revocation tag was successfully cleared.
    """
    file.is_quarantined = False
    file.quarantine_reason = None
    file.quarantined_at = None
    file.quarantined_by = None
    if file.status == FileStatus.QUARANTINED:
        if restore_status is not None:
            file.status = restore_status
        else:
            try:
                file.status = FileStatus(file.pre_quarantine_status)
            except ValueError:
                file.status = FileStatus.COMPLETED
    file.pre_quarantine_status = None
    if clear_legal_hold:
        file.legal_hold = False

    db.commit()
    db.refresh(file)

    # Release also changes what the corpus may serve (the file is servable
    # again) — same reasoning as the bump in `quarantine_file`. Never raises.
    from app.services.chat.retrieval_cache import bump_corpus_version

    bump_corpus_version()

    if clear_legal_hold and file.storage_path:
        try:
            from app.services.minio_service import set_object_legal_hold

            set_object_legal_hold(str(file.storage_path), False)
        except Exception as e:  # noqa: BLE001 — advisory; never break the release
            logger.warning(f"Storage legal-hold disable failed for file {file.id}: {e}")

    # Best-effort presigned-URL untag (issue #907), retried — see this function's
    # docstring for why a failure here is worse than the legal-hold sibling's.
    # Independent of `clear_legal_hold`, mirroring quarantine_file's tag-on being
    # independent of `legal_hold`.
    cleared = True
    if file.storage_path:
        cleared = _untag_quarantine_with_retry(str(file.storage_path))
        if not cleared:
            logger.error(
                f"Presign-tag clear FAILED after {_PRESIGN_UNTAG_RETRIES} attempts for file "
                f"{file.id} ({file.uuid}) — the file is released in the DB but its "
                "presigned media URL will keep returning 403 until this is retried "
                "and succeeds."
            )
    if file.thumbnail_path and not _untag_quarantine_with_retry(str(file.thumbnail_path)):
        logger.error(
            f"Presign-tag clear FAILED after {_PRESIGN_UNTAG_RETRIES} attempts for file "
            f"{file.id}'s ({file.uuid}) thumbnail."
        )

    # Non-persisted (not a DB column — see the TYPE_CHECKING-only declaration on
    # MediaFile): the admin release endpoint reads this off the returned object to
    # report the storage-plane outcome to the caller.
    file.presign_tag_cleared = cleared

    _audit(
        AuditEventType.ADMIN_FILE_RELEASE,
        admin=admin,
        file=file,
        source_ip=source_ip,
        user_agent=user_agent,
        extra={
            "cleared_legal_hold": bool(clear_legal_hold),
            "presign_tag_cleared": bool(cleared),
        },
    )
    logger.info(f"File {file.id} ({file.uuid}) released from quarantine by admin {admin.id}")
    _notify_owner_release(file)

    # Lifting a hold can UNBLOCK a GDPR erasure (issue #442). An Art. 17 request that
    # hit this file was deferred, not abandoned — ``_purge_files`` skipped it under
    # Art. 17(3)(e) and, because ``media_file.user_id`` is a NO ACTION FK, the whole
    # account survived with it. Until this call existed, releasing the hold ended the
    # only justification for that retention and nothing noticed: the deferral became
    # permanent non-compliance. Dispatch only — the sweep decides which entries are now
    # finishable, and it runs on a schedule regardless, so a failed dispatch delays the
    # completion rather than losing it.
    if clear_legal_hold:
        from app.tasks.erasure_reconciliation import notify_hold_released

        notify_hold_released(file)
    return file


def _notify_owner_takedown(file: MediaFile, *, reason: str) -> None:
    """Send the file OWNER the DMCA §512(g) takedown notice. Never raises.

    The quarantined file 404s for the owner on every read surface (by design),
    so this persistent in-app notification is the owner's only in-product way
    to learn a takedown happened and how to dispute it. It carries the
    admin-recorded reason and the deployment's abuse-contact address for a
    counter-notice — and deliberately NOT the acting admin's identity.
    """
    try:
        from app.core.config import settings
        from app.services.notification_service import send_task_notification

        display_name = file.title or file.filename
        contact = settings.ABUSE_CONTACT_EMAIL or ""
        if contact:
            dispute = (
                f"To dispute it, email a counter-notice to {contact} "
                f"referencing file ID {file.uuid}."
            )
        else:
            dispute = (
                "To dispute it, contact your service operator with a "
                f"counter-notice referencing file ID {file.uuid}."
            )
        send_task_notification(
            int(file.user_id),
            OWNER_TAKEDOWN_EVENT,
            status="warning",
            message=(
                f'Your file "{display_name}" has been taken down by the service '
                f"operator following an abuse/copyright report. Reason: {reason}. {dispute}"
            ),
            extra={
                "file_uuid": str(file.uuid),
                "filename": display_name,
                "reason": reason,
                "abuse_contact_email": contact,
            },
        )
    except Exception as e:  # noqa: BLE001 — the notice must never break the takedown
        logger.warning(f"Owner takedown notification failed for file {file.id}: {e}")


def _notify_owner_release(file: MediaFile) -> None:
    """Notify the file OWNER that a taken-down file was restored. Never raises.

    Counterpart of :func:`_notify_owner_takedown` — sent after a successful
    counter-notice / withdrawn complaint, when an admin releases the file.
    ``file_id`` (unlike the takedown notice) is included so the notification
    panel can link to the now-accessible file.
    """
    try:
        from app.services.notification_service import send_task_notification

        display_name = file.title or file.filename
        send_task_notification(
            int(file.user_id),
            OWNER_RELEASE_EVENT,
            status="completed",
            message=f'Your file "{display_name}" has been restored and is accessible again.',
            extra={
                "file_id": str(file.uuid),
                "file_uuid": str(file.uuid),
                "filename": display_name,
            },
        )
    except Exception as e:  # noqa: BLE001 — the notice must never break the release
        logger.warning(f"Owner release notification failed for file {file.id}: {e}")


def _audit(
    event_type: AuditEventType,
    *,
    admin: User,
    file: MediaFile,
    source_ip: str,
    user_agent: str,
    extra: dict,
) -> None:
    """Write a takedown/release audit event (never raises)."""
    details = {"file_id": file.id, "file_uuid": str(file.uuid), **extra}
    try:
        audit_logger.log(
            event_type=event_type,
            outcome=AuditOutcome.SUCCESS,
            user_id=int(admin.id),
            username=str(admin.email),
            source_ip=source_ip or None,
            user_agent=user_agent or None,
            details=details,
            # Org attribution (issue #262a): a takedown of an org-stamped file
            # is that tenant's event — its org admins see it in the audit read.
            organization_id=int(file.organization_id) if file.organization_id else None,
        )
    except Exception as e:  # noqa: BLE001 — audit must never break the action
        logger.warning(f"Audit log for {event_type} failed: {e}")
