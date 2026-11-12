"""Removing an account with a grace period for its personal files (issue #1104, criterion OD-4).

An administrator's removal of an account no longer erases the personal (non-organization) files
at once. Organization content is handed to an administrator exactly as before
(``org_content_transfer``); the account is then deactivated and a ``user`` erasure is scheduled on
the existing ledger machinery (``scheduled_erasure_service``: same state machine, executor, audit
events and notices as #1105). Until it runs, an administrator can restore the account, which
cancels the erasure and switches the account back on, unless it was already off when it was
removed. Legal hold wins: a held file is refused at request time (the unchanged 409) and skipped
by the executor if a hold appears during the wait. Quarantine alone does not stop the erase, as
for an immediate deletion (``user_deletion_service.assert_no_held_files`` gives the reason); a
takedown that must keep its evidence places a legal hold as well.

The deactivation commits with the ledger entry, and the reactivation with its cancellation, so
neither can be left half done.

A grace of 0 days, or an account that belongs to no organization (every community-edition
account), keeps the old immediate erasure.
"""

from __future__ import annotations

import logging
import uuid as uuid_pkg
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.auth.token_service import token_service
from app.core.constants import DEFAULT_GDPR_REMOVED_MEMBER_GRACE_DAYS
from app.core.constants import GDPR_REMOVED_MEMBER_GRACE_DAYS_KEY
from app.core.constants import MAX_GDPR_SELF_ERASURE_GRACE_DAYS
from app.models.erasure import ErasureLedgerEntry
from app.models.user import User
from app.services import erasure_ledger_service as ledger
from app.services import scheduled_erasure_service as scheduled
from app.services import user_deletion_service
from app.services.system_settings_service import get_setting

logger = logging.getLogger(__name__)

#: Ledger actor kinds of an administrator's removal; the account holder's own request is
#: ``data_subject`` and is not managed here.
ADMIN_ACTOR_KINDS = ledger.ADMIN_ACTOR_KINDS

#: Ledger ``counters`` key set to 1 when the account was active at removal, so a restore gives
#: back exactly that. ``counters`` is the entry's only numeric field that can change after insert
#: and it holds nothing else until the erasure runs; a missing key means "leave it off".
RESTORE_REACTIVATES_KEY = "restore_reactivates_account"


def grace_days(db: Session) -> int:
    """The configured grace period, clamped to ``0..MAX_GDPR_SELF_ERASURE_GRACE_DAYS``."""
    raw = get_setting(db, GDPR_REMOVED_MEMBER_GRACE_DAYS_KEY, None)
    try:
        value = int(raw) if raw is not None else DEFAULT_GDPR_REMOVED_MEMBER_GRACE_DAYS
    except (TypeError, ValueError):
        value = DEFAULT_GDPR_REMOVED_MEMBER_GRACE_DAYS
    return max(0, min(value, MAX_GDPR_SELF_ERASURE_GRACE_DAYS))


@dataclass
class RemovalResult:
    """What a removal did: scheduled (``entry`` set) or erased now (``residual_errors``)."""

    entry: ErasureLedgerEntry | None = None
    residual_errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def scheduled(self) -> bool:
        return self.entry is not None


def _is_org_member(db: Session, user_id: int) -> bool:
    """Whether the account belongs to any organization. The community edition has no
    memberships, so there the removal stays the immediate deletion it always was."""
    from app.models.organization import OrganizationMembership

    return (
        db.query(OrganizationMembership.user_id)
        .filter(OrganizationMembership.user_id == user_id)
        .first()
        is not None
    )


def remove_account(
    db: Session, user: User, actor: User, *, client: dict[str, Any] | None = None
) -> RemovalResult:
    """Remove ``user``: hand over organization content, then schedule (or run) the personal erase.

    Raises:
        ConflictError: 409 for a legally held file or an organization with no other administrator.
        scheduled_erasure_service.ScheduledErasureError: the erasure could not be recorded
            (nothing was deactivated).
    """
    days = grace_days(db)
    if days == 0 or not _is_org_member(db, int(user.id)):
        return RemovalResult(residual_errors=user_deletion_service.delete_user_account(db, user))

    user_id = int(user.id)
    user_deletion_service.assert_no_held_files(db, user_id)
    user_deletion_service.hand_over_org_content(db, user_id)

    kind = "super_admin" if str(actor.role) == "super_admin" else "org_admin"
    was_active = bool(user.is_active)
    # Rides the ledger entry's own commit (``record_request``): a scheduled erase never sits on an
    # account that can still sign in, and a ledger failure rolls the deactivation back with it.
    user.is_active = False
    try:
        entry = scheduled.schedule(
            db,
            subject_type="user",
            actor=actor,
            actor_kind=kind,
            delay=timedelta(days=days),
            subject_user=user,
            client=client,
        )
    except scheduled.ScheduledErasureError as error:
        if error.code != "request_exists":
            raise
        open_entry = scheduled.open_request(db, "user", user_id, None)
        if open_entry is None:
            raise
        entry = open_entry
    else:
        if was_active:
            entry.counters = {RESTORE_REACTIVATES_KEY: 1}
    db.commit()
    token_service.stamp_user_revocation_epoch(str(user.uuid))
    return RemovalResult(entry=entry)


def open_removals(db: Session) -> list[ErasureLedgerEntry]:
    """Administrator-initiated account removals waiting out their grace, soonest first."""
    return (
        db.query(ErasureLedgerEntry)
        .filter(
            ErasureLedgerEntry.subject_type == "user",
            ErasureLedgerEntry.status == "deferred",
            ErasureLedgerEntry.deferred_reason == "grace_period",
            ErasureLedgerEntry.actor_kind.in_(ADMIN_ACTOR_KINDS),
        )
        .order_by(ErasureLedgerEntry.scheduled_for.asc(), ErasureLedgerEntry.id.asc())
        .all()
    )


def restore_account(
    db: Session,
    entry: ErasureLedgerEntry,
    actor: User,
    *,
    now: datetime | None = None,
    client: dict[str, Any] | None = None,
) -> ErasureLedgerEntry:
    """Cancel the erasure and reactivate the account if it was active when removed, in the
    cancellation's commit. Organization content already handed over stays with the organization.

    Raises:
        scheduled_erasure_service.ScheduledErasureError: ``already_executing`` once the grace is
            over, ``not_cancellable`` for an entry that is not an administrator's removal.
    """
    if entry.subject_type != "user" or entry.actor_kind not in ADMIN_ACTOR_KINDS:
        raise scheduled.ScheduledErasureError(
            "not_cancellable", "This request cannot be restored here.", entry=entry
        )
    user_id = entry.subject_user_id
    user = db.get(User, user_id) if user_id is not None else None
    if user is not None and (entry.counters or {}).get(RESTORE_REACTIVATES_KEY) == 1:
        user.is_active = True
    try:
        return scheduled.cancel(db, entry, actor=actor, now=now, client=client)
    except scheduled.ScheduledErasureError:
        db.rollback()
        raise


def removal_display_names(db: Session, entries: list[ErasureLedgerEntry]) -> dict[int, str]:
    """Display label (name, else email) of each removed account, by user id."""
    ids = {int(e.subject_user_id) for e in entries if e.subject_user_id is not None}
    if not ids:
        return {}
    rows = db.query(User.id, User.full_name, User.email).filter(User.id.in_(ids))
    return {int(r.id): str(r.full_name or r.email) for r in rows}


def find_removal(db: Session, ledger_uuid: uuid_pkg.UUID) -> ErasureLedgerEntry | None:
    """An administrator's account removal by ledger uuid (open or not), else None."""
    return (
        db.query(ErasureLedgerEntry)
        .filter(
            ErasureLedgerEntry.uuid == ledger_uuid,
            ErasureLedgerEntry.subject_type == "user",
            ErasureLedgerEntry.actor_kind.in_(ADMIN_ACTOR_KINDS),
        )
        .first()
    )
