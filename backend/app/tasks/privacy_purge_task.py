"""Privacy purge task (issue #1280): delete the voiceprints / inferred gender a switch only hides.

Dispatched by the three purge routes (organization admin, super_admin, the person themselves) after
they have checked the confirmation word and that the switch is off. The deletion itself is
``services/privacy/purge.py``; this task is the Celery shell around it.

Two passes, one task id. Pass 1 deletes. Pass 2 runs ``SECOND_PASS_DELAY_SECONDS`` later and
repeats the same idempotent deletion, to catch a voiceprint written by something that decided
before the switch flipped (the OpenSearch write backstop trusts a cached answer for up to
``PRIVACY_WRITE_BACKSTOP_TTL_SECONDS``). Pass 2 recomputes its targets from Postgres, which no
longer has the clusters pass 1 deleted, so pass 1 hands it the ids of any cluster centroid
document it could not confirm gone (``carried_cluster_uuids``). The task's row (``task_type="privacy_purge"``) stays
``in_progress`` until pass 2 ends.

Because a purge is irreversible and the layer can be switched back on between the request and the
run, the task re-checks that the targets are still off before touching anything and aborts if not
(``reason: policy_enabled``). Every pass writes an audit event: the actor, the scope, the
targets, the exact counts removed per store, and the residual the pass measured afterwards. A
non-zero residual after the final pass is a FAILURE (``reason: residual``), never a success.
"""

from __future__ import annotations

import logging
from typing import Any

from app.auth.audit import AuditOutcome
from app.core.celery import celery_app
from app.core.constants import CPUPriority
from app.db.base import SessionLocal
from app.db.session_utils import session_scope
from app.models.user import User
from app.services.privacy import purge
from app.utils.task_utils import TASK_STATUS_COMPLETED
from app.utils.task_utils import TASK_STATUS_FAILED
from app.utils.task_utils import TASK_STATUS_IN_PROGRESS
from app.utils.task_utils import update_task_status

logger = logging.getLogger(__name__)

TASK_NAME = "privacy.purge_voiceprints"
TASK_TYPE = "privacy_purge"


def _set_status(task_id: str, status: str, *, completed: bool = False) -> None:
    """Move the task's row; never raises (a missing row must not undo a purge that ran)."""
    try:
        with session_scope() as db:
            update_task_status(db, task_id, status, completed=completed)
    except Exception:
        logger.exception("privacy purge %s: could not update the task row", task_id)


@celery_app.task(bind=True, name=TASK_NAME, priority=CPUPriority.USER_TRIGGERED)
def privacy_purge_task(
    self: Any,
    scope: dict[str, Any],
    targets: list[str],
    actor_user_id: int,
    phase: int = 1,
    carried_cluster_uuids: list[str] | None = None,
) -> dict[str, Any]:
    """Purge ``targets`` ("voiceprints", "inferred_gender") for ``scope``. Both passes.

    Args:
        scope: ``PurgeScope.to_payload()``. Built by the route from the request context, so it
            can never name a tenant the caller does not administer.
        targets: Subset of ``purge.PURGE_TARGETS``.
        actor_user_id: Who asked. Only the id travels through the broker; the audit username is
            looked up here.
        phase: 1 for the first pass; the task schedules phase 2 itself.
        carried_cluster_uuids: Cluster centroid documents the previous pass could not confirm
            deleted. Their Postgres rows are gone, so only the previous pass knows the ids.

    Returns:
        ``{"status": "completed" | "aborted" | "failed", ...}`` with this pass's counts.
    """
    task_id = str(self.request.id)
    purge_scope = purge.PurgeScope.from_payload(scope)
    wanted = [t for t in dict.fromkeys(targets) if t in purge.PURGE_TARGETS]

    with session_scope() as db:
        blocked = purge.targets_still_enabled(db, purge_scope, wanted)
        actor = db.get(User, actor_user_id)
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
        )

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
        )
    except Exception:
        logger.exception("privacy purge %s failed in pass %s", task_id, phase)
        audit(f"pass_{phase}_failed", AuditOutcome.FAILURE, reason="error")
        _set_status(task_id, TASK_STATUS_FAILED, completed=True)
        return {"status": "failed", "reason": "error"}

    counts = result.counts()
    residual = result.residual()
    previous = purge.read_record(task_id) if phase > 1 else None
    total: purge.GroupedCounts = {purge.POSTGRES: {}, purge.OPENSEARCH: {}}
    if previous and isinstance(previous.get("counts"), dict):
        purge.merge_grouped(total, previous["counts"])
    purge.merge_grouped(total, counts)
    purge.write_record(
        task_id,
        actor_user_id,
        total,
        targets=wanted,
        residual=residual.counts,
        residual_ok=residual.ok,
        residual_unverified=sorted(set(residual.unverified)),
    )

    final = phase > 1
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
                    "carried_cluster_uuids": result.unfinished_cluster_uuids,
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
