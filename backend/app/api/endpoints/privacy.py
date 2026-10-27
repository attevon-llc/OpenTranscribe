"""``/privacy`` and ``/admin/privacy-policy``: voiceprint and gender controls (issue #1280).

Two routers, mounted in ``api/router.py``:

* ``user_router`` (``/privacy``, any signed-in user): the policy that applies to a file or to the
  caller (``GET /effective``), the caller's own voiceprint switch (``GET|PUT /me``), purging the
  caller's own data (``POST /me/purge``) and reading the status of a purge the caller started.
* ``admin_router`` (``/admin/privacy-policy``, super_admin): the instance layer
  (``GET|PUT``) and the instance purge (``POST /purge``).

Turning a switch off hides data and never deletes it; a purge is the separate, explicit,
audited deletion. Contract and error codes: ``app.schemas.privacy``.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from app import models
from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.deps_context import refuse_under_support_grant
from app.api.endpoints.auth import get_current_active_superuser
from app.db.base import get_db
from app.schemas.privacy import AdminPurgeRequest
from app.schemas.privacy import EffectivePrivacy
from app.schemas.privacy import InstancePrivacyPolicy
from app.schemas.privacy import InstancePrivacyPolicyUpdate
from app.schemas.privacy import PurgeAccepted
from app.schemas.privacy import PurgeRequest
from app.schemas.privacy import PurgeStatus
from app.schemas.privacy import UserPrivacyPolicy
from app.schemas.privacy import UserPrivacyPolicyUpdate

logger = logging.getLogger(__name__)

user_router = APIRouter()
admin_router = APIRouter()


def _not_implemented() -> HTTPException:
    return HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="Not implemented")


@user_router.get("/effective", response_model=EffectivePrivacy)
def get_effective_privacy(
    file_uuid: UUID | None = Query(
        default=None,
        description=(
            "A file the caller can see: the answer is for that file's owner and organization. "
            "Another user's file, or a missing one, is a 404 exactly like the file page."
        ),
    ),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> EffectivePrivacy:
    """The policy as it applies to one file, or to the caller in the active organization."""
    raise _not_implemented()


@user_router.get("/me", response_model=UserPrivacyPolicy)
def get_my_privacy(
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    _grant: RequestContext = Depends(refuse_under_support_grant),
) -> UserPrivacyPolicy:
    """The caller's own voiceprint switch and whether a layer above it has locked it."""
    raise _not_implemented()


@user_router.put(
    "/me",
    response_model=UserPrivacyPolicy,
    responses={409: {"description": "policy_locked: a layer above the user has it off."}},
)
def put_my_privacy(
    body: UserPrivacyPolicyUpdate,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    _grant: RequestContext = Depends(refuse_under_support_grant),
) -> UserPrivacyPolicy:
    """Set the caller's own voiceprint switch. 409 ``policy_locked`` while a higher layer is off."""
    raise _not_implemented()


@user_router.post(
    "/me/purge",
    response_model=PurgeAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses={409: {"description": "policy_still_enabled: a target is still switched on."}},
)
def purge_my_data(
    body: PurgeRequest,
    request: Request,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    _grant: RequestContext = Depends(refuse_under_support_grant),
) -> PurgeAccepted:
    """Irreversibly delete the caller's own voiceprints and/or inferred gender in the active scope."""
    raise _not_implemented()


@user_router.get("/purge/{task_id}", response_model=PurgeStatus)
def get_purge_status(
    task_id: str,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    _grant: RequestContext = Depends(refuse_under_support_grant),
) -> PurgeStatus:
    """Status of a purge the caller started. Anyone else's, or an unknown id, is a 404."""
    raise _not_implemented()


@admin_router.get("", response_model=InstancePrivacyPolicy)
def get_instance_privacy_policy(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> InstancePrivacyPolicy:
    """The instance layer of the privacy policy (super_admin)."""
    raise _not_implemented()


@admin_router.put("", response_model=InstancePrivacyPolicy)
def put_instance_privacy_policy(
    body: InstancePrivacyPolicyUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> InstancePrivacyPolicy:
    """Partial update of the instance layer (super_admin). Audited as admin.settings.change."""
    raise _not_implemented()


@admin_router.post(
    "/purge",
    response_model=PurgeAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        403: {"description": "purge_scope_not_allowed: scope 'all' in a multi-tenant deployment."},
        409: {"description": "policy_still_enabled: a target is still switched on."},
    },
)
def purge_instance_data(
    body: AdminPurgeRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> PurgeAccepted:
    """Irreversibly delete voiceprints and/or inferred gender instance-wide (super_admin)."""
    raise _not_implemented()
