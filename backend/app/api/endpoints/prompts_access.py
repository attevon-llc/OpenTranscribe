"""Tenant and sharing gates for summary prompts.

Which prompts a caller may see, clone or select: system defaults, their own, and other
users' shared prompts that reach the caller's active tenant (owner in the tenant, and any
organization stamp naming it). Shared by ``prompts.py``, ``prompts_active.py``,
``prompts_sharing.py`` and ``summarization.py``; a router-free module because the parent
includes the children, so they cannot import the parent.
"""

from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_
from sqlalchemy import not_
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app import models
from app.services.request_context import RequestContext
from app.services.summary_prompt_queries import active_prompt_matches_uuid
from app.services.summary_prompt_queries import count_active_user_prompts
from app.services.summary_prompt_queries import prompt_matches
from app.utils.tenant_sharing import owner_in_tenant
from app.utils.tenant_sharing import shared_visible_in_tenant

MAX_USER_PROMPTS = 50


def _stamp_in_tenant(ctx: RequestContext) -> Any:
    """A prompt's own organization stamp, when it has one, must name the active tenant."""
    org_col = models.SummaryPrompt.organization_id
    return (
        org_col.is_(None) if ctx.org_id is None else or_(org_col.is_(None), org_col == ctx.org_id)
    )


def _in_tenant(ctx: RequestContext) -> Any:
    """Prompts that belong to the caller's active tenant, shared or not.

    The owner must be in that tenant, and a prompt stamped for an organization belongs
    only to that organization.
    """
    return and_(
        owner_in_tenant(models.SummaryPrompt.user_id, ctx.org_id),
        _stamp_in_tenant(ctx),
    )


def _shared_in_tenant(ctx: RequestContext) -> Any:
    """Other users' shared prompts that reach the caller's active tenant."""
    return and_(
        models.SummaryPrompt.is_shared.is_(True),
        models.SummaryPrompt.user_id != ctx.user.id,
        not_(models.SummaryPrompt.is_system_default),
        _in_tenant(ctx),
    )


def _usable_by(ctx: RequestContext) -> Any:
    """Prompts the caller may read, clone or select: system, own, or shared in-tenant."""
    return or_(
        models.SummaryPrompt.is_system_default,
        shared_visible_in_tenant(
            models.SummaryPrompt.user_id,
            models.SummaryPrompt.is_shared,
            ctx.user.id,
            ctx.org_id,
            org_col=models.SummaryPrompt.organization_id,
        ),
    )


def _is_usable(db: Session, prompt: models.SummaryPrompt, ctx: RequestContext) -> bool:
    return prompt_matches(db, prompt.id, _usable_by(ctx))


def require_usable_prompt_uuid(db: Session, prompt_uuid: str, ctx: RequestContext) -> None:
    """404 unless ``prompt_uuid`` names an active prompt the caller may use."""
    if not active_prompt_matches_uuid(db, prompt_uuid, _usable_by(ctx)):
        raise HTTPException(status_code=404, detail="Prompt not found")


def _assert_under_prompt_limit(db: Session, user_id: int) -> None:
    """Raise HTTP 400 if the user is at or above the custom-prompt cap.

    Counts active, user-owned prompts. Clones count toward this limit too.
    """
    if count_active_user_prompts(db, user_id) >= MAX_USER_PROMPTS:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum number of custom prompts reached ({MAX_USER_PROMPTS})",
        )
