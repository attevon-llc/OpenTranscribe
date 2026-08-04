"""RAG chat models — conversations and their messages (issue #52).

Conversations are strictly private to their creator: ``organization_id`` is the
tenancy/billing stamp (v372/v373 pattern), never a sharing surface. Messages
inherit authorization from their conversation, so every lookup goes through an
ownership-checked conversation join rather than querying ``chat_message`` directly.
"""

import uuid as uuid_pkg
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger
from sqlalchemy import Boolean
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.db.base import Base
from app.utils.uuid7 import uuid7

# Message roles / lifecycle states, kept as plain strings to match the VARCHAR
# columns (the schema deliberately avoids native enums — see v073).
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"

STATUS_STREAMING = "streaming"
STATUS_COMPLETE = "complete"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"
STATUS_SUPERSEDED = "superseded"  # replaced by a regenerated answer


class ChatConversation(Base):
    """One chat thread: its pinned transcript scope, settings and message list."""

    __tablename__ = "chat_conversation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("organization.id"), nullable=True, index=True
    )
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Pinned retrieval scope: {"file_uuids": [], "collection_uuids": [], "tag_names": []}
    # All-empty means "every transcript I can access" (authz still applies at query time).
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    llm_config_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("user_llm_settings.id", ondelete="SET NULL"), nullable=True
    )
    # Per-conversation overrides:
    # {"use_context": bool, "system_prompt": str|None, "temperature": float|None,
    #  "search_mode": "hybrid"|"semantic"|"keyword"|None}
    settings: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    messages: Mapped[list["ChatMessage"]] = relationship(
        "ChatMessage",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="ChatMessage.id",
    )

    @property
    def scope(self) -> dict[str, list]:
        """Pinned scope with all three keys present (empty lists when unset)."""
        raw = self.context or {}
        return {
            "file_uuids": list(raw.get("file_uuids") or []),
            "collection_uuids": list(raw.get("collection_uuids") or []),
            "tag_names": list(raw.get("tag_names") or []),
        }


class ChatMessage(Base):
    """One turn in a conversation, with its citations and token accounting.

    Assistant ``content`` and citation snippets are stored post-masking: when
    redact-before-LLM applies, the masked text is what was sent to the provider
    and therefore what the thread should replay.
    """

    __tablename__ = "chat_message"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, index=True)
    uuid: Mapped[uuid_pkg.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid7, index=True
    )
    conversation_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("chat_conversation.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # [{id, file_uuid, title, chunk_index, start_time, end_time, speaker, snippet}]
    citations: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    # Diagnostics only — ids, counts and timings; never prompt/answer text.
    msg_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_estimated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=STATUS_COMPLETE)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    conversation: Mapped["ChatConversation"] = relationship(
        "ChatConversation", back_populates="messages"
    )
