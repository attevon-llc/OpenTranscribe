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


def conversation_settings(conversation: ChatConversation) -> ConversationSettings:
    """Per-conversation overrides as a schema object (empty when unset)."""
    raw = conversation.settings or {}
    return ConversationSettings(
        use_context=raw.get("use_context"),
        system_prompt=raw.get("system_prompt"),
        temperature=raw.get("temperature"),
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
    """Per-conversation toggle when set, otherwise the user's default."""
    raw = (conversation.settings or {}).get("use_context")
    if raw is None:
        return bool(user_defaults["use_context_default"])
    return bool(raw)


def to_summary(conversation: ChatConversation, message_count: int = 0) -> ConversationSummary:
    return ConversationSummary(
        uuid=str(conversation.uuid),
        title=conversation.title,
        is_archived=bool(conversation.is_archived),
        last_message_at=conversation.last_message_at,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        message_count=message_count,
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
