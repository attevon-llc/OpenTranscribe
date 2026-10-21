"""Abuse / DMCA / safe-harbor takedown service — the one place quarantine lives.

This is the enforcement mechanism behind the abuse-intake / DMCA policy (see
``docs/abuse-and-takedown.md``). A **quarantine** (takedown) hides a ``MediaFile``
from every read surface for non-admins while keeping the row, the media, and the
transcript intact (so the action is reversible and leaves an audit + appeal
trail). It is deliberately INDEPENDENT of the processing ``status`` so a fully
``COMPLETED`` file can be taken down and later released back to exactly its prior
state. Note: toxicity/PII redaction masks *text* — it is NOT a takedown; this
service is the takedown.

The module is a facade over four, split by concern (issue #1253):

  * :mod:`app.services.takedown_predicates` — the read side: ``exclude_quarantined``,
    ``is_hidden_for``, ``is_review_admin`` and the WebSocket suppression twins.
    Re-exported here, so every existing import keeps working.
  * :mod:`app.services.takedown_authority` — who may take a file down or release it
    (``platform`` or ``organization``, "platform wins").
  * :mod:`app.services.takedown_notices` — the §512(g) owner notices and the audit
    events.
  * **this module** — :func:`quarantine_file` / :func:`release_file`, the only code
    that mutates takedown state, and :func:`apply_processing_status` /
    :func:`stamp_completion_time`, the two helpers every PIPELINE writer (never the
    actions above) must route a ``status``/``completed_at`` write through, so a file
    mid-transcription when it gets taken down is neither stranded at a stale status
    nor has a held file's retention clock restarted (issue #824).

Community-edition invariance: nothing quarantines automatically, the columns
default to the not-quarantined state, and ``exclude_quarantined``/``is_hidden_for``
are behavior-preserving no-ops for files that were never taken down.
"""

import logging
import time
from collections.abc import Callable
from datetime import UTC
from datetime import datetime

from sqlalchemy.orm import Session

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.core.enums import FileStatus
from app.core.enums import TakedownOrigin
from app.models.media import MediaFile
from app.services.takedown_authority import TakedownActor
from app.services.takedown_authority import TakedownRefusedError
from app.services.takedown_authority import assert_may_quarantine
from app.services.takedown_authority import assert_may_release
from app.services.takedown_authority import current_origin
from app.services.takedown_authority import origin_after_release
from app.services.takedown_notices import OWNER_RELEASE_EVENT
from app.services.takedown_notices import OWNER_TAKEDOWN_EVENT
from app.services.takedown_notices import audit_takedown
from app.services.takedown_notices import notify_owner_release
from app.services.takedown_notices import notify_owner_takedown
from app.services.takedown_predicates import exclude_quarantined
from app.services.takedown_predicates import filter_suppressed_file_uuids
from app.services.takedown_predicates import is_hidden_for
from app.services.takedown_predicates import is_notification_suppressed
from app.services.takedown_predicates import is_notification_suppressed_for_uuid
from app.services.takedown_predicates import is_review_admin

__all__ = [
    "OWNER_RELEASE_EVENT",
    "OWNER_TAKEDOWN_EVENT",
    "TakedownActor",
    "TakedownRefusedError",
    "apply_processing_status",
    "exclude_quarantined",
    "filter_suppressed_file_uuids",
    "is_hidden_for",
    "is_notification_suppressed",
    "is_notification_suppressed_for_uuid",
    "is_review_admin",
    "quarantine_file",
    "release_file",
    "stamp_completion_time",
]

logger = logging.getLogger(__name__)

# Retry budget for clearing the presigned-URL-revocation tag on release (issue #907).
# This is the dangerous edge of the two directions: a newly-minted presigned URL for a
# STILL-TAGGED object is ALSO 403 (measured), so a failed untag leaves a file released
# in the DB but permanently unplayable until a retry succeeds.
_PRESIGN_UNTAG_RETRIES = 3
_PRESIGN_UNTAG_BACKOFF_SECONDS = 0.5


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


def _lock_and_authorize(
    db: Session,
    file: MediaFile,
    *,
    actor: TakedownActor,
    event_type: AuditEventType,
    check: Callable[[TakedownActor, MediaFile], None],
    source_ip: str,
    user_agent: str,
) -> TakedownOrigin | None:
    """Lock ``file``'s row, then decide whether ``actor`` may act on it.

    The lock makes the authority decision and the write one atomic step: two actors
    racing to escalate or to release see each other's committed state, not a stale
    copy. The check runs BEFORE any mutation or storage side effect, so a refusal
    leaves the row, the object tags, the corpus version and the owner untouched; the
    only trace is one ``FAILURE`` audit event.

    Returns:
        The origin that held the file before this action (``None`` if nobody did).

    Raises:
        TakedownRefusedError: The actor's authority does not cover the action.
    """
    db.refresh(file, with_for_update=True)
    try:
        check(actor, file)
    except TakedownRefusedError as refusal:
        audit_takedown(
            event_type,
            actor=actor,
            file=file,
            source_ip=source_ip,
            user_agent=user_agent,
            extra={"refused": True},
            outcome=AuditOutcome.FAILURE,
            error_code=refusal.code,
        )
        raise
    return current_origin(file)


def quarantine_file(
    db: Session,
    file: MediaFile,
    *,
    actor: TakedownActor,
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
    The owner gets a best-effort §512(g) notice (:func:`notify_owner_takedown`).

    **Authority (issue #1253):** ``actor.authority`` is recorded in
    ``takedown_origin``. A platform actor may always act, and taking down a file an
    organization holds escalates it to ``platform``; an organization actor is
    refused (:class:`TakedownRefusedError`, nothing changed) on a file the operator
    holds. See :mod:`app.services.takedown_authority` for the full table.

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
        actor: Who is acting and with which authority (``quarantined_by`` + audit).
        reason: Free-text takedown reason (DMCA notice ref, AUP clause, etc.).
        legal_hold: Also place a legal-hold on the row + S3 object (default True).
        source_ip / user_agent: Request metadata for the audit event.

    Returns:
        The updated (committed) media file. Carries a non-persisted
        ``presign_revoked: bool`` attribute (not a DB column — a storage-plane side
        effect, not app state) reporting whether the revocation tag was applied.

    Raises:
        TakedownRefusedError: The actor may not take this file down.
    """
    previous_origin = _lock_and_authorize(
        db,
        file,
        actor=actor,
        event_type=AuditEventType.ADMIN_FILE_QUARANTINE,
        check=assert_may_quarantine,
        source_ip=source_ip,
        user_agent=user_agent,
    )
    previous_reason = file.quarantine_reason
    file.is_quarantined = True
    file.quarantine_reason = reason
    file.quarantined_at = datetime.now(UTC)
    file.quarantined_by = actor.user.id
    file.takedown_origin = actor.authority.value
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
    # The playback rendition is the same media in another codec, so an already-minted
    # URL for it must die with the original's. It is not put under legal hold: it is a
    # regenerable copy, and the original is the evidentiary one.
    if file.playback_path:
        try:
            from app.services.minio_service import set_object_quarantine_tag

            set_object_quarantine_tag(str(file.playback_path), True)
        except Exception as e:  # noqa: BLE001 — advisory; never break the takedown
            logger.warning(
                f"Presigned-URL revocation tag failed for file {file.id}'s playback rendition: {e}"
            )

    # Non-persisted (not a DB column — see the TYPE_CHECKING-only declaration on
    # MediaFile): the quarantine endpoints read this off the returned object
    # to report the storage-plane outcome to the caller.
    file.presign_revoked = tagged

    extra: dict = {
        "reason": reason,
        "legal_hold": bool(legal_hold),
        "presign_revoked": bool(tagged),
        "previous_origin": previous_origin.value if previous_origin else None,
    }
    if previous_reason is not None:
        # An escalation or refresh overwrites the reason; the earlier one survives here.
        extra["previous_reason"] = previous_reason
    audit_takedown(
        AuditEventType.ADMIN_FILE_QUARANTINE,
        actor=actor,
        file=file,
        source_ip=source_ip,
        user_agent=user_agent,
        extra=extra,
    )
    logger.info(
        f"File {file.id} ({file.uuid}) quarantined by {actor.role_label} {actor.user.id}: {reason}"
    )
    notify_owner_takedown(file, reason=reason, origin=actor.authority)
    return file


def release_file(
    db: Session,
    file: MediaFile,
    *,
    actor: TakedownActor,
    restore_status: FileStatus | None = None,
    clear_legal_hold: bool = True,
    reason: str | None = None,
    source_ip: str = "",
    user_agent: str = "",
) -> MediaFile:
    """Release a quarantined file: clear the flag, optionally lift the hold, audit.

    Restores access for normal users. The display status goes back to the
    file's recorded ``pre_quarantine_status`` (captured at takedown time);
    ``restore_status`` overrides it when the caller knows better, and
    ``COMPLETED`` is the last-resort fallback for rows quarantined before the
    prior status was recorded (pre-v371). The owner gets a best-effort
    access-restored notice (:func:`notify_owner_release`).

    **Authority (issue #1253):** a platform actor may release anything; an
    organization actor may release only what an organization holds and is refused
    (:class:`TakedownRefusedError`, nothing changed) on a platform takedown. The
    recorded origin survives a release that keeps the legal hold and is cleared once
    nothing holds the file.

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
        actor: Who is acting and with which authority (for the audit trail).
        restore_status: Explicit status override (default: the recorded prior status).
        clear_legal_hold: Also lift the DB + S3 legal-hold (default True).
        reason: Optional free-text release reason, recorded in the audit event only.
        source_ip / user_agent: Request metadata for the audit event.

    Returns:
        The updated (committed) media file. Carries a non-persisted
        ``presign_tag_cleared: bool`` attribute (not a DB column) reporting whether
        the revocation tag was successfully cleared.

    Raises:
        TakedownRefusedError: The actor may not release this file.
    """
    previous_origin = _lock_and_authorize(
        db,
        file,
        actor=actor,
        event_type=AuditEventType.ADMIN_FILE_RELEASE,
        check=assert_may_release,
        source_ip=source_ip,
        user_agent=user_agent,
    )
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
    remaining_origin = origin_after_release(file)
    file.takedown_origin = remaining_origin.value if remaining_origin else None

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
    if file.playback_path and not _untag_quarantine_with_retry(str(file.playback_path)):
        logger.error(
            f"Presign-tag clear FAILED after {_PRESIGN_UNTAG_RETRIES} attempts for file "
            f"{file.id}'s ({file.uuid}) playback rendition — it will not play until retried."
        )

    # Non-persisted (not a DB column — see the TYPE_CHECKING-only declaration on
    # MediaFile): the release endpoints read this off the returned object to
    # report the storage-plane outcome to the caller.
    file.presign_tag_cleared = cleared

    extra: dict = {
        "cleared_legal_hold": bool(clear_legal_hold),
        "presign_tag_cleared": bool(cleared),
        "previous_origin": previous_origin.value if previous_origin else None,
    }
    if reason:
        extra["reason"] = reason
    audit_takedown(
        AuditEventType.ADMIN_FILE_RELEASE,
        actor=actor,
        file=file,
        source_ip=source_ip,
        user_agent=user_agent,
        extra=extra,
    )
    logger.info(
        f"File {file.id} ({file.uuid}) released from quarantine by "
        f"{actor.role_label} {actor.user.id}"
    )
    notify_owner_release(file)

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
