"""Read-time redaction gate for transcript reads.

Resolves the caller's effective redaction policy for a transcript read (and who may reveal
what), withholds a transcript whose detection scan has not finished, and owns the one
claim/publish/withdraw protocol that queues a file's detection scan.
"""

import logging
from typing import Any
from typing import NamedTuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core import redaction_defaults
from app.core.tenancy import UNSCOPED
from app.core.tenancy import OrgScope
from app.core.tenancy import _Unscoped
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.user import User
from app.services.platform_bypass import PlatformBypass
from app.services.redaction.reveal_audit import audit_unredacted_reveal

logger = logging.getLogger(__name__)


def _resolve_redaction_for_request(
    db: Session,
    db_file: MediaFile,
    current_user: User,
    *,
    bypass: PlatformBypass,
    redact: bool,
    organization_id: OrgScope = UNSCOPED,
) -> tuple[Any, set]:
    """Resolve (effective_cfg, reveal_categories) for a transcript read.

    The owner (and admins, audited) may set ``redact=false`` to reveal NON-forced
    categories; admin-forced categories stay masked. Non-owners never reveal. In
    multi-tenant mode only the owner reveals, and under a support-access grant the policy
    that applies is the owner's, not the staff member's.

    Args:
        organization_id: The requester's active tenant scope. ``UNSCOPED`` (the
            legacy/non-HTTP default) normalizes to personal scope, since
            ``resolve_effective_config`` takes a strict ``int | None`` — never
            the sentinel itself (#988). The one production caller always passes
            the request's own ``ctx.org_id`` instead.

    Raises:
        HTTPException: 503 when the redaction policy cannot be resolved.
    """
    try:
        from app.services.redaction.config import resolve_effective_config

        resolved_org_id = None if isinstance(organization_id, _Unscoped) else organization_id
        if bypass.under_grant:
            # Support sees exactly what the tenant's policy shows: the policy of the file's
            # OWNER in the file's tenant, never the staff member's own (looser) preferences.
            cfg = resolve_effective_config(
                db, int(db_file.user_id), organization_id=db_file.organization_id
            )
        else:
            cfg = resolve_effective_config(db, current_user.id, organization_id=resolved_org_id)
    except Exception as e:
        # FAIL CLOSED. Returning (None, set()) told every downstream reader that
        # redaction was off: `_apply_redaction` short-circuits on a None config
        # and returned every segment raw, `_redaction_pending` stopped withholding
        # transcripts mid-detection, and no `transcript.view_unredacted` audit
        # event fired because `reveal` was empty — an unredacted read with no
        # compliance trail, including admin-forced categories. Refuse the read.
        logger.exception("Failed to resolve redaction config; refusing the transcript read")
        raise HTTPException(
            status_code=503,
            detail="Redaction policy is temporarily unavailable; transcript withheld.",
        ) from e

    can_reveal = bool(db_file.user_id == current_user.id) or bypass.admin_reveal_allowed
    reveal = cfg.reveal_categories(requested=(redact is False), is_owner=can_reveal)

    audit_unredacted_reveal(db_file, current_user, reveal, surface="transcript")

    return cfg, reveal


class RedactionScanDispatch(NamedTuple):
    """Outcome of :func:`claim_and_dispatch_redaction_scan`.

    ``queued`` is the only thing a caller may branch on. ``task_id`` is for the
    operator-facing message and is ``None`` whenever the broker handed back
    something without an ``id`` — never a signal that the publish failed.
    """

    queued: bool
    task_id: str | None


def _withdraw_redaction_claim(db: Session, db_file: MediaFile, previous_status: str | None) -> None:
    """Undo a ``pending`` claim whose scan was never published — but only if it still stands.

    Deliberately a guarded ``UPDATE ... WHERE redaction_status = 'pending'`` rather
    than an ORM attribute write. Undoing the claim is correct only while the claim is
    the row's current state. Between our commit and this call another session may
    legitimately have moved it on: a re-scan queued by
    ``RedactionService._mark_redaction_stale`` after a segment edit, or a worker
    already writing ``processing``/``done``. An unconditional write would then either
    resurrect the old ``done`` — making cached spans that no longer cover the text
    authoritative again — or cancel a scan that is genuinely running. The guard costs
    one indexed UPDATE and removes the whole class.

    Never raises: this runs on an error path, and a failure to withdraw is logged as
    the stranding it is rather than replacing the caller's exception.
    """
    try:
        withdrawn = (
            db.query(MediaFile)
            .filter(
                MediaFile.id == db_file.id,
                MediaFile.redaction_status == redaction_defaults.REDACTION_STATUS_PENDING,
            )
            .update({MediaFile.redaction_status: previous_status}, synchronize_session=False)
        )
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception(
            "File %s is stranded at 'pending' with no scan queued; its transcript will "
            "stay withheld until the status is cleared",
            db_file.id,
        )
        return

    if not withdrawn:
        logger.info(
            "Not withdrawing the redaction claim on file %s: another writer moved it off "
            "'pending' since we claimed it, so that writer's status is the current one.",
            db_file.id,
        )


def claim_and_dispatch_redaction_scan(
    db: Session, db_file: MediaFile, *, surface: str
) -> RedactionScanDispatch:
    """Claim ``pending``, publish the detection scan, and withdraw the claim if it never landed.

    **One implementation on purpose.** Two call sites need this exact protocol — the
    lazy dispatch on a transcript read (:func:`_lazy_dispatch_redaction`) and the
    bulk/selective "Redact" action
    (``endpoints/files/management._handle_redact_action``) — and the second shipped
    without the withdrawal, which is how files got stranded. A second copy of a
    recovery protocol is a second chance to omit half of it.

    The claim has to be written *before* the publish: two readers opening the same
    unscanned file, or two overlapping bulk batches, would otherwise queue two scans.
    But ``pending`` asserts "a scan is coming", and ``detect_and_store`` — the only
    writer of a terminal status — is exactly what failed to be queued. Nothing else in
    the system recovers such a row:

    * :func:`_redaction_pending` re-dispatches only from ``NULL``, so no later read
      retries it.
    * ``RedactionService._mark_redaction_stale`` short-circuits when the status is
      already ``pending``/``processing`` (its ``already_queued`` branch), so editing a
      segment does not retry it either.
    * ``redaction_reindex_all_task`` defaults to ``only_stale=True``, which filters on
      ``redaction_model_version``; a file that *was* scanned once keeps the current
      version and is therefore **skipped** by the admin backfill.

    A stranded row consequently withholds its transcript on all four surfaces that
    share ``export_policy.export_masking_is_pending`` — the transcript read, the
    single-file subtitle download, the bulk-export ZIP and the burned-in subtitle
    render — until a superuser re-runs the reindex with ``only_stale=false``. An
    unreachable broker is not exotic: a restarting Redis, a rebuild, or an
    out-of-process test harness all produce it.

    Args:
        db: Session owning ``db_file``. Committed here (twice at most).
        db_file: A completed file to scan. Whatever status it currently holds is
            captured and restored on a failed publish, so this is safe to call from
            ``NULL`` (never scanned) and from ``done`` (an explicit re-scan) alike.
        surface: Short name of the caller, for the operator-facing log only.

    Returns:
        :class:`RedactionScanDispatch`. ``queued`` is False when the status commit
        failed (nothing was claimed) or when the publish raised (the claim was
        withdrawn); in both cases no scan is running.
    """
    previous_status = db_file.redaction_status
    try:
        db_file.redaction_status = redaction_defaults.REDACTION_STATUS_PENDING  # type: ignore[assignment]
        db.commit()
    except Exception as e:  # noqa: BLE001
        db.rollback()
        logger.warning(
            "Could not mark file %s pending for redaction (%s): %s", db_file.id, surface, e
        )
        return RedactionScanDispatch(queued=False, task_id=None)

    try:
        from app.tasks.redaction_task import redaction_detect_task

        task = redaction_detect_task.delay(file_id=int(db_file.id), user_id=int(db_file.user_id))
    except Exception as e:  # noqa: BLE001
        logger.warning("Redaction dispatch failed for file %s (%s): %s", db_file.id, surface, e)
        _withdraw_redaction_claim(db, db_file, previous_status)
        return RedactionScanDispatch(queued=False, task_id=None)

    task_id = getattr(task, "id", None)
    return RedactionScanDispatch(queued=True, task_id=str(task_id) if task_id else None)


_LAZY_SCAN_FILE_STATUSES = (FileStatus.COMPLETED, FileStatus.ERROR)


def _lazy_dispatch_redaction(db: Session, db_file: MediaFile) -> None:
    """Kick off redaction detection for a completed file that never had it (legacy).

    Only legal from ``redaction_status IS NULL`` — see :func:`_redaction_pending`,
    which is its sole caller and gates on exactly that. The claim/publish/withdraw
    protocol itself is :func:`claim_and_dispatch_redaction_scan`, shared with the
    bulk "Redact" action; this wrapper exists to carry that precondition, which the
    shared helper deliberately does not impose (a re-scan is legal from any status).
    """
    claim_and_dispatch_redaction_scan(db, db_file, surface="lazy dispatch on transcript read")


def _redaction_pending(db: Session, cfg: Any, db_file: MediaFile) -> bool:
    """Whether transcript display should be withheld until redaction completes.

    Only applies when redaction is enabled for the viewer. 'done'/'failed' never block
    (failed = redaction couldn't run; don't trap the user). A file that never had
    detection (legacy, status None) is dispatched lazily and treated as pending.
    """
    from app.services.redaction.export_policy import export_masking_is_pending

    status = getattr(db_file, "redaction_status", None)
    # The status rule itself lives in `services/redaction/export_policy.py`, because the
    # bulk-export and burned-in-subtitle workers need the identical rule and cannot
    # import an API endpoint module. Only the lazy re-dispatch is local: it is a DB
    # write, which belongs on a read a user is waiting on and not in a download worker.
    if not export_masking_is_pending(cfg, status):
        return False
    # Only a file the pipeline has finished with is "legacy": COMPLETED, or ERROR (a failed
    # reprocess leaves its earlier transcript, which would otherwise be withheld forever
    # with no scan ever queued). Both are terminal for `update_media_file_from_task_status`,
    # so the scan's task bookkeeping cannot change their status. A PENDING/PROCESSING file
    # is still owned by the pipeline: that bookkeeping would take over its `active_task_id`
    # and mark it COMPLETED, so one read of a requeued file would finish it without a
    # transcript. It stays withheld (fail-closed) and is scanned once it completes.
    if status is None and db_file.status in _LAZY_SCAN_FILE_STATUSES:
        _lazy_dispatch_redaction(db, db_file)
    return True  # None (just dispatched) | pending | processing
