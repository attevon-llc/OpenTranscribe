"""FedRAMP AC-10 concurrent-session ceiling — the periodic sweep half (issue #632).

``token_service.enforce_session_ceiling`` is now called from every session-minting
path (see its docstring), so a session count above the cap should be self-correcting
at the next login. This sweep is defence in depth for the cases that are not: a cap
LOWERED by an admin (existing sessions minted under the old, higher cap do not
retroactively shrink until someone logs in again), and a one-time backlog left over
from before this fix existed (login.py's old "evict exactly one, mint exactly one"
mechanism could never bring an above-cap count back down on its own).

Follows `account_lifecycle_service.run_inactivity_sweep`'s shape: a report dict,
`status: "disabled"` on a no-op configuration, one commit per subject (never one
transaction for the whole pass — a single bad row must not roll back every other
user's already-applied eviction), and a purpose-built audit event per subject with
a system actor (`user_id=None`, `target_user_id=<subject>` — issue #443's
actor/target convention: this is a scheduled sweep, nobody's login triggered it).
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.token_service import enforce_session_ceiling
from app.core.auth_settings import get_auth_settings
from app.core.constants import SESSION_CAP_SWEEP_MAX_PER_USER
from app.models.user import User

logger = logging.getLogger(__name__)


def run_session_cap_sweep(db: Session) -> dict:
    """Run one AC-10 concurrent-session-ceiling pass and return a report dict.

    Finds every user currently over the configured limit (a single grouped query,
    not one query per user) and enforces the ceiling for each of them in turn. A
    failure evicting one user's sessions must not undo another's — see the module
    docstring — so each subject is committed independently, and a per-subject
    failure is caught and counted rather than propagated.

    Args:
        db: Database session. **Must own its own transaction** — the flat,
            one-real-transaction session `db/session_utils.session_scope()` hands
            the Celery task. Do not pass a session enlisted in a caller's outer
            transaction (same contract as `account_lifecycle_service.
            run_inactivity_sweep`, which documents why in detail).

    Returns:
        A JSON-serializable report: `status`, and on `"ok"` also
        `users_over_limit`, `sessions_revoked`, `limit`.
    """
    limit = get_auth_settings(db).max_concurrent_sessions
    if limit <= 0:
        return {"status": "disabled", "reason": "unlimited"}

    offenders = db.execute(
        text(
            """
            SELECT user_id, count(*) AS active_count
              FROM refresh_token
             WHERE revoked_at IS NULL
               AND expires_at > now()
             GROUP BY user_id
            HAVING count(*) > :limit
            """
        ),
        {"limit": limit},
    ).fetchall()

    if not offenders:
        return {"status": "ok", "users_over_limit": 0, "sessions_revoked": 0, "limit": limit}

    total_revoked = 0
    users_touched = 0
    for user_id, active_count in offenders:
        try:
            revoked_jtis = enforce_session_ceiling(
                db, user_id, limit, batch_limit=SESSION_CAP_SWEEP_MAX_PER_USER
            )
            db.commit()
        except Exception:
            db.rollback()
            logger.exception(
                "Session cap sweep failed to enforce the ceiling for user_id=%s "
                "(had %d active sessions, limit=%d)",
                user_id,
                active_count,
                limit,
            )
            continue

        if not revoked_jtis:
            # The grouped query and the enforcement query are not the same
            # snapshot — a concurrent logout/rotation between them can already
            # have brought this user back under the limit. Not an error.
            continue

        users_touched += 1
        total_revoked += len(revoked_jtis)

        user_row = db.query(User.email).filter(User.id == user_id).first()
        audit_logger.log(
            event_type=AuditEventType.AUTH_SESSION_LIMIT_EXCEEDED,
            outcome=AuditOutcome.SUCCESS,
            # System actor: no human login triggered this, the periodic sweep did
            # (issue #443's actor/target convention — user_id is the ACTOR).
            user_id=None,
            target_user_id=user_id,
            target_username=str(user_row[0]) if user_row else None,
            details={
                "reason": "periodic_sweep",
                "policy": "ceiling",
                "max_concurrent_sessions": limit,
                "sessions_revoked": len(revoked_jtis),
            },
        )
        logger.warning(
            "Session cap sweep revoked %d session(s) for user_id=%s (had %d active, limit=%d)",
            len(revoked_jtis),
            user_id,
            active_count,
            limit,
        )

    return {
        "status": "ok",
        "users_over_limit": users_touched,
        "sessions_revoked": total_revoked,
        "limit": limit,
    }
