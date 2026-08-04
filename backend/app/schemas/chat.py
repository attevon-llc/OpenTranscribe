"""Wire contracts for the RAG chat API (issue #52).

The scope shape ``{file_uuids, collection_uuids, tag_names}`` appears in the
conversation record, the create/patch bodies and the context estimator, so it is
defined once here and reused. An all-empty scope means "every transcript I can
access" — authorization is still enforced at query time, never by the scope.

Caps are deliberate abuse controls, not UI conveniences: they bound both the
Postgres scope resolution and the OpenSearch ``terms`` filter it produces.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator

SearchMode = Literal["hybrid", "semantic", "keyword"]

MAX_MESSAGE_CHARS = 8000
MAX_SYSTEM_PROMPT_CHARS = 2000

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _validate_uuid_list(values: list[str]) -> list[str]:
    """Reject anything that is not a canonical UUID before it reaches a query."""
    for value in values:
        if not _UUID_RE.match(value):
            raise ValueError(f"Invalid UUID: {value!r}")
    return values


class ChatScope(BaseModel):
    """Which transcripts a conversation may retrieve from (empty = all accessible)."""

    file_uuids: list[str] = Field(default_factory=list, max_length=100)
    collection_uuids: list[str] = Field(default_factory=list, max_length=20)
    tag_names: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("file_uuids", "collection_uuids")
    @classmethod
    def _check_uuids(cls, v: list[str]) -> list[str]:
        return _validate_uuid_list(v)

    @field_validator("tag_names")
    @classmethod
    def _check_tags(cls, v: list[str]) -> list[str]:
        for name in v:
            if not name.strip() or len(name) > 100:
                raise ValueError("Tag names must be 1-100 characters")
        return v

    @property
    def is_empty(self) -> bool:
        return not (self.file_uuids or self.collection_uuids or self.tag_names)


class ConversationSettings(BaseModel):
    """Per-conversation overrides (None = inherit the user's default)."""

    use_context: bool | None = None
    system_prompt: str | None = Field(None, max_length=MAX_SYSTEM_PROMPT_CHARS)
    temperature: float | None = Field(None, ge=0.0, le=2.0)
    search_mode: SearchMode | None = None


class ConversationCreate(BaseModel):
    title: str | None = Field(None, max_length=255)
    scope: ChatScope = Field(default_factory=ChatScope)
    llm_config_uuid: str | None = None
    settings: ConversationSettings | None = None


class ConversationUpdate(BaseModel):
    """PATCH body — only provided fields are applied."""

    title: str | None = Field(None, max_length=255)
    is_archived: bool | None = None
    scope: ChatScope | None = None
    llm_config_uuid: str | None = None
    settings: ConversationSettings | None = None


class Citation(BaseModel):
    """One retrieved transcript excerpt the answer may reference as ``[n]``.

    ``snippet`` is stored/returned post-masking, matching what was sent to the LLM.
    """

    id: int
    file_uuid: str
    title: str = ""
    chunk_index: int = 0
    start_time: float = 0.0
    end_time: float | None = None
    speaker: str | None = None
    snippet: str = ""


class ChatMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    uuid: str
    role: str
    content: str
    citations: list[Citation] | None = None
    msg_metadata: dict[str, Any] | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    tokens_estimated: bool = False
    provider: str | None = None
    model: str | None = None
    status: str = "complete"
    error: str | None = None
    created_at: datetime | None = None


class ConversationSummary(BaseModel):
    """Sidebar row — no messages."""

    model_config = ConfigDict(from_attributes=True)

    uuid: str
    title: str | None = None
    is_archived: bool = False
    last_message_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    message_count: int = 0


class ConversationDetail(ConversationSummary):
    scope: ChatScope = Field(default_factory=ChatScope)
    settings: ConversationSettings = Field(default_factory=ConversationSettings)
    llm_config_uuid: str | None = None
    use_context: bool = True  # resolved value (conversation override ∪ user default)


class ConversationList(BaseModel):
    conversations: list[ConversationSummary]
    total: int
    limit: int
    offset: int


class MessageList(BaseModel):
    messages: list[ChatMessageOut]
    total: int
    limit: int
    offset: int


class MessageCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=MAX_MESSAGE_CHARS)
    search_mode: SearchMode | None = None


class ChatUserSettings(BaseModel):
    """Per-user chat defaults (``GET/PUT /user-settings/chat``)."""

    system_prompt: str = Field("", max_length=MAX_SYSTEM_PROMPT_CHARS)
    use_context_default: bool = True
    default_search_mode: SearchMode = "hybrid"


class ChatUserSettingsUpdate(BaseModel):
    system_prompt: str | None = Field(None, max_length=MAX_SYSTEM_PROMPT_CHARS)
    use_context_default: bool | None = None
    default_search_mode: SearchMode | None = None


class ContextEstimate(BaseModel):
    """How much of the model's context window a scope would occupy."""

    file_count: int
    estimated_tokens: int
    context_window: int
    pct: float
    warning_level: Literal["ok", "warn", "over"] = "ok"


class ChatAdminSettings(BaseModel):
    """Platform-admin RAG knobs (all DB-backed SystemSettings; no ``.env`` vars)."""

    candidate_pool: int = Field(48, ge=1, le=500)
    final_chunks: int = Field(12, ge=1, le=100)
    max_chunks_per_file: int = Field(4, ge=1, le=50)
    rerank_enabled: bool = True
    rerank_max_pairs: int = Field(50, ge=1, le=500)
    query_rewrite_enabled: bool = True
    cache_ttl_seconds: int = Field(300, ge=0, le=86400)
    semantic_cache_enabled: bool = False
    semantic_cache_threshold: float = Field(0.97, ge=0.5, le=1.0)
    history_max_turns: int = Field(10, ge=1, le=50)
    messages_per_hour: int = Field(120, ge=1, le=10000)
    max_concurrent_streams: int = Field(2, ge=1, le=20)
    retention_days: int = Field(0, ge=0, le=3650)


class ChatAdminSettingsUpdate(BaseModel):
    candidate_pool: int | None = Field(None, ge=1, le=500)
    final_chunks: int | None = Field(None, ge=1, le=100)
    max_chunks_per_file: int | None = Field(None, ge=1, le=50)
    rerank_enabled: bool | None = None
    rerank_max_pairs: int | None = Field(None, ge=1, le=500)
    query_rewrite_enabled: bool | None = None
    cache_ttl_seconds: int | None = Field(None, ge=0, le=86400)
    semantic_cache_enabled: bool | None = None
    semantic_cache_threshold: float | None = Field(None, ge=0.5, le=1.0)
    history_max_turns: int | None = Field(None, ge=1, le=50)
    messages_per_hour: int | None = Field(None, ge=1, le=10000)
    max_concurrent_streams: int | None = Field(None, ge=1, le=20)
    retention_days: int | None = Field(None, ge=0, le=3650)
