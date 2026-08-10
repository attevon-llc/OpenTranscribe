"""Admin API for the account-approval queue (``v379``) — **admin** tier.

Tier rationale, per the rule ``tests/unit/test_route_privilege_tiers.py`` enforces:
deciding who gets an account is managing users, which is ``admin``. Only
*deployment configuration* is ``super_admin`` — and the switch that turns this
feature on (``require_account_approval``) is exactly that, so it lives behind
``/admin/auth-config`` where the rest of the auth configuration does. The two tiers
are the two halves of the control: a super_admin decides whether there is a queue, an
admin works it.

Surface:

- ``GET  /admin/user-approvals``                    the pending queue
- ``POST /admin/user-approvals/{user_uuid}/approve``
- ``POST /admin/user-approvals/{user_uuid}/reject``

Rejection **never deletes the row**. The audit trail has to survive the decision,
and so does the email address — release it and the same person signs up again
looking new, which turns a refusal into a speed bump.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_admin_user
from app.api.endpoints.auth.dependencies import _get_client_info
from app.auth.approval import APPROVAL_APPROVED
from app.auth.approval import APPROVAL_PENDING
from app.auth.approval import APPROVAL_REJECTED
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.db.base import get_db
from app.models.user import User
from app.schemas.approval import ApprovalDecisionRequest
from app.schemas.approval import ApprovalDecisionResponse
from app.schemas.approval import PendingAccount
from app.services.account_security_service import revoke_all_sessions
from app.utils.uuid_helpers import get_user_by_uuid

logger = logging.getLogger(__name__)

router = APIRouter()


def _audit_decision(
    request: Request,
    actor: User,
    target: User,
    decision: str,
    reason: str | None,
) -> None:
    """Record an approval decision.

    ``ADMIN_USER_UPDATE`` rather than a new event type: this is an administrator
    changing another account's state, which is precisely what that event already
    means, and the ``action`` detail carries the specifics.
    """
    client_ip, user_agent = _get_client_info(request)
    audit_logger.log(
        event_type=AuditEventType.ADMIN_USER_UPDATE,
        outcome=AuditOutcome.SUCCESS,
        user_id=actor.id,
        username=str(actor.email),
        source_ip=client_ip,
        user_agent=user_agent,
        details={
            "action": f"account_{decision}",
            "target_user": str(target.uuid),
            "target_email": str(target.email),
            "target_auth_type": str(target.auth_type),
            "reason": reason,
        },
    )


def _decision_response(user: User, actor: User | None) -> ApprovalDecisionResponse:
    return ApprovalDecisionResponse(
        uuid=user.uuid,
        email=str(user.email),
        approval_status=str(user.approval_status),
        approved_at=user.approved_at,
        approved_by=actor.uuid if actor is not None else None,
    )


def _load_decidable(db: Session, user_uuid: str) -> User:
    """Fetch the target account, refusing one that is not awaiting a decision.

    Re-deciding an already-approved account would silently rewrite
    ``approved_by``/``approved_at`` and, for a reject, revoke the sessions of a
    working account — so this is a 409, not an idempotent no-op. Reversing a
    decision is a deliberate act that goes through the ordinary user-management
    routes (deactivate / delete), not through the queue.
    """
    user = get_user_by_uuid(db, user_uuid)
    if str(user.approval_status) != APPROVAL_PENDING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"This account is already '{user.approval_status}'. "
                "Only an account awaiting approval can be approved or rejected."
            ),
        )
    return user


@router.get("", response_model=list[PendingAccount])
def list_pending_accounts(
    limit: int = Query(200, ge=1, le=1000, description="Max accounts to return"),
    offset: int = Query(0, ge=0, description="Number of accounts to skip"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """List accounts awaiting administrator approval, oldest first.

    Oldest first because this is a queue: the person who has been waiting longest
    is the one to deal with next. Paginated on the same shape as
    ``GET /admin/users``.
    """
    accounts = (
        db.query(User)
        .filter(User.approval_status == APPROVAL_PENDING)
        .order_by(User.created_at, User.id)
        .offset(offset)
        .limit(limit)
        .all()
    )
    return accounts


@router.post("/{user_uuid}/approve", response_model=ApprovalDecisionResponse)
def approve_account(
    request: Request,
    user_uuid: str,
    payload: ApprovalDecisionRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Admit a pending account.

    No session handling is needed: a pending account's credential already worked,
    it was only the lifecycle gate refusing it. Clearing the state is enough for
    the *existing* token to start being accepted on the next request.
    """
    user = _load_decidable(db, user_uuid)
    reason = payload.reason if payload else None

    user.approval_status = APPROVAL_APPROVED
    user.approved_at = datetime.now(UTC)
    user.approved_by = current_user.id
    db.commit()
    db.refresh(user)

    _audit_decision(request, current_user, user, "approved", reason)
    logger.info("Account %s approved by %s", user.email, current_user.email)
    return _decision_response(user, current_user)


@router.post("/{user_uuid}/reject", response_model=ApprovalDecisionResponse)
def reject_account(
    request: Request,
    user_uuid: str,
    payload: ApprovalDecisionRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Refuse a pending account, keeping the row.

    Sessions are revoked even though the lifecycle gate already refuses every
    request from a rejected account. Belt and braces is the house rule for a
    privilege change (``services/account_security_service``), and it means the
    refusal does not depend on a single enforcement point staying correct.
    """
    user = _load_decidable(db, user_uuid)
    reason = payload.reason if payload else None

    user.approval_status = APPROVAL_REJECTED
    # approved_at/approved_by record WHO DECIDED, not "who approved" — the status
    # column says which way. Leaving them NULL on a rejection would lose the one
    # thing an operator asks later: who turned this person away.
    user.approved_at = datetime.now(UTC)
    user.approved_by = current_user.id
    revoke_all_sessions(db, user, reason="account_rejected")
    db.commit()
    db.refresh(user)

    _audit_decision(request, current_user, user, "rejected", reason)
    logger.info("Account %s rejected by %s", user.email, current_user.email)
    return _decision_response(user, current_user)
