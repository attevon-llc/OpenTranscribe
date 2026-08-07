"""Chat conversation CRUD.

Conversations are private to their creator. ``organization_id`` is stamped at
creation from the request context (v372/v373 tenancy pattern) and every lookup
re-checks it, so a conversation started in an org can never be read from personal
scope or another tenant.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.chat.common import get_owned_conversation
from app.api.endpoints.chat.common import read_user_chat_settings
from app.api.endpoints.chat.common import resolve_llm_config_id
from app.api.endpoints.chat.common import to_detail
from app.api.endpoints.chat.common import to_summary
from app.api.endpoints.chat.projects import get_owned_project
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.db.base import get_db
from app.models.chat import ChatConversation
from app.models.chat import ChatMessage
from app.schemas.chat import ConversationCreate
from app.schemas.chat import ConversationDetail
from app.schemas.chat import ConversationList
from app.schemas.chat import ConversationUpdate

logger = logging.getLogger(__name__)

router = APIRouter()


def _message_counts(db: Session, conversation_ids: list[int]) -> dict[int, int]:
    """Message counts for a page of conversations, in one query."""
    if not conversation_ids:
        return {}
    rows = (
        db.query(ChatMessage.conversation_id, func.count(ChatMessage.id))
        .filter(ChatMessage.conversation_id.in_(conversation_ids))
        .group_by(ChatMessage.conversation_id)
        .all()
    )
    return {int(cid): int(count) for cid, count in rows}


@router.post("/conversations", response_model=ConversationDetail, status_code=201)
def create_conversation(
    request: Request,
    body: ConversationCreate,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> ConversationDetail:
    """Start a new conversation, optionally pre-scoped to files/collections/tags."""
    llm_config_id = resolve_llm_config_id(db, ctx.user.id, body.llm_config_uuid)

    # 404s through get_owned_project if the project isn't the caller's, so a
    # conversation can never be filed into someone else's workspace.
    project = get_owned_project(db, body.project_uuid, ctx) if body.project_uuid else None
    # A project's model is a default for chats created inside it; an explicit
    # llm_config_uuid on the request still wins.
    if project is not None and llm_config_id is None:
        llm_config_id = project.llm_config_id

    conversation = ChatConversation(
        user_id=ctx.user.id,
        organization_id=ctx.org_id,
        project_id=project.id if project else None,
        title=body.title,
        context=body.scope.model_dump(),
        llm_config_id=llm_config_id,
        settings=body.settings.model_dump(exclude_none=True) if body.settings else None,
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)

    audit_logger.log(
        event_type=AuditEventType.CHAT_CONVERSATION_CREATE,
        outcome=AuditOutcome.SUCCESS,
        user_id=ctx.user.id,
        username=str(ctx.user.email),
        organization_id=ctx.org_id,
        source_ip=request.client.host if request.client else None,
        details={
            "conversation_uuid": str(conversation.uuid),
            # Counts only — never the selected filenames or tag names.
            "scope_files": len(body.scope.file_uuids),
            "scope_collections": len(body.scope.collection_uuids),
            "scope_tags": len(body.scope.tag_names),
        },
    )

    return to_detail(db, conversation, read_user_chat_settings(db, ctx.user.id))


@router.get("/conversations", response_model=ConversationList)
def list_conversations(
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    q: str | None = Query(None, max_length=200, description="Filter by title"),
    archived: bool = Query(False, description="Return archived conversations instead"),
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> ConversationList:
    """List the caller's conversations, most recently active first."""
    query = db.query(ChatConversation).filter(
        ChatConversation.user_id == ctx.user.id,
        ChatConversation.is_archived.is_(archived),
    )
    query = (
        query.filter(ChatConversation.organization_id == ctx.org_id)
        if ctx.org_id is not None
        else query.filter(ChatConversation.organization_id.is_(None))
    )
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.filter(ChatConversation.title.ilike(f"%{escaped}%"))

    total = query.count()
    rows = (
        query.order_by(
            ChatConversation.last_message_at.desc().nullslast(),
            ChatConversation.id.desc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )

    counts = _message_counts(db, [row.id for row in rows])
    return ConversationList(
        conversations=[to_summary(row, counts.get(row.id, 0)) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/conversations/{conversation_uuid}", response_model=ConversationDetail)
def get_conversation(
    conversation_uuid: str,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> ConversationDetail:
    """Fetch one conversation's metadata, scope and settings."""
    conversation = get_owned_conversation(db, ctx, conversation_uuid)
    counts = _message_counts(db, [conversation.id])
    return to_detail(
        db,
        conversation,
        read_user_chat_settings(db, ctx.user.id),
        counts.get(conversation.id, 0),
    )


@router.patch("/conversations/{conversation_uuid}", response_model=ConversationDetail)
def update_conversation(
    conversation_uuid: str,
    body: ConversationUpdate,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> ConversationDetail:
    """Update title, archive state, scope, model override or settings."""
    conversation = get_owned_conversation(db, ctx, conversation_uuid)

    if body.title is not None:
        conversation.title = body.title
    if body.is_archived is not None:
        conversation.is_archived = body.is_archived
    if body.scope is not None:
        conversation.context = body.scope.model_dump()
    if body.llm_config_uuid is not None:
        conversation.llm_config_id = (
            resolve_llm_config_id(db, ctx.user.id, body.llm_config_uuid)
            if body.llm_config_uuid
            else None
        )
    if body.project_uuid is not None:
        # "" is the explicit "move out to ungrouped" signal; a uuid moves it.
        conversation.project_id = (
            get_owned_project(db, body.project_uuid, ctx).id if body.project_uuid else None
        )
    if body.settings is not None:
        # Merge so the Chat Controls panel can PATCH one field at a time.
        merged = dict(conversation.settings or {})
        merged.update(body.settings.model_dump(exclude_unset=True))
        conversation.settings = merged

    db.commit()
    db.refresh(conversation)

    counts = _message_counts(db, [conversation.id])
    return to_detail(
        db,
        conversation,
        read_user_chat_settings(db, ctx.user.id),
        counts.get(conversation.id, 0),
    )


@router.delete("/conversations/{conversation_uuid}")
def delete_conversation(
    request: Request,
    conversation_uuid: str,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> dict:
    """Permanently delete a conversation and its messages (FK CASCADE)."""
    conversation = get_owned_conversation(db, ctx, conversation_uuid)
    db.delete(conversation)
    db.commit()

    audit_logger.log(
        event_type=AuditEventType.CHAT_CONVERSATION_DELETE,
        outcome=AuditOutcome.SUCCESS,
        user_id=ctx.user.id,
        username=str(ctx.user.email),
        organization_id=ctx.org_id,
        source_ip=request.client.host if request.client else None,
        details={"conversation_uuid": conversation_uuid},
    )
    return {"status": "deleted", "uuid": conversation_uuid}
