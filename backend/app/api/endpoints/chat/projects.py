"""Chat project CRUD (issue #360).

Projects group conversations about one recurring subject and pin two things
those conversations inherit: a default transcript scope and a system-prompt
layer. Like conversations, they are private to their creator and stamped with
``organization_id`` at creation (v372/v373 tenancy pattern); every lookup
re-checks both, so a project created in an org can never be read from personal
scope or another tenant.

Deleting a project deliberately does NOT delete its conversations — the FK is
``ON DELETE SET NULL`` and they fall back to ungrouped. Losing a grouping is
recoverable; losing the threads is not.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.chat.common import resolve_llm_config_id
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.db.base import get_db
from app.models.chat import ChatConversation
from app.models.chat import ChatProject
from app.schemas.chat import ChatScope
from app.schemas.chat import ProjectCreate
from app.schemas.chat import ProjectDetail
from app.schemas.chat import ProjectList
from app.schemas.chat import ProjectSummary
from app.schemas.chat import ProjectUpdate

logger = logging.getLogger(__name__)

router = APIRouter()


def get_owned_project(db: Session, uuid: str, ctx: RequestContext) -> ChatProject:
    """Fetch one project, or 404 if it isn't this user's in this tenant.

    404 rather than 403 on purpose: a project the caller may not see should not
    be distinguishable from one that does not exist.
    """
    project = (
        db.query(ChatProject)
        .filter(ChatProject.uuid == uuid, ChatProject.user_id == ctx.user.id)
        .first()
    )
    if project is None or project.organization_id != ctx.org_id:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _conversation_counts(db: Session, project_ids: list[int]) -> dict[int, int]:
    """Conversation counts for a page of projects, in one query."""
    if not project_ids:
        return {}
    rows = (
        db.query(ChatConversation.project_id, func.count(ChatConversation.id))
        .filter(ChatConversation.project_id.in_(project_ids))
        .group_by(ChatConversation.project_id)
        .all()
    )
    return {int(pid): int(count) for pid, count in rows}


def to_project_summary(project: ChatProject, conversation_count: int = 0) -> ProjectSummary:
    return ProjectSummary(
        uuid=str(project.uuid),
        name=project.name,
        description=project.description,
        is_archived=bool(project.is_archived),
        conversation_count=conversation_count,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


def to_project_detail(db: Session, project: ChatProject) -> ProjectDetail:
    count = _conversation_counts(db, [int(project.id)]).get(int(project.id), 0)
    llm_uuid: str | None = None
    if project.llm_config_id is not None:
        from app.models.user_llm_settings import UserLLMSettings

        cfg = db.query(UserLLMSettings).filter(UserLLMSettings.id == project.llm_config_id).first()
        llm_uuid = str(cfg.uuid) if cfg else None

    return ProjectDetail(
        **to_project_summary(project, count).model_dump(),
        system_prompt=project.system_prompt,
        scope=ChatScope(**project.default_scope),
        llm_config_uuid=llm_uuid,
        has_scope=project.has_scope,
    )


@router.post("/projects", response_model=ProjectDetail, status_code=201)
def create_project(
    request: Request,
    body: ProjectCreate,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> ProjectDetail:
    """Create a project, optionally pinning a scope and a prompt layer."""
    project = ChatProject(
        user_id=ctx.user.id,
        organization_id=ctx.org_id,
        name=body.name.strip(),
        description=body.description,
        system_prompt=body.system_prompt,
        scope=body.scope.model_dump(),
        llm_config_id=resolve_llm_config_id(db, ctx.user.id, body.llm_config_uuid),
    )
    db.add(project)
    db.commit()
    db.refresh(project)

    audit_logger.log(
        event_type=AuditEventType.CHAT_CONVERSATION_CREATE,
        outcome=AuditOutcome.SUCCESS,
        user_id=ctx.user.id,
        username=str(ctx.user.email),
        organization_id=ctx.org_id,
        source_ip=request.client.host if request.client else None,
        details={
            "project_uuid": str(project.uuid),
            # Counts only — never the project name, prompt, or selected files.
            "scope_files": len(body.scope.file_uuids),
            "scope_collections": len(body.scope.collection_uuids),
            "scope_tags": len(body.scope.tag_names),
            "has_prompt": bool(body.system_prompt),
        },
    )
    return to_project_detail(db, project)


@router.get("/projects", response_model=ProjectList)
def list_projects(
    include_archived: bool = Query(False),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> ProjectList:
    """This user's projects, alphabetical, with conversation counts."""
    query = db.query(ChatProject).filter(ChatProject.user_id == ctx.user.id)
    if not include_archived:
        query = query.filter(ChatProject.is_archived.is_(False))

    # Tenant comparison in Python, matching get_owned_conversation: org_id is
    # NULL for personal scope and SQL equality never matches NULL to NULL.
    projects = [
        p
        for p in query.order_by(func.lower(ChatProject.name)).all()
        if p.organization_id == ctx.org_id
    ]
    counts = _conversation_counts(db, [int(p.id) for p in projects])
    return ProjectList(
        projects=[to_project_summary(p, counts.get(int(p.id), 0)) for p in projects],
        total=len(projects),
    )


@router.get("/projects/{project_uuid}", response_model=ProjectDetail)
def get_project(
    project_uuid: str,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> ProjectDetail:
    return to_project_detail(db, get_owned_project(db, project_uuid, ctx))


@router.patch("/projects/{project_uuid}", response_model=ProjectDetail)
def update_project(
    project_uuid: str,
    body: ProjectUpdate,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> ProjectDetail:
    """Update name, description, prompt layer, pinned scope, model or archive state."""
    project = get_owned_project(db, project_uuid, ctx)

    if body.name is not None:
        project.name = body.name.strip()
    if body.description is not None:
        project.description = body.description
    if body.system_prompt is not None:
        # "" clears the layer; None means "not supplied" and leaves it alone.
        project.system_prompt = body.system_prompt or None
    if body.scope is not None:
        project.scope = body.scope.model_dump()
    if body.llm_config_uuid is not None:
        project.llm_config_id = resolve_llm_config_id(db, ctx.user.id, body.llm_config_uuid)
    if body.is_archived is not None:
        project.is_archived = body.is_archived

    db.commit()
    db.refresh(project)
    return to_project_detail(db, project)


@router.delete("/projects/{project_uuid}", status_code=204)
def delete_project(
    project_uuid: str,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> None:
    """Delete a project. Its conversations survive, ungrouped.

    The FK is ON DELETE SET NULL, but that fires in the database and the ORM's
    identity map would keep stale project_id values on any conversation already
    loaded in this session. Detaching explicitly first keeps both in agreement.
    """
    project = get_owned_project(db, project_uuid, ctx)
    db.query(ChatConversation).filter(ChatConversation.project_id == project.id).update(
        {ChatConversation.project_id: None}, synchronize_session=False
    )
    db.delete(project)
    db.commit()
