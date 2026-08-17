"""Periodic directory reconciliation — privileges, group membership, deprovisioning.

Before this existed there was **no deprovisioning at all**. Sync ran only at login
and only upward: it created and promoted accounts, and it could refuse a login, but
nothing ever set ``is_active = False`` except a manual admin lock. An account deleted
or disabled in Active Directory therefore kept a live OpenTranscribe row forever, and
because refresh tokens rotate on every use while ``sessions.py`` only checks the local
``is_active`` flag, an actively-used session survived the user's termination
indefinitely. Revoking sessions is the half that actually closes that hole; disabling
without revoking would leave the token rotating.

Four rules shape everything here:

1. **Fail closed on ambiguity, not on error.** "The directory says this user is gone"
   and "I could not ask the directory" are different answers. Only the first one acts;
   the second aborts the pass (:class:`~app.auth.ldap_auth.LdapDirectoryUnavailableError`).
2. **``super_admin`` and ``local`` accounts are never touched** — the first is the
   documented local-only break-glass account, the second has no upstream identity.
3. **Disable, never delete.** Deleting data because LDAP hiccupped is unrecoverable.
4. **Bounded and opt-in** — dry-run and ``enabled=False`` by default, plus a per-run cap.

Since ``v376`` the same pass also **reconciles what the account still has**, not
only whether it still exists: for every account the directory reports present, it
applies the configured ``group_mapping`` rows through
``services/idp_group_mapping_service.reconcile_user`` — the same implementation
login uses. That closes the other half of the drift. Login-time sync only ever
reaches accounts that log in, so a user moved out of ``CN=Legal-Team`` in AD kept
their in-app group (and, via ``admin_groups``, their admin role) until their next
sign-in — potentially forever for an account nobody uses but everybody shares
with. Group changes are not covered by ``max_disables_per_run``: that cap bounds
account deactivation, and a membership change is recoverable by re-adding the row.

Scope is LDAP only. OIDC/PKI have no "list users" primitive without provider-specific
admin APIs, so they need a different mechanism entirely — OIDC group mappings are
therefore applied **at login only**, which is the one genuine capability difference
between the two directory paths.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.constants import AUTH_TYPE_LDAP
from app.auth.ldap_auth import DIRECTORY_ABSENT
from app.auth.ldap_auth import DIRECTORY_DISABLED
from app.auth.ldap_auth import DIRECTORY_NOT_ENTITLED
from app.auth.ldap_auth import DIRECTORY_PRESENT
from app.auth.ldap_auth import LdapConfig
from app.auth.ldap_auth import LdapDirectoryUnavailableError
from app.auth.ldap_auth import LdapProbe
from app.auth.ldap_auth import ldap_directory_session
from app.auth.ldap_auth import probe_ldap_user
from app.auth.roles import ROLE_SUPER_ADMIN
from app.core import constants as C  # noqa: N812
from app.models.group import MAPPING_SOURCE_LDAP
from app.models.user import User
from app.services import system_settings_service as sss
from app.services.account_security_service import revoke_all_sessions
from app.services.idp_group_mapping_service import reconcile_user

logger = logging.getLogger(__name__)

# --- SystemSettings keys ------------------------------------------------------
KEY_ENABLED = "directory_sync.enabled"
KEY_SCHEDULE = "directory_sync.schedule"
KEY_DRY_RUN = "directory_sync.dry_run"
KEY_MAX_DISABLES = "directory_sync.max_disables_per_run"
KEY_LAST_RUN_AT = "directory_sync.last_run_at"
KEY_LAST_RESULT = "directory_sync.last_result"

#: Directory answers that justify deprovisioning, mapped to the reason we record.
ACTIONABLE_STATUSES = {
    DIRECTORY_ABSENT: "absent_from_directory",
    DIRECTORY_DISABLED: "disabled_in_directory",
    DIRECTORY_NOT_ENTITLED: "no_longer_in_required_groups",
}


@dataclass(frozen=True)
class SweepConfig:
    """The three knobs a single pass needs, already resolved."""

    dry_run: bool
    max_disables: int


@contextlib.contextmanager
def _session(db: Session | None) -> Iterator[Session]:
    """Yield the passed session, or a short-lived one closed after use."""
    if db is not None:
        yield db
        return
    from app.db.base import SessionLocal

    own = SessionLocal()
    try:
        yield own
    finally:
        own.close()


# =============================================================================
# Settings round-trip
# =============================================================================
def get_settings(db: Session | None = None) -> dict[str, Any]:
    """Return all directory-sync settings as a plain dict (coded defaults if unset)."""
    with _session(db) as s:
        vals = sss.get_settings_map(
            s,
            [
                KEY_ENABLED,
                KEY_SCHEDULE,
                KEY_DRY_RUN,
                KEY_MAX_DISABLES,
                KEY_LAST_RUN_AT,
                KEY_LAST_RESULT,
            ],
        )

        def _b(key: str, default: bool) -> bool:
            v = vals.get(key)
            return v.lower() in ("true", "1", "yes", "on") if v is not None else default

        def _i(key: str, default: int) -> int:
            v = vals.get(key)
            if v is None:
                return default
            try:
                return int(v)
            except (ValueError, TypeError):
                return default

        last_result: dict[str, Any] | None = None
        if vals.get(KEY_LAST_RESULT):
            try:
                last_result = json.loads(str(vals[KEY_LAST_RESULT]))
            except (ValueError, TypeError):
                last_result = None

        return {
            "enabled": _b(KEY_ENABLED, C.DEFAULT_DIRECTORY_SYNC_ENABLED),
            "schedule": vals.get(KEY_SCHEDULE) or C.DEFAULT_DIRECTORY_SYNC_SCHEDULE,
            "dry_run": _b(KEY_DRY_RUN, C.DEFAULT_DIRECTORY_SYNC_DRY_RUN),
            "max_disables_per_run": _i(
                KEY_MAX_DISABLES, C.DEFAULT_DIRECTORY_SYNC_MAX_DISABLES_PER_RUN
            ),
            "last_run_at": vals.get(KEY_LAST_RUN_AT),
            "last_result": last_result,
        }


def update_settings(
    db: Session,
    *,
    enabled: bool | None = None,
    schedule: str | None = None,
    dry_run: bool | None = None,
    max_disables_per_run: int | None = None,
) -> dict[str, Any]:
    """Persist any provided directory-sync settings; return the full current set.

    Mirrors ``backup_service.update_settings``'s only-provided-fields contract.
    """
    if enabled is not None:
        sss.set_setting(
            db, KEY_ENABLED, enabled, "Periodic LDAP reconciliation/deprovisioning master toggle"
        )
    if schedule is not None:
        from app.services.backup_service import is_valid_cron

        if not is_valid_cron(schedule):
            raise ValueError(f"Invalid cron schedule: {schedule!r}")
        sss.set_setting(
            db, KEY_SCHEDULE, schedule, "Directory reconciliation cron schedule (5-field, UTC)"
        )
    if dry_run is not None:
        sss.set_setting(
            db,
            KEY_DRY_RUN,
            dry_run,
            "Report what the sweep would disable without changing anything",
        )
    if max_disables_per_run is not None:
        if max_disables_per_run < 1:
            raise ValueError("max_disables_per_run must be at least 1")
        sss.set_setting(
            db,
            KEY_MAX_DISABLES,
            int(max_disables_per_run),
            "Per-run cap on accounts the sweep may disable",
        )
    return get_settings(db)


def update_settings_last_run(db: Session, when_iso: str) -> None:
    """Stamp the dispatch time so the next beat tick can't re-fire the same window."""
    sss.set_setting(db, KEY_LAST_RUN_AT, when_iso, "Last directory reconciliation run (UTC)")


def record_result(db: Session, result: dict[str, Any]) -> None:
    """Persist the last run's outcome for the admin UI."""
    sss.set_setting(db, KEY_LAST_RESULT, json.dumps(result), "Last directory reconciliation result")


# =============================================================================
# Candidate selection
# =============================================================================
def is_protected(user: User) -> bool:
    """Return True for accounts the sweep must never disable.

    Checked at the point of action, not only in the query, so a future change to
    the candidate SQL cannot quietly widen the blast radius.
    """
    if str(user.role) == ROLE_SUPER_ADMIN:
        return True
    return str(user.auth_type) != AUTH_TYPE_LDAP


def candidate_users(db: Session) -> list[User]:
    """Active LDAP accounts below super_admin, oldest first (stable cap ordering)."""
    return (
        db.query(User)
        .filter(
            User.auth_type == AUTH_TYPE_LDAP,
            User.is_active.is_(True),
            User.role != ROLE_SUPER_ADMIN,
        )
        .order_by(User.id)
        .all()
    )


def probe_users(db: Session, users: list[User]) -> Iterator[tuple[User, LdapProbe]]:
    """Yield ``(user, probe)`` for each candidate, one bind for the pass.

    The probe carries the account's groups and admin signal as well as its status,
    so reconciliation costs no extra directory round-trip.

    Raises:
        LdapDirectoryUnavailableError: The directory is unreachable, the service account
            cannot bind, or a search failed mid-pass. Callers must stop.
    """
    cfg = LdapConfig.from_db(db)
    if not cfg.enabled:
        raise LdapDirectoryUnavailableError("LDAP is not enabled; nothing can be reconciled")

    with ldap_directory_session(cfg) as conn:
        for user in users:
            yield user, probe_ldap_user(cfg, conn, str(user.ldap_uid or ""), str(user.email or ""))


# =============================================================================
# The sweep
# =============================================================================
def _disable_user(db: Session, user: User, reason: str) -> int:
    """Disable *user*, revoke every session, audit it. Returns sessions revoked."""
    user.is_active = False  # type: ignore[assignment]
    revoked = revoke_all_sessions(db, user, reason=f"directory_sync:{reason}")
    db.commit()

    audit_logger.log(
        event_type=AuditEventType.AUTH_ACCOUNT_DISABLED,
        outcome=AuditOutcome.SUCCESS,
        # No human actor: this is the periodic sweep. `user_id` is the ACTOR
        # (issue #443), so leaving the SUBJECT there made "actions performed by
        # this user" return the deactivation of that same user, by nobody.
        user_id=None,
        target_user_id=int(user.id),
        target_username=str(user.email),
        details={
            "actor": "directory_sync",
            "reason": reason,
            "ldap_uid": str(user.ldap_uid or ""),
            "sessions_revoked": revoked,
        },
    )
    logger.warning(
        "Directory sync disabled user %s (ldap_uid=%s): %s; %d session(s) revoked",
        user.email,
        user.ldap_uid,
        reason,
        revoked,
    )
    return revoked


def _reconcile(
    db: Session, user: User, probe: LdapProbe, *, dry_run: bool
) -> dict[str, Any] | None:
    """Apply group mappings and privilege for one still-present account.

    Returns the report entry when something changed (or would change under
    ``dry_run``), otherwise ``None`` — a steady-state pass over a thousand accounts
    should not produce a thousand no-op report lines.

    A reconciliation failure for one account must not abort the pass: the
    directory is fine, the accounts after this one still deserve to be checked,
    and the alternative is one bad ``group_mapping`` row silently stopping
    deprovisioning too.
    """
    try:
        result = reconcile_user(
            db,
            user,
            MAPPING_SOURCE_LDAP,
            list(probe.groups),
            legacy_admin=probe.is_admin,
            reason="directory_sync",
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001 - one bad account must not stop the sweep
        db.rollback()
        logger.error("Directory sync could not reconcile %s: %s", user.email, exc)
        return {"user_uuid": str(user.uuid), "email": str(user.email), "error": str(exc)}

    if not result.changed:
        return None
    return {"user_uuid": str(user.uuid), "email": str(user.email), **result.as_dict()}


def sweep_ldap(db: Session, cfg: SweepConfig) -> dict[str, Any]:
    """Run one reconciliation pass and return a report dict.

    Never raises for a directory problem: an unreachable directory produces
    ``status="directory_unavailable"`` with zero changes, because "I could not ask"
    is not evidence that anyone was offboarded.

    Args:
        db: Session owning the transaction.
        cfg: Resolved dry-run flag and per-run cap.

    Returns:
        A JSON-serializable report: counts, the per-account actions, and whether the
        cap or a directory failure cut the pass short.
    """
    started = datetime.now(UTC)
    candidates = candidate_users(db)
    actions: list[dict[str, Any]] = []
    reconciliations: list[dict[str, Any]] = []
    checked = 0
    disabled = 0
    capped = False
    status = "ok"
    error: str | None = None

    try:
        for user, probe in probe_users(db, candidates):
            checked += 1
            reason = ACTIONABLE_STATUSES.get(probe.status)
            if reason is None:
                if probe.status == DIRECTORY_PRESENT:
                    reconciled = _reconcile(db, user, probe, dry_run=cfg.dry_run)
                    if reconciled is not None:
                        reconciliations.append(reconciled)
                continue
            if is_protected(user):
                # Unreachable via candidate_users, but this is the guard that matters.
                logger.info("Directory sync skipping protected account %s", user.email)
                continue
            if disabled >= cfg.max_disables:
                capped = True
                logger.warning(
                    "Directory sync hit the per-run cap of %d; %s and any further "
                    "accounts are left active for the next pass",
                    cfg.max_disables,
                    user.email,
                )
                break

            entry: dict[str, Any] = {
                "user_uuid": str(user.uuid),
                "email": str(user.email),
                "ldap_uid": str(user.ldap_uid or ""),
                "reason": reason,
                "applied": not cfg.dry_run,
            }
            if cfg.dry_run:
                logger.info(
                    "Directory sync (dry-run) WOULD disable %s (ldap_uid=%s): %s",
                    user.email,
                    user.ldap_uid,
                    reason,
                )
            else:
                entry["sessions_revoked"] = _disable_user(db, user, reason)
            actions.append(entry)
            disabled += 1
    except LdapDirectoryUnavailableError as e:
        # Fail closed on ambiguity: keep whatever was positively determined before the
        # failure, act on nothing after it, and make the reason visible in the report.
        status = "directory_unavailable"
        error = str(e)
        logger.error("Directory sync aborted — directory could not be consulted: %s", e)

    return {
        "status": status,
        "error": error,
        "dry_run": cfg.dry_run,
        "candidates": len(candidates),
        "checked": checked,
        "disabled": 0 if cfg.dry_run else disabled,
        "would_disable": disabled if cfg.dry_run else 0,
        "capped": capped,
        "max_disables_per_run": cfg.max_disables,
        "actions": actions,
        "reconciled": len(reconciliations),
        "reconciliations": reconciliations,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
    }


def run_scheduled_sweep(
    db: Session | None = None, *, dry_run: bool | None = None
) -> dict[str, Any]:
    """Load DB settings and run one pass. The Celery task's entry point.

    Args:
        db: Optional session; a short-lived one is used when omitted.
        dry_run: Override the stored flag (admin "Preview" action). ``None`` uses
            the configured value — an override can only ever be passed explicitly,
            so the safe default is never bypassed by accident.
    """
    with _session(db) as s:
        settings = get_settings(s)
        if not settings["enabled"]:
            return {"status": "disabled"}

        cfg = SweepConfig(
            dry_run=settings["dry_run"] if dry_run is None else dry_run,
            max_disables=max(0, int(settings["max_disables_per_run"])),
        )
        result = sweep_ldap(s, cfg)
        with contextlib.suppress(Exception):
            record_result(s, result)
        return result
