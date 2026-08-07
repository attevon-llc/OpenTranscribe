"""Shared helpers for the chat endpoints.

Ownership lookup lives here because it is the authorization boundary for the
whole feature: every conversation and message route resolves through
:func:`get_owned_conversation`, which requires BOTH the owning user and a
matching tenant stamp, and 404s (never 403) on a miss so a probe cannot confirm
that someone else's conversation exists.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.core import constants as C  # noqa: N812
from app.models.chat import ChatConversation
from app.models.chat import ChatProject
from app.schemas.chat import ChatScope
from app.schemas.chat import ConversationDetail
from app.schemas.chat import ConversationSettings
from app.schemas.chat import ConversationSummary

logger = logging.getLogger(__name__)

# Per-user preference keys (UserSetting).
USER_SETTING_KEYS = {
    "system_prompt": "chat.system_prompt",
    "use_context_default": "chat.use_context_default",
    "default_search_mode": "chat.default_search_mode",
}


def get_owned_conversation(
    db: Session, ctx: RequestContext, conversation_uuid: str
) -> ChatConversation:
    """Load a conversation the caller owns, in their tenant scope.

    Raises:
        HTTPException: 404 when it doesn't exist, isn't theirs, or belongs to a
            different tenant — all indistinguishable by design.
    """
    conversation = (
        db.query(ChatConversation)
        .filter(
            ChatConversation.uuid == conversation_uuid,
            ChatConversation.user_id == ctx.user.id,
        )
        .first()
    )
    if conversation is None or conversation.organization_id != ctx.org_id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


def resolve_effective_scope(
    conversation: ChatConversation, project: ChatProject | None
) -> ChatScope:
    """The scope a turn actually retrieves against (issue #360).

    Precedence: the conversation's own pinned recordings, else the project's
    pinned default, else empty — which ``resolve_scope_file_uuids`` turns into
    ``None``, meaning "everything the caller can access".

    That last step is why this function must not be clever. An empty scope means
    "all accessible", while an *explicitly resolved but empty* file list means
    "match nothing". Returning a ChatScope with empty lists when a project pins
    nothing preserves the first meaning; inventing an empty file_uuids list here
    would silently switch a project to the second and answer every question with
    "no relevant excerpts".

    Speakers are a separate axis, so the conversation's own speaker filter
    survives inheriting the project's recordings — "what did Dana say" stays
    scoped to Dana while widening to the client's calls.

    Args:
        conversation: The conversation being answered.
        project: Its owning project, or None when ungrouped.

    Returns:
        The scope to resolve into file UUIDs.
    """
    conv_scope = ChatScope(**conversation.scope)
    if not conv_scope.is_empty or project is None or not project.has_scope:
        return conv_scope

    inherited = ChatScope(**project.default_scope)
    return ChatScope(
        file_uuids=inherited.file_uuids,
        collection_uuids=inherited.collection_uuids,
        tag_names=inherited.tag_names,
        speakers=conv_scope.speakers or inherited.speakers,
    )


def conversation_settings(conversation: ChatConversation) -> ConversationSettings:
    """Per-conversation overrides as a schema object (empty when unset)."""
    raw = conversation.settings or {}
    return ConversationSettings(
        use_context=raw.get("use_context"),
        system_prompt=raw.get("system_prompt"),
        temperature=raw.get("temperature"),
        max_tokens=raw.get("max_tokens"),
        top_p=raw.get("top_p"),
        search_mode=raw.get("search_mode"),
    )


def read_user_chat_settings(db: Session, user_id: int) -> dict:
    """Load the user's chat preferences, with coded defaults for unset keys."""
    from app import models

    rows = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == user_id,
            models.UserSetting.setting_key.in_(list(USER_SETTING_KEYS.values())),
        )
        .all()
    )
    stored = {str(row.setting_key): str(row.setting_value) for row in rows}

    use_context_raw = stored.get(USER_SETTING_KEYS["use_context_default"])
    return {
        "system_prompt": stored.get(
            USER_SETTING_KEYS["system_prompt"], C.DEFAULT_CHAT_SYSTEM_PROMPT
        ),
        "use_context_default": (
            C.DEFAULT_CHAT_USE_CONTEXT
            if use_context_raw is None
            else use_context_raw.lower() in ("true", "1", "yes", "on")
        ),
        "default_search_mode": stored.get(
            USER_SETTING_KEYS["default_search_mode"], C.DEFAULT_CHAT_SEARCH_MODE
        ),
    }


def resolve_use_context(conversation: ChatConversation, user_defaults: dict) -> bool:
    """Per-conversation toggle when set, otherwise the user's default.

    Forced to True when the ``chat.ungrounded`` capability is off. Ungrounded chat
    is the one switch that turns a grounded transcript assistant into a
    general-purpose chatbot, so an edition may withhold it without disabling chat.
    Degrading to grounded (rather than rejecting the request) keeps the feature
    usable: the user still gets an answer, just one anchored to their transcripts.

    Enforced here rather than in the UI because the toggle is user-supplied
    (``conversation.settings``), so a client that skips the control would otherwise
    set it freely.
    """
    from app.core.capabilities import capability_enabled

    raw = (conversation.settings or {}).get("use_context")
    resolved = bool(user_defaults["use_context_default"]) if raw is None else bool(raw)
    if not resolved and not capability_enabled("chat.ungrounded"):
        return True
    return resolved


def to_summary(conversation: ChatConversation, message_count: int = 0) -> ConversationSummary:
    return ConversationSummary(
        uuid=str(conversation.uuid),
        title=conversation.title,
        is_archived=bool(conversation.is_archived),
        last_message_at=conversation.last_message_at,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        message_count=message_count,
        project_uuid=str(conversation.project.uuid) if conversation.project else None,
    )


def to_detail(
    db: Session,
    conversation: ChatConversation,
    user_defaults: dict,
    message_count: int = 0,
) -> ConversationDetail:
    """Full conversation record, including the resolved use-context value."""
    llm_config_uuid = None
    if conversation.llm_config_id is not None:
        from app.models.user_llm_settings import UserLLMSettings

        row = (
            db.query(UserLLMSettings.uuid)
            .filter(UserLLMSettings.id == conversation.llm_config_id)
            .first()
        )
        if row is not None:
            llm_config_uuid = str(row[0])

    return ConversationDetail(
        uuid=str(conversation.uuid),
        title=conversation.title,
        is_archived=bool(conversation.is_archived),
        last_message_at=conversation.last_message_at,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        message_count=message_count,
        project_uuid=str(conversation.project.uuid) if conversation.project else None,
        scope=ChatScope(**conversation.scope),
        settings=conversation_settings(conversation),
        llm_config_uuid=llm_config_uuid,
        use_context=resolve_use_context(conversation, user_defaults),
    )


def resolve_llm_config_id(db: Session, user_id: int, llm_config_uuid: str | None) -> int | None:
    """Map a user-visible LLM config uuid to its row id, if the caller may use it."""
    if not llm_config_uuid:
        return None

    from sqlalchemy import or_

    from app.models.user_llm_settings import UserLLMSettings

    row = (
        db.query(UserLLMSettings.id)
        .filter(
            UserLLMSettings.uuid == llm_config_uuid,
            or_(
                UserLLMSettings.user_id == user_id,
                UserLLMSettings.is_shared == True,  # noqa: E712
            ),
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="LLM configuration not found")
    return int(row[0])
