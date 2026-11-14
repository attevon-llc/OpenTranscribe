"""Invitation endpoints — admin provisioning that actually produces a login.

Three admin routes (create / list / revoke) and two public ones (lookup /
accept). The public pair is rate-limited like every other unauthenticated auth
route and answers every bad token with one identical error, so it is neither a
brute-force target nor a token oracle.

Privilege split follows ``app/auth/CLAUDE.md``: managing user accounts is
``admin``, but minting an ``admin``/``super_admin`` is ``super_admin`` — an
invitation is a deferred account creation, so it inherits the same gate as
``POST /api/admin/users``.
"""

import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import status
from sqlalchemy.orm import Session

from app.api.endpoints.auth.dependencies import get_current_admin_user
from app.api.endpoints.auth.lifecycle_gates import _get_client_info
from app.api.rate_limit import get_auth_rate_limit
from app.api.rate_limit import limiter
from app.auth.audit import audit_logger
from app.auth.audit_events import AuditEventType
from app.auth.invitations import GENERIC_INVALID
from app.auth.invitations import INVALID_CODE
from app.auth.invitations import accept_invitation
from app.auth.invitations import create_invitation
from app.auth.invitations import invitation_status
from app.auth.invitations import lookup_invitation
from app.auth.invitations import revoke_invitation
from app.auth.roles import ELEVATED_ROLES
from app.auth.roles import ROLE_SUPER_ADMIN
from app.core.auth_types import AUTH_TYPE_LOCAL
from app.db.base import get_db
from app.models.invitation import UserInvitation
from app.models.user import User
from app.schemas.invitation import InvitationAcceptRequest
from app.schemas.invitation import InvitationAcceptResponse
from app.schemas.invitation import InvitationCreate
from app.schemas.invitation import InvitationLookupRequest
from app.schemas.invitation import InvitationLookupResponse
from app.schemas.invitation import InvitationResponse
from app.services.invitation_queries import list_invitations as list_invitations_query
from app.services.uuid_helpers import get_by_uuid

logger = logging.getLogger(__name__)

router = APIRouter()


def _as_response(invitation: UserInvitation) -> InvitationResponse:
    return InvitationResponse(
        uuid=invitation.uuid,
        email=str(invitation.email),
        full_name=invitation.full_name,
        role=str(invitation.role),
        auth_type=str(invitation.auth_type),
        expires_at=invitation.expires_at,
        created_at=invitation.created_at,
        used_at=invitation.used_at,
        revoked_at=invitation.revoked_at,
        status=invitation_status(invitation),
    )


@router.post("/invitations", response_model=InvitationResponse, status_code=status.HTTP_201_CREATED)
def create_invitation_endpoint(
    request: Request,
    body: InvitationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> Any:
    """Invite someone to create an account (admin; super_admin for elevated roles).

    The admin never sees or chooses a password. For an external ``auth_type`` no
    local password is created at all — the account is pre-provisioned so the IdP
    matches it at first login.
    """
    if body.role in ELEVATED_ROLES and current_user.role != ROLE_SUPER_ADMIN:
        logger.warning(
            "Admin %s (role=%s) attempted to invite a '%s' — denied",
            current_user.email,
            current_user.role,
            body.role,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a super_admin can invite admin or super_admin accounts",
        )

    client_ip, user_agent = _get_client_info(request)
    invitation, error = create_invitation(
        db,
        email=str(body.email),
        full_name=body.full_name,
        role=body.role,
        auth_type=body.auth_type,
        expires_in_hours=body.expires_in_hours,
        created_by=current_user,
        ip_address=client_ip,
    )
    if invitation is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error)

    audit_logger.log_admin_action(
        event_type=AuditEventType.ADMIN_USER_CREATE,
        admin_user_id=int(current_user.id),
        admin_username=str(current_user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        details={
            "action": "invitation_created",
            "invited_email": str(body.email),
            "role": body.role,
            "auth_type": body.auth_type,
        },
    )
    return _as_response(invitation)


@router.get("/invitations", response_model=list[InvitationResponse])
def list_invitations(
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> Any:
    """List invitations (admin). Pending only unless ``include_inactive``."""
    invitations = list_invitations_query(db, include_inactive)
    return [_as_response(i) for i in invitations]


@router.delete("/invitations/{invitation_uuid}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_invitation_endpoint(
    request: Request,
    invitation_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> None:
    """Revoke a pending invitation (admin). Idempotent."""
    # Validates the UUID format (400) and 404s a miss, like every other route.
    invitation = get_by_uuid(db, UserInvitation, invitation_uuid, "Invitation not found")
    revoke_invitation(db, invitation)

    client_ip, user_agent = _get_client_info(request)
    audit_logger.log_admin_action(
        event_type=AuditEventType.ADMIN_USER_UPDATE,
        admin_user_id=int(current_user.id),
        admin_username=str(current_user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        details={"action": "invitation_revoked", "invited_email": str(invitation.email)},
    )
    return None


def _invalid_detail() -> dict[str, str]:
    """The one refusal for every bad token state: a stable code plus the English sentence."""
    return {"code": INVALID_CODE, "message": GENERIC_INVALID}


@router.post("/invitations/lookup", response_model=InvitationLookupResponse)
@limiter.limit(get_auth_rate_limit())
def lookup_invitation_endpoint(
    request: Request,
    response: Response,
    body: InvitationLookupRequest,
    db: Session = Depends(get_db),
) -> Any:
    """What the accept page needs, for the holder of a valid invite token.

    Public and rate-limited. Discloses nothing to a caller who does not already
    hold the token, and returns one identical 400 for unknown, expired, revoked
    and already-used tokens alike.
    """
    invitation = lookup_invitation(db, body.token)
    if invitation is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_invalid_detail())

    return InvitationLookupResponse(
        email=str(invitation.email),
        full_name=invitation.full_name,
        auth_type=str(invitation.auth_type),
        requires_password=str(invitation.auth_type) == AUTH_TYPE_LOCAL,
        expires_at=invitation.expires_at,
    )


@router.post("/invitations/accept", response_model=InvitationAcceptResponse)
@limiter.limit(get_auth_rate_limit())
def accept_invitation_endpoint(
    request: Request,
    response: Response,
    body: InvitationAcceptRequest,
    db: Session = Depends(get_db),
) -> Any:
    """Redeem an invitation and activate the account.

    Public and rate-limited. The account is created with the role and auth_type
    the admin chose; the invitee chooses the password (local) or is handed to
    the IdP (external).
    """
    user, error = accept_invitation(db, body.token, body.password, body.full_name)
    if user is None:
        # A token-state rejection carries the stable code; a password-policy refusal is a
        # specific sentence the user acts on, so it stays a plain detail.
        detail = _invalid_detail() if error == GENERIC_INVALID else error
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)

    client_ip, user_agent = _get_client_info(request)
    audit_logger.log_admin_action(
        event_type=AuditEventType.ADMIN_USER_CREATE,
        admin_user_id=int(user.id),
        admin_username=str(user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        details={"registration_type": "invitation", "auth_type": str(user.auth_type)},
    )

    can_login = str(user.auth_type) == AUTH_TYPE_LOCAL
    return InvitationAcceptResponse(
        email=str(user.email),
        auth_type=str(user.auth_type),
        can_login_with_password=can_login,
        message=(
            "Your account is ready — you can sign in now."
            if can_login
            else "Your account is ready. Sign in with your organization's identity provider."
        ),
    )
