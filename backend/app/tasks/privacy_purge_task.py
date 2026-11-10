"""Privacy purge task (issue #1280): delete the voiceprints / inferred gender a switch only hides.

Dispatched by the three purge routes (organization admin, super_admin, the person themselves) after
they have checked the confirmation word and that the switch is off. The deletion itself is
``services/privacy/purge.py``; this task is the Celery shell around it.

Two passes, one task id. Pass 1 deletes. Pass 2 runs ``SECOND_PASS_DELAY_SECONDS`` later and
repeats the same idempotent deletion, to catch a voiceprint written by something that decided
before the switch flipped (the OpenSearch write backstop trusts a cached answer for up to
``PRIVACY_WRITE_BACKSTOP_TTL_SECONDS``). Pass 2 recomputes its targets from Postgres, which no
longer has the clusters pass 1 deleted, so the ids of their centroid documents are kept in the
purge's ``privacy_purge_pending`` row, written in the same commit that deletes the cluster rows
(``services/privacy/purge_pending.py``, v461). The task's row (``task_type="privacy_purge"``)
stays ``in_progress`` until pass 2 ends.

A worker that dies after pass 1's relational commit leaves that row behind. ``privacy.purge_reverify``
(beat) deletes and measures its documents once it is older than
``purge_pending.REVERIFY_GRACE_SECONDS``, audits the result, and fails a purge whose final pass
never ended with ``reason: interrupted``: the purge completes its centroid work or reports
incomplete, never done. That row, not a blind replay, is this task's recovery from worker loss.
One re-verify runs at a time (the ``privacy_purge_reverify`` lease, fail closed): the beat tick and
an administrator's on-demand run would otherwise both take the same rows, deleting and auditing
each purge twice and failing it as interrupted twice.

Because a purge is irreversible and the layer can be switched back on between the request and the
run, the task re-checks that the targets are still off before touching anything and aborts if not
(``reason: policy_enabled``). Every pass writes an audit event: the actor, the scope, the
targets, the exact counts removed per store, and the residual the pass measured afterwards. A
non-zero residual after the final pass is a FAILURE (``reason: residual``), never a success.
"""

from __future__ import annotations

import logging
from typing import Any

from app.auth.audit_events import AuditOutcome
from app.core.celery import celery_app
from app.core.queues import CPUPriority
from app.core.queues import UtilityPriority
from app.db.base import SessionLocal
from app.db.session_utils import session_scope
from app.models.user import User
from app.services.privacy import purge
from app.services.privacy import purge_pending
from app.services.privacy.measure import GroupedCounts
from app.services.privacy.measure import merge_grouped
from app.services.privacy.purge_file_guard import PROTECTION_COUNT_KEYS
from app.services.privacy.purge_file_guard import guard_files_scope
from app.utils.task_lock import task_lock_manager
from app.utils.task_utils import TASK_STATUS_COMPLETED
from app.utils.task_utils import TASK_STATUS_FAILED
from app.utils.task_utils import TASK_STATUS_IN_PROGRESS
from app.utils.task_utils import update_task_status

logger = logging.getLogger(__name__)

TASK_NAME = "privacy.purge_voiceprints"
REVERIFY_TASK_NAME = "privacy.purge_reverify"
TASK_TYPE = "privacy_purge"
#: The lease that keeps re-verify runs from overlapping (``utils/task_lock``; fails closed: an
#: outstanding row simply waits for the next tick).
REVERIFY_LOCK_KEY = "privacy_purge_reverify"
#: Longest the lease is renewed for: one run handles ``purge_pending.REVERIFY_BATCH`` rows.
REVERIFY_LOCK_TIMEOUT_SECONDS = 1800


def _set_status(task_id: str, status: str, *, completed: bool = False) -> None:
    """Move the task's row; never raises (a missing row must not undo a purge that ran)."""
    try:
        with session_scope() as db:
            update_task_status(db, task_id, status, completed=completed)
    except Exception:
        logger.exception("privacy purge %s: could not update the task row", task_id)


def _end_without_a_pass(
    task_id: str, actor_user_id: int | None, wanted: list[str], *, final: bool
) -> None:
    """Record a pass skipped because every file is now held or quarantined.

    After pass 1 (``final``) the record keeps what pass 1 removed, since that removal happened;
    an empty record would tell the requester nothing was purged. The pending row is settled as
    the final pass would: dropped when it owes no centroid document, else kept with exactly
    those ids for the re-verify to delete (their cluster rows are already gone).
    """
    previous = purge.read_record(task_id) if final else None
    counts = previous.get("counts") if previous else None
    purge.write_record(
        task_id,
        actor_user_id,
        counts if isinstance(counts, dict) else {},
        targets=wanted,
        reason="files_protected",
    )
    if final and purge.VOICEPRINTS in wanted:
        with session_scope() as db:
            owed = purge_pending.cluster_uuids(db, task_id)
        purge_pending.settle(SessionLocal, task_id, owed, final=True)


@celery_app.task(bind=True, name=TASK_NAME, priority=CPUPriority.USER_TRIGGERED)
def privacy_purge_task(
    self: Any,
    scope: dict[str, Any],
    targets: list[str],
    actor_user_id: int | None,
    phase: int = 1,
    carried_cluster_uuids: list[str] | None = None,
    purge_reason: str | None = None,
) -> dict[str, Any]:
    """Purge ``targets`` ("voiceprints", "inferred_gender") for ``scope``. Both passes.

    Args:
        scope: ``PurgeScope.to_payload()``. Built by the route from the request context, so it
            can never name a tenant the caller does not administer.
        targets: Subset of ``purge.PURGE_TARGETS``.
        actor_user_id: Who asked. Only the id travels through the broker; the audit username is
            looked up here. ``None`` for a scheduled destruction (no person asked).
        phase: 1 for the first pass; the task schedules phase 2 itself.
        carried_cluster_uuids: Centroid ids handed over in the message by a pass 1 queued before
            v461; a current pass 1 leaves them in ``privacy_purge_pending`` instead.
        purge_reason: ``purge.RETENTION`` for a destruction a retention schedule decided (the
            ``files`` scope): allowed whatever the privacy switches say, and recorded in the
            audit event as ``purge_reason``. ``None`` for a privacy purge.

    Returns:
        ``{"status": "completed" | "aborted" | "failed", ...}`` with this pass's counts.
    """
    task_id = str(self.request.id)
    purge_scope = purge.PurgeScope.from_payload(scope)
    wanted = [t for t in dict.fromkeys(targets) if t in purge.PURGE_TARGETS]

    with session_scope() as db:
        blocked = purge.targets_still_enabled(db, purge_scope, wanted, purge_reason=purge_reason)
        # Re-checked here, not only at request time: a hold can be placed while the task waits.
        guarded_scope = guard_files_scope(db, purge_scope)
        actor = db.get(User, actor_user_id) if actor_user_id is not None else None
        username = str(actor.email) if actor is not None else None

    def audit(
        phase_name: str,
        outcome: AuditOutcome,
        outcomes: dict[str, purge.TargetOutcome] | None = None,
        reason: str | None = None,
    ) -> None:
        purge.audit_purge(
            scope=purge_scope,
            targets=wanted,
            phase=phase_name,
            actor_user_id=actor_user_id,
            actor_username=username,
            outcome=outcome,
            task_id=task_id,
            outcomes=outcomes,
            reason=reason,
            purge_reason=purge_reason,
        )

    if guarded_scope is None and wanted and not blocked:
        logger.warning("privacy purge %s aborted: every file is held or quarantined", task_id)
        audit(f"pass_{phase}_aborted", AuditOutcome.FAILURE, reason="files_protected")
        _end_without_a_pass(task_id, actor_user_id, wanted, final=phase > 1)
        _set_status(task_id, TASK_STATUS_FAILED, completed=True)
        return {"status": "aborted", "reason": "files_protected"}
    if guarded_scope is not None:
        purge_scope = guarded_scope

    if not wanted or blocked:
        reason = "no_targets" if not wanted else "policy_enabled"
        logger.warning("privacy purge %s aborted: %s %s", task_id, reason, blocked)
        audit(f"pass_{phase}_aborted", AuditOutcome.FAILURE, reason=reason)
        _set_status(task_id, TASK_STATUS_FAILED, completed=True)
        return {"status": "aborted", "reason": reason, "targets_still_enabled": blocked}

    _set_status(task_id, TASK_STATUS_IN_PROGRESS)
    try:
        result = purge.run_purge_pass(
            purge_scope,
            wanted,
            session_factory=SessionLocal,
            carried_cluster_uuids=carried_cluster_uuids or (),
            pending_task_id=task_id,
        )
    except Exception:
        logger.exception("privacy purge %s failed in pass %s", task_id, phase)
        audit(f"pass_{phase}_failed", AuditOutcome.FAILURE, reason="error")
        _set_status(task_id, TASK_STATUS_FAILED, completed=True)
        return {"status": "failed", "reason": "error"}

    final = phase > 1
    if purge.VOICEPRINTS in wanted:
        purge_pending.settle(SessionLocal, task_id, result.unfinished_cluster_uuids, final=final)
    counts = result.counts()
    if final:
        # The second pass sees the same protected files; the total must not count them twice.
        for key in PROTECTION_COUNT_KEYS:
            counts[purge.POSTGRES].pop(key, None)
    residual = result.residual()
    previous = purge.read_record(task_id) if final else None
    total: GroupedCounts = {purge.POSTGRES: {}, purge.OPENSEARCH: {}}
    if previous and isinstance(previous.get("counts"), dict):
        merge_grouped(total, previous["counts"])
    merge_grouped(total, counts)
    purge.write_record(
        task_id,
        actor_user_id,
        total,
        targets=wanted,
        residual=residual.counts,
        residual_ok=residual.ok,
        residual_unverified=sorted(set(residual.unverified)),
    )

    # A residual after the final pass is a purge that did not do what it was asked: FAILURE,
    # never a success with a footnote. After pass 1 it is recorded and pass 2 gets to finish it.
    if not residual.ok:
        outcome = AuditOutcome.FAILURE if final else AuditOutcome.PARTIAL
        outcome_reason: str | None = "residual"
    elif result.errors:
        outcome, outcome_reason = AuditOutcome.PARTIAL, "store_errors"
    else:
        outcome, outcome_reason = AuditOutcome.SUCCESS, None
    audit(f"pass_{phase}_completed", outcome, outcomes=result.outcomes, reason=outcome_reason)

    if not final:
        try:
            self.apply_async(
                kwargs={
                    "scope": scope,
                    "targets": wanted,
                    "actor_user_id": actor_user_id,
                    "phase": 2,
                    "purge_reason": purge_reason,
                },
                task_id=task_id,
                countdown=purge.SECOND_PASS_DELAY_SECONDS,
            )
        except Exception:
            # The first pass stands; say so rather than leave the row in progress for ever.
            logger.exception("privacy purge %s: could not schedule the second pass", task_id)
            audit("pass_2_not_scheduled", AuditOutcome.PARTIAL, reason="dispatch_failed")
            _set_status(task_id, TASK_STATUS_FAILED, completed=True)
            return {"status": "completed", "counts": counts, "second_pass": "not_scheduled"}
        return {"status": "completed", "counts": counts, "second_pass": "scheduled"}

    if not residual.ok:
        _set_status(task_id, TASK_STATUS_FAILED, completed=True)
        return {
            "status": "failed",
            "reason": "residual",
            "counts": counts,
            "residual": residual.counts,
            "residual_unverified": sorted(set(residual.unverified)),
        }
    _set_status(
        task_id, TASK_STATUS_FAILED if result.errors else TASK_STATUS_COMPLETED, completed=True
    )
    return {"status": "completed", "counts": counts}


@celery_app.task(name=REVERIFY_TASK_NAME, priority=UtilityPriority.ROUTINE)
def privacy_purge_reverify_task(grace_seconds: float | None = None) -> dict[str, int]:
    """Finish or report the centroid work of purges a dead worker left (``purge_pending.reverify``).

    Args:
        grace_seconds: Override of ``purge_pending.REVERIFY_GRACE_SECONDS`` (the beat passes none).

    Returns:
        The ``purge_pending.reverify`` counts plus ``skipped`` (1 when another run held the
        lease, or Redis could not grant it, and nothing was done).
    """
    with task_lock_manager.acquire_lock(
        REVERIFY_LOCK_KEY, timeout=REVERIFY_LOCK_TIMEOUT_SECONDS
    ) as acquired:
        if not acquired:
            logger.info("privacy purge re-verify: another run holds the lease; skipping")
            return {"rows": 0, "finished": 0, "remaining": 0, "interrupted": 0, "skipped": 1}
        summary = purge_pending.reverify(
            SessionLocal,
            grace_seconds=purge_pending.REVERIFY_GRACE_SECONDS
            if grace_seconds is None
            else grace_seconds,
        )
    if summary["rows"]:
        logger.info("privacy purge re-verify: %s", summary)
    return {**summary, "skipped": 0}
