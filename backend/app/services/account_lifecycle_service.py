"""FedRAMP AC-2 account-inactivity expiration — the sweep half.

`_enforce_account_expiry` (``api/endpoints/auth/dependencies.py``) enforces a
DIFFERENT AC-2 mechanism: a fixed, admin-set `User.account_expires_at` date, checked
at request time. This module is the other one — a periodic sweep that deactivates
an account nobody has used in `settings.ACCOUNT_INACTIVE_DAYS` days, gated on
`settings.ACCOUNT_EXPIRATION_ENABLED` (both default: disabled). Distinct triggers,
same downstream effect (`is_active = False`, already enforced everywhere a login is
checked), and the same audit event type — `AUTH_ACCOUNT_EXPIRED` — with `details`
distinguishing which one fired.

Follows `directory_sync_service`'s shape for a no-human-actor deactivation: disable,
revoke sessions, audit with `user_id=None` (no actor) / `target_user_id` (the
subject) — see `_disable_user` there for the precedent this mirrors line-for-line.

Two safety rules:

1. **NULL `last_login_at` is never "inactive."** It means "never recorded a login,"
   not "infinitely idle" — see `models/CLAUDE.md`'s note on this exact column.
   Every real login path (local/OIDC/PKI/MFA-enrollment) stamps it, so a NULL row
   has never actually authenticated and has nothing to expire.
2. **Never deactivate the last active super_admin.** Auth configuration, role
   changes and the audit log are all super_admin-gated, so zeroing them out locks
   everyone out permanently with no recovery path short of editing the database by
   hand — the same reasoning `api/endpoints/users.py::_assert_not_last_super_admin`
   already applies to a manual role change, adapted here as a non-raising
   skip-and-log check (a scheduled sweep has no request to fail).
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from sqlalchemy.orm import Session

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.roles import ROLE_SUPER_ADMIN
from app.core.config import settings
from app.core.constants import ACCOUNT_INACTIVITY_MAX_DISABLES_PER_RUN
from app.models.user import User
from app.services.account_security_service import revoke_all_sessions

logger = logging.getLogger(__name__)


def _would_leave_no_super_admin(db: Session, candidate_ids: set[int]) -> bool:
    """True if deactivating every id in *candidate_ids* would zero active super_admins.

    One query against the whole candidate set rather than per-row, matching this
    codebase's "one query, not N" convention for bulk sweeps.
    """
    remaining = (
        db.query(User)
        .filter(
            User.role == ROLE_SUPER_ADMIN,
            User.id.notin_(candidate_ids),
            User.is_active.is_(True),
        )
        .count()
    )
    return remaining == 0


def _disable_inactive_user(db: Session, user: User, *, last_login_at: datetime) -> int:
    """Disable *user* for inactivity, revoke sessions, commit, audit.

    Mirrors `directory_sync_service._disable_user`'s shape exactly (disable ->
    revoke -> commit -> audit -> log), the established precedent for a
    no-human-actor deactivation in this codebase. Returns sessions revoked.
    """
    user.is_active = False  # type: ignore[assignment]
    revoked = revoke_all_sessions(db, user, reason="account_inactivity_sweep")
    db.commit()

    audit_logger.log(
        event_type=AuditEventType.AUTH_ACCOUNT_EXPIRED,
        outcome=AuditOutcome.SUCCESS,
        # No human actor: this is the periodic sweep. `user_id` is the ACTOR
        # (issue #443) — leaving the subject there would make "actions performed
        # by this user" return this same user's own deactivation, by nobody.
        user_id=None,
        target_user_id=int(user.id),
        target_username=str(user.email),
        details={
            "actor": "account_inactivity_sweep",
            "trigger": "inactivity",
            "last_login_at": last_login_at.isoformat(),
            "inactive_days_threshold": settings.ACCOUNT_INACTIVE_DAYS,
            "sessions_revoked": revoked,
        },
    )
    logger.warning(
        "Account inactivity sweep disabled user %s (last_login_at=%s, threshold=%d days); "
        "%d session(s) revoked",
        user.email,
        last_login_at.isoformat(),
        settings.ACCOUNT_INACTIVE_DAYS,
        revoked,
    )
    return revoked


def run_inactivity_sweep(db: Session) -> dict:
    """Run one AC-2 inactivity-expiration pass and return a report dict.

    A bad CANDIDATE row must not abort the batch, and doesn't — that per-row work is
    wrapped in try/except (matches `directory_sync_service`'s convention). This does NOT
    mean the function never raises: the candidate query, the super-admin protection
    check, and the report construction all run outside that per-row guard, and a DB
    error there propagates out through `session_scope` and fails the Celery task.

    **Transaction contract:** *db* must own its transaction — the flat, one-real-
    transaction session `db/session_utils.session_scope()` hands the Celery task. Each
    candidate is committed on its own (making that deactivation durable) and a failure
    is rolled back, discarding only the work since the last commit. Do NOT pass a
    session enlisted in some *caller's* outer transaction: `Session.rollback()` always
    unwinds the TOPMOST transaction, not the innermost unit of work, so one bad row
    would take the caller's earlier — already "committed" — work down with it. The
    `production_txn_session` fixture in
    `tests/unit/test_account_lifecycle_service.py` documents the measured shape of
    that failure.

    Returns:
        A JSON-serializable report: `status`, `candidates_checked`, `deactivated`,
        `skipped_super_admin` (protected, not touched), `errors`, `capped` (more
        genuine candidates existed than `ACCOUNT_INACTIVITY_MAX_DISABLES_PER_RUN`
        allowed this pass to touch).
    """
    if not settings.ACCOUNT_EXPIRATION_ENABLED:
        return {"status": "disabled", "reason": "not_enabled"}

    # `_int_env` applies no range check, so a misconfigured 0/negative value would
    # make `cutoff >= now` and turn every account with any recorded login into a
    # candidate on the very next tick. Fail safe (skip the pass, log loudly) rather
    # than crash the whole worker on a bad env var.
    if settings.ACCOUNT_INACTIVE_DAYS < 1:
        logger.error(
            "Account inactivity sweep: ACCOUNT_INACTIVE_DAYS=%d is not a valid "
            "threshold (must be >= 1) — refusing to run this pass",
            settings.ACCOUNT_INACTIVE_DAYS,
        )
        return {"status": "disabled", "reason": "invalid_inactive_days"}

    cutoff = datetime.now(UTC) - timedelta(days=settings.ACCOUNT_INACTIVE_DAYS)
    base_query = db.query(User).filter(
        User.is_active.is_(True),
        User.last_login_at.isnot(None),
        User.last_login_at < cutoff,
    )
    total_candidates = base_query.count()
    # Oldest last_login_at first: if a pass is capped, the accounts left for next
    # time are the ones closest to the threshold, not the most overdue.
    candidates = (
        base_query.order_by(User.last_login_at.asc())
        .limit(ACCOUNT_INACTIVITY_MAX_DISABLES_PER_RUN)
        .all()
    )
    capped = total_candidates > len(candidates)
    if capped:
        logger.warning(
            "Account inactivity sweep: %d candidates found, capped to %d this pass "
            "(ACCOUNT_INACTIVITY_MAX_DISABLES_PER_RUN) — the rest will be picked up "
            "by a later pass",
            total_candidates,
            len(candidates),
        )

    if not candidates:
        return {
            "status": "completed",
            "candidates_checked": 0,
            "deactivated": 0,
            "skipped_super_admin": 0,
            "errors": 0,
            "capped": False,
        }

    candidate_ids = {int(u.id) for u in candidates}
    super_admin_ids = {int(u.id) for u in candidates if str(u.role) == ROLE_SUPER_ADMIN}
    protect_super_admins = bool(super_admin_ids) and _would_leave_no_super_admin(db, candidate_ids)
    if protect_super_admins:
        logger.warning(
            "Account inactivity sweep: deactivating %d inactive super_admin(s) would "
            "leave the deployment with none — skipping them this pass",
            len(super_admin_ids),
        )

    deactivated = 0
    skipped_super_admin = 0
    errors = 0
    for user_id, user_email in [(int(u.id), str(u.email)) for u in candidates]:
        if protect_super_admins and user_id in super_admin_ids:
            skipped_super_admin += 1
            # For a FedRAMP control, "declined to apply to this privileged account"
            # belongs in the audit trail, not only in the worker log -- an assessor
            # needs to see the exemption, not just infer it from an absence.
            audit_logger.log(
                event_type=AuditEventType.AUTH_ACCOUNT_EXPIRED,
                outcome=AuditOutcome.SUCCESS,
                user_id=None,
                target_user_id=user_id,
                target_username=user_email,
                details={
                    "actor": "account_inactivity_sweep",
                    "trigger": "inactivity_skipped_super_admin",
                    "reason": "deactivating this account would leave no active super_admin",
                },
            )
            continue
        # Re-fetched fresh, not reused from `candidates`: an earlier iteration's
        # rollback (below) expires every object the session is tracking, so a
        # pre-rollback reference from the initial query is no longer trustworthy.
        user = db.query(User).filter(User.id == user_id).first()
        if user is None:
            continue
        # Re-check the invariant on the FRESH row, not the pre-loop snapshot: a
        # multi-minute sweep can straddle a real login or an admin reactivation
        # between the candidate query and this iteration. Disabling anyway would
        # both act on a no-longer-inactive account and record a stale
        # `last_login_at` in the audit trail as if it were still true.
        if not bool(user.is_active) or user.last_login_at is None or user.last_login_at >= cutoff:
            logger.info(
                "Account inactivity sweep: %s (id=%s) is no longer a candidate "
                "(reactivated or logged in during this sweep) — skipping",
                user_email,
                user_id,
            )
            continue
        try:
            _disable_inactive_user(db, user, last_login_at=user.last_login_at)
            deactivated += 1
        except Exception:
            db.rollback()
            # "Will be retried" is only true for a failure BEFORE `_disable_inactive_user`'s
            # own `db.commit()` — its audit-log call reads `user.id`/`user.email` after that
            # commit, so a failure there (e.g. an expired-instance refresh) leaves the account
            # already disabled; `db.rollback()` cannot undo a commit, and the disabled row is
            # then permanently excluded from the next pass's candidate query. Don't claim a
            # retry that may not happen — direct the operator to check instead.
            logger.exception(
                "Account inactivity sweep failed to disable user %s (id=%s) — the account "
                "may or may not have actually been disabled (failure could be before or "
                "after commit); verify its state manually",
                user_email,
                user_id,
            )
            errors += 1

    return {
        "status": "completed",
        "candidates_checked": len(candidates),
        "deactivated": deactivated,
        "skipped_super_admin": skipped_super_admin,
        "errors": errors,
        "capped": capped,
    }
