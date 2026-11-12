"""``/admin/removed-members``: accounts an administrator removed, waiting out their grace (#1104).

* ``GET  /admin/removed-members``: the open removals, soonest first.
* ``POST /admin/removed-members/{ledger_uuid}/restore``: cancel the erasure and reactivate the
  account (409 ``already_executing`` once the grace is over). A restore is a write to the
  account, so it follows the rule of every other one (``users_admin_edit``): only a super_admin
  restores a super_admin, or undoes a super_admin's removal (403).

The scheduling itself is ``DELETE /admin/users/{uuid}`` / ``DELETE /users/{uuid}``
(:mod:`app.services.removed_member_service`); the grace length is ``gdpr.removed_member_grace_days``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_admin_user
from app.api.endpoints.auth.lifecycle_gates import _get_client_info
from app.api.endpoints.users_admin_edit import _enforce_update_user_privilege_boundaries
from app.auth.refusal_audit import audit_user_update_refusal
from app.auth.roles import ROLE_SUPER_ADMIN
from app.db.base import get_db
from app.middleware.audit import get_request_context
from app.models.erasure import ErasureLedgerEntry
from app.models.user import User
from app.services import removed_member_service as removal
from app.services import scheduled_erasure_service as scheduled
from app.services.support_access_service import coded_error

router = APIRouter()


class RemovedMember(BaseModel):
    """One removal waiting out its grace period. Ids, times and the display label only."""

    ledger_uuid: UUID
    subject_user_uuid: UUID | None
    display_name: str | None
    requested_at: datetime
    scheduled_for: datetime | None
    status: Literal["scheduled", "cancelled"]


def _item(entry: ErasureLedgerEntry, name: str | None) -> RemovedMember:
    return RemovedMember(
        ledger_uuid=entry.uuid,
        subject_user_uuid=entry.subject_user_uuid,
        display_name=name,
        requested_at=entry.requested_at,
        scheduled_for=entry.scheduled_for,
        status="cancelled" if entry.status == "cancelled" else "scheduled",
    )


@router.get("", response_model=list[RemovedMember])
def list_removed_members(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> list[RemovedMember]:
    """Accounts removed by an administrator whose personal files are not yet erased."""
    entries = removal.open_removals(db)
    names = removal.removal_display_names(db, entries)
    return [
        _item(e, names.get(int(e.subject_user_id)) if e.subject_user_id is not None else None)
        for e in entries
    ]


def _assert_may_restore(
    request: Request, db: Session, entry: ErasureLedgerEntry, current_user: User
) -> None:
    """403 unless ``current_user`` may write to the removed account and undo its removal.

    The account half is ``users_admin_edit``'s own boundary, reused rather than restated. A
    removal a super_admin made is theirs to undo as well: otherwise any admin could bring back an
    account (an admin's, say) that the platform owner took away.
    """
    client_ip, user_agent = _get_client_info(request)
    subject = db.get(User, entry.subject_user_id) if entry.subject_user_id is not None else None
    if subject is not None:
        _enforce_update_user_privilege_boundaries(subject, current_user, {}, client_ip, user_agent)
    if entry.actor_kind == "super_admin" and current_user.role != ROLE_SUPER_ADMIN:
        if subject is not None:
            audit_user_update_refusal(
                "super_admin_removal_denied", subject, current_user, client_ip, user_agent
            )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a super_admin can restore an account a super_admin removed",
        )


@router.post("/{ledger_uuid}/restore", response_model=RemovedMember)
def restore_removed_member(
    request: Request,
    ledger_uuid: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> RemovedMember:
    """Cancel the pending erasure and reactivate the account (403 for a super_admin's account or
    removal unless the caller is one, 409 once the grace is over)."""
    entry = removal.find_removal(db, ledger_uuid)
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Removal not found")
    _assert_may_restore(request, db, entry, current_user)
    try:
        entry = removal.restore_account(
            db, entry, current_user, client=get_request_context(request)
        )
    except scheduled.ScheduledErasureError as error:
        raise coded_error(status.HTTP_409_CONFLICT, error.code, error.message) from error
    return _item(entry, None)
