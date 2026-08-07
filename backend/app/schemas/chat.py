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
    """Which transcripts a conversation may retrieve from (empty = all accessible).

    ``speakers`` is a different axis from the other three: files, collections and
    tags choose WHICH RECORDINGS to search, while speakers narrow to WHO WAS
    TALKING within them. Because chunks are speaker turns, a speaker filter is
    exact — "what did Dana say about pricing" retrieves only Dana's words.
    """

    file_uuids: list[str] = Field(default_factory=list, max_length=100)
    collection_uuids: list[str] = Field(default_factory=list, max_length=20)
    tag_names: list[str] = Field(default_factory=list, max_length=20)
    speakers: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("file_uuids", "collection_uuids")
    @classmethod
    def _check_uuids(cls, v: list[str]) -> list[str]:
        return _validate_uuid_list(v)

    @field_validator("tag_names", "speakers")
    @classmethod
    def _check_names(cls, v: list[str]) -> list[str]:
        for name in v:
            if not name.strip() or len(name) > 200:
                raise ValueError("Names must be 1-200 characters")
        return v

    @property
    def is_empty(self) -> bool:
        """Whether the RECORDING scope is unset (speakers are a separate axis).

        Speakers deliberately do not count here: "everything Dana said, across
        all my recordings" is a valid and useful scope, and it still resolves
        the file set to "all accessible".
        """
        return not (self.file_uuids or self.collection_uuids or self.tag_names)


class ConversationSettings(BaseModel):
    """Per-conversation overrides (None = inherit the user's default).

    ``max_tokens`` and ``top_p`` are validated for shape only. The real ceiling
    is the model's context window and any per-tenant cap, neither of which is
    knowable here — both are applied when the turn is assembled.
    """

    use_context: bool | None = None
    system_prompt: str | None = Field(default=None, max_length=MAX_SYSTEM_PROMPT_CHARS)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=256, le=200_000)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    search_mode: SearchMode | None = None


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=255)
    scope: ChatScope = Field(default_factory=ChatScope)
    llm_config_uuid: str | None = None
    settings: ConversationSettings | None = None
    # Joining a project makes the conversation inherit its scope and prompt layer.
    project_uuid: str | None = None


class ConversationUpdate(BaseModel):
    """PATCH body — only provided fields are applied."""

    title: str | None = Field(default=None, max_length=255)
    is_archived: bool | None = None
    scope: ChatScope | None = None
    llm_config_uuid: str | None = None
    settings: ConversationSettings | None = None
    # "" moves the conversation out to ungrouped; None leaves it where it is.
    project_uuid: str | None = None


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
    # NULL = ungrouped. The sidebar groups on this.
    project_uuid: str | None = None


class ConversationDetail(ConversationSummary):
    # Pydantic v2 deep-copies model defaults per instance, so a bare instance is
    # safe here and keeps mypy happy (default_factory over a BaseModel subclass
    # trips the plugin's overload resolution).
    scope: ChatScope = ChatScope()
    settings: ConversationSettings = ConversationSettings()
    llm_config_uuid: str | None = None
    use_context: bool = True  # resolved value (conversation override ∪ user default)


class ConversationList(BaseModel):
    conversations: list[ConversationSummary]
    total: int
    limit: int
    offset: int


class ProjectCreate(BaseModel):
    """A new project. Everything except the name is optional."""

    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    system_prompt: str | None = Field(default=None, max_length=MAX_SYSTEM_PROMPT_CHARS)
    scope: ChatScope = ChatScope()
    llm_config_uuid: str | None = None


class ProjectUpdate(BaseModel):
    """PATCH body — only provided fields are applied."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    system_prompt: str | None = Field(default=None, max_length=MAX_SYSTEM_PROMPT_CHARS)
    scope: ChatScope | None = None
    llm_config_uuid: str | None = None
    is_archived: bool | None = None


class ProjectSummary(BaseModel):
    """Sidebar group header."""

    model_config = ConfigDict(from_attributes=True)

    uuid: str
    name: str
    description: str | None = None
    is_archived: bool = False
    conversation_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ProjectDetail(ProjectSummary):
    system_prompt: str | None = None
    scope: ChatScope = ChatScope()
    llm_config_uuid: str | None = None
    # True when the project pins any recordings, i.e. new chats inherit a scope
    # rather than searching everything. Surfaced so the UI can say so plainly.
    has_scope: bool = False


class ProjectList(BaseModel):
    projects: list[ProjectSummary]
    total: int


class MessageList(BaseModel):
    messages: list[ChatMessageOut]
    total: int
    limit: int
    offset: int


class MessageCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=MAX_MESSAGE_CHARS)
    search_mode: SearchMode | None = None


class ChatUserSettings(BaseModel):
    """Per-user chat defaults (``GET/PUT /user-settings/chat``).

    ``final_chunks`` and ``rerank_enabled`` are preferences that may only ever
    TIGHTEN the admin's platform values — a user can make their own chats
    cheaper and faster, never more expensive than the operator permits. ``None``
    inherits. The clamp itself lives in ``apply_user_preferences``; the ceiling
    is not knowable here.
    """

    system_prompt: str = Field("", max_length=MAX_SYSTEM_PROMPT_CHARS)
    use_context_default: bool = True
    default_search_mode: SearchMode = "hybrid"
    final_chunks: int | None = Field(default=None, ge=1, le=50)
    rerank_enabled: bool | None = None


class ChatUserSettingsUpdate(BaseModel):
    system_prompt: str | None = Field(default=None, max_length=MAX_SYSTEM_PROMPT_CHARS)
    use_context_default: bool | None = None
    default_search_mode: SearchMode | None = None
    final_chunks: int | None = Field(default=None, ge=1, le=50)
    rerank_enabled: bool | None = None


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
    candidate_pool: int | None = Field(default=None, ge=1, le=500)
    final_chunks: int | None = Field(default=None, ge=1, le=100)
    max_chunks_per_file: int | None = Field(default=None, ge=1, le=50)
    rerank_enabled: bool | None = None
    rerank_max_pairs: int | None = Field(default=None, ge=1, le=500)
    query_rewrite_enabled: bool | None = None
    cache_ttl_seconds: int | None = Field(default=None, ge=0, le=86400)
    semantic_cache_enabled: bool | None = None
    semantic_cache_threshold: float | None = Field(default=None, ge=0.5, le=1.0)
    history_max_turns: int | None = Field(default=None, ge=1, le=50)
    messages_per_hour: int | None = Field(default=None, ge=1, le=10000)
    max_concurrent_streams: int | None = Field(default=None, ge=1, le=20)
    retention_days: int | None = Field(default=None, ge=0, le=3650)
