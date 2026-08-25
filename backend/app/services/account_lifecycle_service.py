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

    Never raises — a bad row must not abort the batch (matches
    `directory_sync_service`'s per-row try/except convention).

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
        `skipped_super_admin` (protected, not touched), `errors`.
    """
    if not settings.ACCOUNT_EXPIRATION_ENABLED:
        return {"status": "disabled", "reason": "not_enabled"}

    cutoff = datetime.now(UTC) - timedelta(days=settings.ACCOUNT_INACTIVE_DAYS)
    candidates = (
        db.query(User)
        .filter(
            User.is_active.is_(True),
            User.last_login_at.isnot(None),
            User.last_login_at < cutoff,
        )
        .all()
    )

    if not candidates:
        return {
            "status": "completed",
            "candidates_checked": 0,
            "deactivated": 0,
            "skipped_super_admin": 0,
            "errors": 0,
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
    for user_id, last_login_at, user_email in [
        # `last_login_at` is `datetime | None` on the model, but the candidates
        # query above already filters `isnot(None)` — the guard makes that
        # invariant visible to mypy instead of a `type: ignore` at the call site.
        (int(u.id), u.last_login_at, str(u.email))
        for u in candidates
        if u.last_login_at is not None
    ]:
        if protect_super_admins and user_id in super_admin_ids:
            skipped_super_admin += 1
            continue
        # Re-fetched fresh, not reused from `candidates`: an earlier iteration's
        # rollback (below) expires every object the session is tracking, so a
        # pre-rollback reference from the initial query is no longer trustworthy.
        user = db.query(User).filter(User.id == user_id).first()
        if user is None:
            continue
        try:
            _disable_inactive_user(db, user, last_login_at=last_login_at)
            deactivated += 1
        except Exception:
            db.rollback()
            logger.exception(
                "Account inactivity sweep failed to disable user %s (id=%s) — "
                "skipping, will be retried next pass",
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
    }
