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
from typing import Literal
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.deps_context import refuse_under_support_grant
from app.api.endpoints.auth import get_current_active_superuser
from app.api.endpoints.auth.dependencies import _get_client_info
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.db.base import get_db
from app.models.media import Task
from app.models.organization import Organization
from app.schemas.privacy import AdminPurgeRequest
from app.schemas.privacy import EffectivePrivacy
from app.schemas.privacy import InstancePrivacyPolicy
from app.schemas.privacy import InstancePrivacyPolicyUpdate
from app.schemas.privacy import PrivacyDecision
from app.schemas.privacy import PurgeAccepted
from app.schemas.privacy import PurgeAuditRef
from app.schemas.privacy import PurgeRequest
from app.schemas.privacy import PurgeStatus
from app.schemas.privacy import UserPrivacyPolicy
from app.schemas.privacy import UserPrivacyPolicyUpdate
from app.services.platform_access import TenancyMode
from app.services.platform_access import tenancy_mode
from app.services.privacy import policy_admin
from app.services.privacy.policy import gender_decision
from app.services.privacy.policy import voiceprint_decision
from app.services.privacy.purge import PurgeScope
from app.services.privacy.purge import purge_audit_event_types
from app.services.privacy.purge import read_record
from app.tasks.privacy_purge_task import TASK_TYPE
from app.utils.uuid_helpers import get_file_by_uuid_with_permission

logger = logging.getLogger(__name__)

user_router = APIRouter()
admin_router = APIRouter()


def _purge_status(
    raw: str | None,
) -> Literal["pending", "in_progress", "completed", "failed", "skipped"]:
    """The task row's status as the wire vocabulary; anything else reads as still pending."""
    if raw == "in_progress":
        return "in_progress"
    if raw == "completed":
        return "completed"
    if raw == "failed":
        return "failed"
    if raw == "skipped":
        return "skipped"
    return "pending"


def purge_still_enabled_error(exc: policy_admin.PurgeStillEnabledError) -> HTTPException:
    """409 ``policy_still_enabled``: shared by every purge route."""
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": "policy_still_enabled", "targets": exc.targets},
    )


def queue_purge(
    db: Session,
    request: Request,
    scope: PurgeScope,
    targets: list[str],
    actor: models.User,
) -> PurgeAccepted:
    """Start a purge for ``actor`` and answer for the route (409 / 503 mapped here)."""
    source_ip, user_agent = _get_client_info(request)
    try:
        task_id = policy_admin.start_purge(
            db,
            scope,
            targets,
            actor_user_id=int(actor.id),
            actor_username=str(actor.email),
            source_ip=source_ip,
            user_agent=user_agent,
        )
    except policy_admin.PurgeStillEnabledError as exc:
        raise purge_still_enabled_error(exc) from exc
    except policy_admin.PurgeNotQueuedError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The purge could not be queued. Nothing was deleted; try again shortly.",
        ) from exc
    return PurgeAccepted(task_id=task_id)


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
    file_id: int | None = None
    owner_id = int(ctx.user.id)
    organization_id = ctx.org_id
    if file_uuid is not None:
        try:
            media_file = get_file_by_uuid_with_permission(
                db,
                file_uuid,
                owner_id,
                bypass=ctx.bypass,
                organization_id=ctx.org_id,
            )
        except HTTPException as exc:
            # The helper answers 403 for a file that exists and 404 for one that does not; this
            # route reports the policy of a file, so both must read as "no such file".
            if exc.status_code in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND):
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
                ) from exc
            raise
        file_id = int(media_file.id)
        owner_id = int(media_file.user_id)
        organization_id = media_file.organization_id

    voiceprints = voiceprint_decision(
        db, user_id=owner_id, organization_id=organization_id, media_file_id=file_id
    )
    gender = gender_decision(db, user_id=owner_id, organization_id=organization_id)
    organization_uuid = (
        db.scalar(select(Organization.uuid).where(Organization.id == organization_id))
        if organization_id is not None
        else None
    )
    return EffectivePrivacy(
        organization_uuid=organization_uuid,
        voiceprint_matching=PrivacyDecision(
            enabled=voiceprints.enabled, locked_by=voiceprints.locked_by
        ),
        gender_inference=PrivacyDecision(enabled=gender.enabled, locked_by=gender.locked_by),
    )


def _my_policy(db: Session, ctx: RequestContext) -> UserPrivacyPolicy:
    return UserPrivacyPolicy(
        voiceprint_matching_enabled=policy_admin.read_user_switch(db, int(ctx.user.id)),
        locked_by=policy_admin.user_lock_layer(db, ctx.org_id),
    )


@user_router.get("/me", response_model=UserPrivacyPolicy)
def get_my_privacy(
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    _grant: RequestContext = Depends(refuse_under_support_grant),
) -> UserPrivacyPolicy:
    """The caller's own voiceprint switch and whether a layer above it has locked it."""
    return _my_policy(db, ctx)


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
    locked_by = policy_admin.user_lock_layer(db, ctx.org_id)
    if locked_by is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "policy_locked", "locked_by": locked_by},
        )
    policy_admin.write_user_switch(db, int(ctx.user.id), body.voiceprint_matching_enabled)
    return _my_policy(db, ctx)


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
    scope = PurgeScope("user", organization_id=ctx.org_id, user_id=int(ctx.user.id))
    return queue_purge(db, request, scope, list(body.targets), ctx.user)


@user_router.get("/purge/{task_id}", response_model=PurgeStatus)
def get_purge_status(
    task_id: str,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    _grant: RequestContext = Depends(refuse_under_support_grant),
) -> PurgeStatus:
    """Status of a purge the caller started. Anyone else's, or an unknown id, is a 404."""
    task = db.get(Task, task_id)
    if task is None or task.task_type != TASK_TYPE or int(task.user_id) != int(ctx.user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Purge not found")
    record = read_record(task_id) or {}
    counts = record.get("counts")
    residual = record.get("residual")
    return PurgeStatus(
        task_id=task_id,
        status=_purge_status(task.status),
        counts=counts if isinstance(counts, dict) and any(counts.values()) else None,
        residual=residual if isinstance(residual, dict) else None,
        residual_ok=record.get("residual_ok") if isinstance(residual, dict) else None,
        residual_unverified=list(record.get("residual_unverified") or []),
        audit=[
            PurgeAuditRef(event_type=str(event_type.value), task_id=task_id)
            for event_type in purge_audit_event_types(record.get("targets"))
        ],
    )


@admin_router.get("", response_model=InstancePrivacyPolicy)
def get_instance_privacy_policy(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> InstancePrivacyPolicy:
    """The instance layer of the privacy policy (super_admin)."""
    return policy_admin.read_instance_policy(db)


@admin_router.put("", response_model=InstancePrivacyPolicy)
def put_instance_privacy_policy(
    body: InstancePrivacyPolicyUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> InstancePrivacyPolicy:
    """Partial update of the instance layer (super_admin). Audited as admin.settings.change."""
    changes = policy_admin.update_instance_policy(
        db,
        voiceprint_matching=body.voiceprint_matching_enabled,
        gender_inference=body.gender_inference_enabled,
        actor_user_id=int(current_user.id),
    )
    source_ip, user_agent = _get_client_info(request)
    for change in changes:
        audit_logger.log(
            event_type=AuditEventType.ADMIN_SETTINGS_CHANGE,
            outcome=AuditOutcome.SUCCESS,
            user_id=current_user.id,
            username=str(current_user.email),
            source_ip=source_ip,
            user_agent=user_agent,
            details=change,
        )
    return policy_admin.read_instance_policy(db)


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
    if body.scope == "all" and tenancy_mode(db) is not TenancyMode.SINGLE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "purge_scope_not_allowed"},
        )
    scope = PurgeScope("instance_all" if body.scope == "all" else "instance_personal")
    return queue_purge(db, request, scope, list(body.targets), current_user)
