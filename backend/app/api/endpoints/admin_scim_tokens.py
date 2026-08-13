"""Issue and revoke SCIM provisioning tokens — ``/api/admin/scim-tokens``.

**super_admin tier.** A SCIM token can create accounts and disable them across the
whole deployment, which puts it in the same class as the LDAP bind password and the
OIDC client secret: infrastructure credentials, not user management. The dividing
rule is in ``backend/app/auth/CLAUDE.md``, and
``tests/unit/test_route_privilege_tiers.py`` pins this prefix.

The plaintext token is returned by ``POST`` **once** and never again — the row stores
only a SHA-256 digest. Losing it means issuing a new one, which is the intended
recovery path.
"""

from __future__ import annotations

import logging
import uuid as uuid_pkg
from datetime import datetime

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import status
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from app.api.endpoints.auth.dependencies import _get_client_info
from app.api.endpoints.auth.dependencies import get_current_active_superuser
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.db.base import get_db
from app.models.user import User
from app.services import scim_token_service

router = APIRouter()
logger = logging.getLogger(__name__)


class SCIMTokenCreate(BaseModel):
    """Request body for issuing a token."""

    name: str = Field(..., min_length=1, max_length=255)
    #: Optional expiry. Omitted means "until revoked" — a SCIM connector is
    #: configured once, and a surprise expiry is a silent provisioning outage.
    expires_at: datetime | None = None


class SCIMTokenResponse(BaseModel):
    """A token as listed. Never carries the secret."""

    uuid: str
    name: str
    created_at: datetime | None = None
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class SCIMTokenCreatedResponse(SCIMTokenResponse):
    """The one response that carries the plaintext, immediately after issue."""

    token: str


def _as_response(row) -> SCIMTokenResponse:
    return SCIMTokenResponse(
        uuid=str(row.uuid),
        name=str(row.name),
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )


@router.get("", response_model=list[SCIMTokenResponse])
def list_scim_tokens(
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_active_superuser),
):
    """List every SCIM token, including revoked ones (they explain past events)."""
    return [_as_response(row) for row in scim_token_service.list_tokens(db)]


@router.post("", response_model=SCIMTokenCreatedResponse, status_code=status.HTTP_201_CREATED)
def create_scim_token(
    payload: SCIMTokenCreate,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_active_superuser),
):
    """Issue a token and return its plaintext exactly once."""
    row, plaintext = scim_token_service.issue_token(
        db,
        name=payload.name,
        created_by=int(admin.id),
        expires_at=payload.expires_at,
    )
    client_ip, user_agent = _get_client_info(request)
    audit_logger.log(
        event_type=AuditEventType.ADMIN_SETTINGS_CHANGE,
        outcome=AuditOutcome.SUCCESS,
        user_id=admin.id,
        username=str(admin.email),
        source_ip=client_ip,
        user_agent=user_agent,
        details={"action": "scim_token_create", "token_uuid": str(row.uuid), "name": row.name},
    )
    base = _as_response(row)
    return SCIMTokenCreatedResponse(**base.model_dump(), token=plaintext)


@router.delete("/{token_uuid}", response_model=SCIMTokenResponse)
def revoke_scim_token(
    token_uuid: str,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_active_superuser),
):
    """Revoke a token. Idempotent, and never reversible.

    Raises:
        HTTPException: 400 when *token_uuid* is not a UUID, 404 when it names no
            token. The malformed case used to reach a Postgres UUID comparison and
            die as an unhandled ``DataError`` — a 500 whose aborted transaction also
            poisoned the rest of the request's session. The message matches
            ``utils/uuid_helpers.get_by_uuid`` so every admin UUID path answers the
            same way.
    """
    try:
        parsed_uuid = uuid_pkg.UUID(token_uuid)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid UUID format: {token_uuid}",
        ) from None

    row = scim_token_service.revoke_token(db, str(parsed_uuid))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SCIM token not found")
    client_ip, user_agent = _get_client_info(request)
    audit_logger.log(
        event_type=AuditEventType.ADMIN_SETTINGS_CHANGE,
        outcome=AuditOutcome.SUCCESS,
        user_id=admin.id,
        username=str(admin.email),
        source_ip=client_ip,
        user_agent=user_agent,
        details={"action": "scim_token_revoke", "token_uuid": str(row.uuid), "name": row.name},
    )
    return _as_response(row)
