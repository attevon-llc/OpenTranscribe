"""Shared prompt library and the share toggle.

Static ``/shared`` paths, included by ``prompts.py`` before its ``/{prompt_uuid}`` routes.
The tenant gates live in ``prompts_access.py``.
"""

import logging
from datetime import UTC
from datetime import datetime
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from sqlalchemy import and_
from sqlalchemy import not_
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import models
from app import schemas
from app.api.deps_context import get_current_context
from app.api.endpoints.prompts_access import _in_tenant
from app.api.endpoints.prompts_access import _shared_in_tenant
from app.auth.audit import audit_logger
from app.auth.audit import request_org_id
from app.auth.audit_events import AuditEventType
from app.auth.audit_events import AuditOutcome
from app.db.base import get_db
from app.middleware.audit import get_request_context
from app.services.platform_access import TenancyMode
from app.services.platform_access import tenancy_mode
from app.services.request_context import RequestContext
from app.services.summary_prompt_queries import clear_active_prompt_settings
from app.services.summary_prompt_queries import prompt_matches
from app.services.summary_prompt_queries import shared_library_page
from app.services.summary_prompt_queries import shared_library_tags
from app.services.summary_prompt_queries import users_by_id
from app.services.uuid_helpers import get_prompt_by_uuid

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/shared/library", response_model=schemas.SharedPromptLibrary)
def get_shared_prompt_library(
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    content_type: str | None = Query(None),
    tags: str | None = Query(None, description="Comma-separated tags"),
    search: str | None = Query(None, description="Search name/description"),
    sort_by: str = Query("newest", description="popular, newest, or name"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
) -> Any:
    """Browse shared prompts with filtering and pagination."""
    current_user = ctx.user
    # The library shows prompts shared within the caller's tenant, the caller's own
    # shared prompts included.
    shared_here = or_(
        _shared_in_tenant(ctx),
        and_(
            models.SummaryPrompt.user_id == current_user.id,
            models.SummaryPrompt.is_shared.is_(True),
            not_(models.SummaryPrompt.is_system_default),
        ),
    )
    prompts, total = shared_library_page(
        db,
        shared_here,
        content_type=content_type,
        tags=tags,
        search=search,
        sort_by=sort_by,
        skip=skip,
        limit=limit,
    )

    # Batch-fetch users referenced for attribution: creators + sharers
    referenced_ids = {p.user_id for p in prompts if p.user_id}
    referenced_ids |= {p.shared_by for p in prompts if p.shared_by}
    users = users_by_id(db, referenced_ids)

    prompt_list = []
    for p in prompts:
        sp = schemas.SummaryPrompt.model_validate(p)
        sp.is_owner = p.user_id == current_user.id
        owner = users.get(p.user_id) if p.user_id else None
        if owner:
            sp.author_name = owner.full_name
            sp.author_role = owner.role
        if p.shared_by:
            sharer = users.get(p.shared_by)
            if sharer:
                sp.shared_by_name = sharer.full_name
        prompt_list.append(sp)

    # Get available tags (from the same tenant-scoped set as the listing)
    available_tags: list[str] = []
    try:
        available_tags = shared_library_tags(db, shared_here)
    except Exception:
        logger.debug("Could not fetch available tags", exc_info=True)

    return schemas.SharedPromptLibrary(
        prompts=prompt_list,
        total=total,
        page=skip // limit + 1 if limit > 0 else 1,
        size=len(prompt_list),
        has_next=(skip + limit) < total,
        has_prev=skip > 0,
        available_tags=available_tags,
    )


@router.post("/shared/{prompt_uuid}/toggle", response_model=schemas.SummaryPrompt)
def share_prompt(
    prompt_uuid: str,
    share_data: schemas.SummaryPromptShare,
    request: Request,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
    """Toggle sharing on a prompt. The owner, or an admin of the prompt's tenant, can toggle.

    A prompt outside the caller's tenant answers 404, the same as a missing one, so its
    existence is not disclosed. A personal workspace is its own tenant: in a multi-tenant
    deployment nobody but the owner reaches a personal prompt, and the platform role only
    does through a write support grant for that user. Inside an organization the authority
    is the org admin role or a write grant (which assumes the granted tenant), never the
    platform role alone. A single-tenant deployment keeps instance-wide admin access.
    """
    current_user = ctx.user
    prompt = get_prompt_by_uuid(db, prompt_uuid)

    if prompt.user_id != current_user.id and not prompt.is_system_default:
        if not prompt_matches(db, prompt.id, _in_tenant(ctx)):
            raise HTTPException(status_code=404, detail="Prompt not found")
        may_administer = ctx.is_org_admin or ctx.bypass.allows(
            org_id=ctx.org_id,
            owner_id=int(prompt.user_id) if prompt.user_id is not None else None,
            need="write",
            resource_type="prompt",
            resource_uuid=str(prompt.uuid),
        )
        if not may_administer:
            if ctx.org_id is None and tenancy_mode(db) is TenancyMode.MULTI:
                # Another person's personal workspace: indistinguishable from a missing prompt.
                raise HTTPException(status_code=404, detail="Prompt not found")
            raise HTTPException(status_code=403, detail="Not authorized to share this prompt")
    if prompt.is_system_default:
        raise HTTPException(status_code=400, detail="Cannot share system prompts")

    prompt.is_shared = share_data.is_shared
    if share_data.is_shared:
        if not prompt.shared_at:
            prompt.shared_at = datetime.now(UTC)
        # Record who flipped sharing on (may differ from the creator)
        prompt.shared_by = current_user.id
    else:
        prompt.shared_at = None
        prompt.shared_by = None
        # Clean up other users who had this prompt set as active
        clear_active_prompt_settings(db, prompt.id, except_user_id=current_user.id)

    db.commit()
    db.refresh(prompt)

    req_meta = get_request_context(request)
    audit_logger.log(
        event_type=AuditEventType.PROMPT_SHARE
        if share_data.is_shared
        else AuditEventType.PROMPT_UNSHARE,
        outcome=AuditOutcome.SUCCESS,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=req_meta["source_ip"],
        user_agent=req_meta["user_agent"],
        # Org attribution (issue #262a): resolved by resolve_org_context when
        # the request carried org context; None (personal/community) otherwise.
        organization_id=request_org_id(request),
        details={
            "prompt_uuid": str(prompt.uuid),
            "prompt_name": str(prompt.name),
            "owner_id": int(prompt.user_id) if prompt.user_id else None,
        },
    )
    return prompt
