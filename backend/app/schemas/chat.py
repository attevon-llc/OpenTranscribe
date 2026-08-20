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
from enum import StrEnum
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


class ChatWarningCode(StrEnum):
    """Codes a ``warning`` SSE frame's ``code`` field may carry (issue #52+).

    Must mirror `frontend/src/lib/types/chat.ts`'s ``ChatWarningCode`` union
    exactly — a code present on only one side means either the server reports
    a problem nobody ever sees (missing from the TS union, silently dropped by
    the client) or the client claims to handle a code the server never sends.
    `tests/unit/test_chat_sse_contract.py` checks both directions.

    `services/chat/service.py` currently builds ``warning`` frames as plain
    dicts with a literal string ``code``, not by referencing this enum — this
    is the canonical list new callers should read from rather than inventing
    a new string, not (yet) a type the frame payload is constructed through.
    """

    CONTEXT_DROPPED = "context_dropped"
    NO_CONTEXT = "no_context"
    #: The chunk-plane search itself raised or had no OpenSearch client,
    #: distinct from `NO_CONTEXT` where the search ran and genuinely found
    #: nothing (issue #438's open half — landed on `frontend/src/lib/types/
    #: chat.ts` and in `services/search/chunk_retrieval.py` while this enum
    #: was being written; added here so the two sides agree).
    RETRIEVAL_FAILED = "retrieval_failed"
    UNSUPPORTED_LANGUAGE = "unsupported_language"
    #: A speaker filter matched more than one candidate and could not be
    #: resolved to a single person without asking (Wave 2; no emitter yet).
    AMBIGUOUS_SPEAKER = "ambiguous_speaker"
    #: A recurrence/trend block was requested but could not be produced for
    #: this scope (Wave 2; no emitter yet).
    RECURRENCE_UNAVAILABLE = "recurrence_unavailable"
    #: The router's query plan could not be built or failed to execute, and
    #: the turn fell back to an unplanned answer (Wave 2; no emitter yet).
    PLAN_FAILED = "plan_failed"
    #: The question's detected language did not match what the router
    #: expected/could route with confidence (Wave 2; no emitter yet).
    ROUTER_LANGUAGE_UNMATCHED = "router_language_unmatched"


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
    #: Whether the model should reason before answering (issue #64). ``False``
    #: is honoured **only** where a probe measured a working off-switch for the
    #: model in play (``services/llm_reasoning``); on every other model the
    #: request is built exactly as it is today, because a provider accepting the
    #: parameter is not evidence the model obeys it. Storing a preference the
    #: current model cannot honour is deliberate — it applies again if the
    #: conversation is later pointed at a model that can.
    reasoning: bool | None = None


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
    """One retrieved excerpt the answer may reference as ``[n]`` — the FULL union
    across every citation kind the RAG pipeline can produce (issue #464 amendment a).

    ⚠️ **Widened deliberately, ALL AT ONCE, rather than kind by kind.** Pydantic
    v2 silently DROPS a dict key that is not a declared field on validation (no
    ``extra="forbid"`` here, by the repo's own no-aliases-anywhere convention) —
    and ``ChatMessageOut.citations`` validates straight from the persisted
    ``chat_message.citations`` JSONB column (``models/chat.py``,
    ``Mapped[list | None]``) every time a conversation is reloaded. Before this
    widening, ``chat/citations.py.build_citation`` already put ``kind`` and
    ``digest_section`` into that dict at STREAM time (`KIND_CHUNK` /
    `KIND_DIGEST`, ``chunk.source.digest_section``) and both survived the SSE
    frame — but neither field existed here, so both were silently gone the
    moment the SAME message was read back after a reload: a digest citation the
    user saw correctly labelled mid-stream rendered as an ordinary quote after
    a refresh, with no error anywhere. A later lane's document-plane citations
    (``page``/``section_path``/``char_start``/``char_end``, #362/#403 Stage 6)
    reuse this union rather than re-triggering the same silent-drop bug and
    re-migrating every already-persisted message a second time.

    ``kind`` values in play: ``"chunk"`` (a transcript speech turn — the
    default, matching every citation minted before this field existed),
    ``"digest"`` (extractive prose spanning several turns — never a quote),
    ``"summary"`` (issue #464 — LLM-generated prose about the recording; a
    LABELLED INTERPRETATION, never a quote, and never attributed to a
    speaker), ``"recurrence"`` (W2.5 — a group of items judged the same thing
    recurring across MULTIPLE recordings; the UI seam is built ahead of the
    emitter, same as ``"document"`` was). A later lane is expected to add
    ``"document"`` onto this same field.

    ``snippet`` is stored/returned post-masking, matching what was sent to the LLM.
    """

    id: int
    #: See the kind list in the class docstring. Untyped ``str`` rather than a
    #: ``Literal``/enum on purpose: a new kind added by a later lane, in a file
    #: outside this one, must not need an edit here to stay valid.
    kind: str = "chunk"
    #: The PRIMARY file — for ``"recurrence"``, the first entry of
    #: :attr:`file_uuids` (never empty when that field is set), so a reader
    #: that only knows the old single-file contract still gets a valid uuid
    #: rather than an empty string.
    file_uuid: str
    title: str = ""
    chunk_index: int = 0
    #: ``None`` for a kind with no natural single timestamp (``"summary"``, or
    #: a multi-file ``"recurrence"`` citation) — the absence is a first-class
    #: case here for the same reason the frontend ``ChatSource`` type makes it
    #: optional, not a ``0`` sentinel a client would render as 0:00.
    start_time: float | None = 0.0
    end_time: float | None = None
    speaker: str | None = None
    snippet: str = ""
    #: Set for ``kind in ("digest", "summary")`` — which extractive section (or,
    #: for a summary citation, the sentinel index ``chat/mapreduce.
    #: scope_digest_hits`` uses) the citation draws from. ``None`` for a chunk.
    digest_section: int | None = None
    #: Document-plane fields (a later lane, #362/#403 Stage 6). ``None`` for
    #: every kind this lane emits.
    page: int | None = None
    section_path: str | None = None
    char_start: int | None = None
    char_end: int | None = None
    #: W2.5, ``kind == "recurrence"`` ONLY. Every recording the recurring
    #: group spans (``RecurrenceGroup.file_uuids``) — ``None`` for every other
    #: kind. A recurrence group is not one person's words in one place, so it
    #: cannot be represented by ``file_uuid`` alone the way a chunk/digest/
    #: summary citation is; this is the field a later lane's emitter fills in.
    file_uuids: list[str] | None = None


class ChatMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    uuid: str
    role: str
    content: str
    # A provider's separately-streamed reasoning/"thinking" text (v384). None for
    # user messages and for any assistant reply whose provider never streamed one.
    reasoning_content: str | None = None
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
    speaker_facet_content_scope: bool = False
    speaker_stats_enabled: bool = False
    map_tier_summaries: bool = False
    speaker_resolver_enabled: bool = False
    map_tier_speaker_summaries: bool = False
    recurrence_enabled: bool = False
    planner_enabled: bool = False
    planner_max_parallel_legs: int = Field(4, ge=1, le=8)
    enrichment_enabled: bool = False
    context_expansion_enabled: bool = False


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
    speaker_facet_content_scope: bool | None = None
    speaker_stats_enabled: bool | None = None
    map_tier_summaries: bool | None = None
    speaker_resolver_enabled: bool | None = None
    map_tier_speaker_summaries: bool | None = None
    recurrence_enabled: bool | None = None
    planner_enabled: bool | None = None
    planner_max_parallel_legs: int | None = Field(default=None, ge=1, le=8)
    enrichment_enabled: bool | None = None
    context_expansion_enabled: bool | None = None
