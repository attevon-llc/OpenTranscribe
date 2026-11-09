"""Chat conversation, message, project and chat-preference queries behind the chat endpoints.

Lives beside the chat service package rather than inside it. Every function takes the
request's ``Session`` and plain arguments; nothing here commits. The routers decide the
HTTP status for a missing row.
"""

from collections.abc import Collection

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import models
from app.models.chat import ROLE_ASSISTANT
from app.models.chat import ROLE_USER
from app.models.chat import STATUS_SUPERSEDED
from app.models.chat import ChatConversation
from app.models.chat import ChatMessage
from app.models.chat import ChatProject
from app.models.user_llm_settings import UserLLMSettings
from app.utils.db_helpers import org_stamp_is
from app.utils.sql_like import escape_like
from app.utils.tenant_sharing import shared_visible_in_tenant

# --- conversations -----------------------------------------------------------------------


def find_user_conversation(
    db: Session, conversation_uuid: str, user_id: int
) -> ChatConversation | None:
    """The user's conversation with this uuid in any tenant, or None."""
    return (
        db.query(ChatConversation)
        .filter(
            ChatConversation.uuid == conversation_uuid,
            ChatConversation.user_id == user_id,
        )
        .first()
    )


def message_counts(db: Session, conversation_ids: list[int]) -> dict[int, int]:
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


def list_conversation_page(
    db: Session,
    *,
    user_id: int,
    organization_id: int | None,
    archived: bool,
    title_filter: str | None,
    limit: int,
    offset: int,
) -> tuple[list[ChatConversation], int]:
    """One page of the user's conversations in a tenant, most recently active first.

    Returns ``(rows, total)`` where ``total`` ignores paging. A conversation that has never
    been messaged sorts by ``created_at`` so a brand-new chat stays on page one.
    """
    query = db.query(ChatConversation).filter(
        ChatConversation.user_id == user_id,
        ChatConversation.is_archived.is_(archived),
    )
    query = (
        query.filter(ChatConversation.organization_id == organization_id)
        if organization_id is not None
        else query.filter(ChatConversation.organization_id.is_(None))
    )
    if title_filter:
        query = query.filter(
            ChatConversation.title.ilike(f"%{escape_like(title_filter)}%", escape="\\")
        )

    total = query.count()
    recency = func.coalesce(ChatConversation.last_message_at, ChatConversation.created_at)
    rows = (
        query.order_by(recency.desc(), ChatConversation.id.desc()).offset(offset).limit(limit).all()
    )
    return rows, total


def llm_config_uuid_for_id(db: Session, llm_config_id: int) -> str | None:
    """The public uuid of an LLM configuration row, or None when it no longer exists."""
    row = db.query(UserLLMSettings.uuid).filter(UserLLMSettings.id == llm_config_id).first()
    return str(row[0]) if row is not None else None


def usable_llm_config_id(
    db: Session, llm_config_uuid: str, user_id: int, organization_id: int | None
) -> int | None:
    """Row id of an LLM configuration the user owns or that is shared in their tenant."""
    row = (
        db.query(UserLLMSettings.id)
        .filter(
            UserLLMSettings.uuid == llm_config_uuid,
            shared_visible_in_tenant(
                UserLLMSettings.user_id, UserLLMSettings.is_shared, user_id, organization_id
            ),
        )
        .first()
    )
    return int(row[0]) if row is not None else None


# --- messages ----------------------------------------------------------------------------


def visible_messages(db: Session, conversation_id: int) -> list[ChatMessage]:
    """Every non-superseded message of a conversation, oldest first."""
    return (
        db.query(ChatMessage)
        .filter(
            ChatMessage.conversation_id == conversation_id,
            ChatMessage.status != STATUS_SUPERSEDED,
        )
        .order_by(ChatMessage.id.asc())
        .all()
    )


def recent_history(db: Session, conversation_id: int, max_turns: int) -> list[ChatMessage]:
    """The newest ``max_turns * 2`` messages that are neither superseded nor errors, newest first."""
    return (
        db.query(ChatMessage)
        .filter(
            ChatMessage.conversation_id == conversation_id,
            ChatMessage.status.notin_([STATUS_SUPERSEDED, "error"]),
        )
        .order_by(ChatMessage.id.desc())
        .limit(max_turns * 2)
        .all()
    )


def message_page(
    db: Session, conversation_id: int, *, offset: int, limit: int
) -> tuple[list[ChatMessage], int]:
    """``(rows, total)`` for a conversation's messages, oldest first, superseded ones included."""
    query = db.query(ChatMessage).filter(ChatMessage.conversation_id == conversation_id)
    total = query.count()
    rows = query.order_by(ChatMessage.id.asc()).offset(offset).limit(limit).all()
    return rows, total


def last_user_message(db: Session, conversation_id: int) -> ChatMessage | None:
    """The conversation's newest user message."""
    return (
        db.query(ChatMessage)
        .filter(
            ChatMessage.conversation_id == conversation_id,
            ChatMessage.role == ROLE_USER,
        )
        .order_by(ChatMessage.id.desc())
        .first()
    )


def supersede_assistant_replies_after(db: Session, conversation_id: int, message_id: int) -> None:
    """Mark every assistant message after ``message_id`` superseded (bulk UPDATE, no commit)."""
    db.query(ChatMessage).filter(
        ChatMessage.conversation_id == conversation_id,
        ChatMessage.role == ROLE_ASSISTANT,
        ChatMessage.id > message_id,
    ).update({ChatMessage.status: STATUS_SUPERSEDED}, synchronize_session=False)


def find_user_message(db: Session, conversation_id: int, message_uuid: str) -> ChatMessage | None:
    """A user-role message of the conversation by uuid, or None."""
    return (
        db.query(ChatMessage)
        .filter(
            ChatMessage.uuid == message_uuid,
            ChatMessage.conversation_id == conversation_id,
            ChatMessage.role == ROLE_USER,
        )
        .first()
    )


def newest_message_id(db: Session, conversation_id: int) -> int | None:
    """Highest message id in the conversation, or None when it has none."""
    result: int | None = (
        db.query(func.max(ChatMessage.id))
        .filter(ChatMessage.conversation_id == conversation_id)
        .scalar()
    )
    return result


def supersede_message_range(
    db: Session, conversation_id: int, *, first_id: int, last_id: int | None
) -> None:
    """Mark messages with ``first_id <= id <= last_id`` superseded (bulk UPDATE, no commit)."""
    db.query(ChatMessage).filter(
        ChatMessage.conversation_id == conversation_id,
        ChatMessage.id >= first_id,
        ChatMessage.id <= last_id,
    ).update({ChatMessage.status: STATUS_SUPERSEDED}, synchronize_session=False)


def message_owned_in_tenant(
    db: Session, message_uuid: object, user_id: int, organization_id: int | None
) -> bool:
    """Whether the message belongs to one of the user's conversations in the tenant."""
    return (
        db.query(ChatMessage.id)
        .join(ChatConversation, ChatConversation.id == ChatMessage.conversation_id)
        .filter(
            ChatMessage.uuid == message_uuid,
            ChatConversation.user_id == user_id,
            org_stamp_is(ChatConversation.organization_id, organization_id),
        )
        .first()
        is not None
    )


# --- projects ----------------------------------------------------------------------------


def find_user_project(db: Session, project_uuid: str, user_id: int) -> ChatProject | None:
    """The user's chat project with this uuid in any tenant, or None."""
    return (
        db.query(ChatProject)
        .filter(ChatProject.uuid == project_uuid, ChatProject.user_id == user_id)
        .first()
    )


def conversation_counts(db: Session, project_ids: list[int]) -> dict[int, int]:
    """Conversation counts for a page of projects, in one query."""
    if not project_ids:
        return {}
    rows = (
        db.query(ChatConversation.project_id, func.count(ChatConversation.id))
        .filter(ChatConversation.project_id.in_(project_ids))
        .group_by(ChatConversation.project_id)
        .all()
    )
    return {int(pid): int(count) for pid, count in rows if pid is not None}


def list_user_projects(db: Session, user_id: int, *, include_archived: bool) -> list[ChatProject]:
    """The user's projects across tenants, alphabetical (callers filter the tenant)."""
    query = db.query(ChatProject).filter(ChatProject.user_id == user_id)
    if not include_archived:
        query = query.filter(ChatProject.is_archived.is_(False))
    return query.order_by(func.lower(ChatProject.name)).all()


def detach_conversations(db: Session, project_id: int) -> None:
    """Clear ``project_id`` on the project's conversations (bulk UPDATE, no commit)."""
    db.query(ChatConversation).filter(ChatConversation.project_id == project_id).update(
        {ChatConversation.project_id: None}, synchronize_session=False
    )


# --- preferences -------------------------------------------------------------------------


def setting_rows(
    db: Session, user_id: int, setting_keys: Collection[str]
) -> list[models.UserSetting]:
    """The user's stored settings among ``setting_keys``."""
    return (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == user_id,
            models.UserSetting.setting_key.in_(list(setting_keys)),
        )
        .all()
    )


def delete_settings(db: Session, user_id: int, setting_keys: Collection[str]) -> None:
    """Delete the user's stored settings among ``setting_keys`` (no commit)."""
    db.query(models.UserSetting).filter(
        models.UserSetting.user_id == user_id,
        models.UserSetting.setting_key.in_(list(setting_keys)),
    ).delete(synchronize_session=False)
