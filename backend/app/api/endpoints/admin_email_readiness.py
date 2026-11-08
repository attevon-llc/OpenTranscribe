"""Whether authentication mail can be delivered (admin+).

This is a status read, not deployment configuration: it lives under ``/api/admin/email``
and not ``/api/admin/auth-config`` (whose whole namespace is super_admin), so the
tier guard needs no exemption. A plain admin can invite users but cannot read the
designation (super_admin), so the invite form had no way to warn that the invitation
will not be sent.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from sqlalchemy.orm import Session

from app.api.endpoints.auth import get_current_admin_user
from app.auth.email_verification import email_verification_required
from app.db.base import get_db
from app.models.user import User
from app.schemas.email_notification import AuthMailReadinessResponse
from app.services import auth_mail_config_service as designation

router = APIRouter()


@router.get("/readiness", response_model=AuthMailReadinessResponse)
def get_auth_mail_readiness(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> AuthMailReadinessResponse:
    """Whether auth mail can be delivered, for admins who invite users (admin+).

    Returns only ``deliverable`` and ``verification_required``; the config name,
    UUID, provider and env-SMTP flag never leave the super_admin designation route.
    """
    current = designation.describe_designation(db)
    return AuthMailReadinessResponse(
        deliverable=current.deliverable,
        verification_required=email_verification_required(db),
    )
