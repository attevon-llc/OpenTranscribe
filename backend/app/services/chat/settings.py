"""Admin-tunable RAG chat settings, resolved in one query.

Every knob is a DB-backed ``SystemSettings`` row with a coded default in
``app.core.constants`` — there are deliberately no ``.env`` vars for chat, so an
operator can retune retrieval from the admin UI without a restart.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import replace

from sqlalchemy.orm import Session

from app.core import constants as C  # noqa: N812

logger = logging.getLogger(__name__)

# SystemSettings key ↔ dataclass field.
KEY_PREFIX = "chat."
SETTING_KEYS: dict[str, str] = {
    "candidate_pool": "chat.rag.candidate_pool",
    "final_chunks": "chat.rag.final_chunks",
    "max_chunks_per_file": "chat.rag.max_chunks_per_file",
    "rerank_enabled": "chat.rag.rerank_enabled",
    "rerank_max_pairs": "chat.rag.rerank_max_pairs",
    "query_rewrite_enabled": "chat.rag.query_rewrite_enabled",
    "cache_ttl_seconds": "chat.rag.cache_ttl_seconds",
    "semantic_cache_enabled": "chat.rag.semantic_cache_enabled",
    "semantic_cache_threshold": "chat.rag.semantic_cache_threshold",
    "history_max_turns": "chat.history_max_turns",
    "messages_per_hour": "chat.limits.messages_per_hour",
    "max_concurrent_streams": "chat.limits.max_concurrent_streams",
    "retention_days": "chat.retention_days",
    "speaker_facet_content_scope": "chat.aggregate.speaker_facet_content_scope",
    "speaker_stats_enabled": "chat.aggregate.speaker_stats_enabled",
    "map_tier_summaries": "chat.rag.map_tier_summaries",
    "speaker_resolver_enabled": "chat.speaker_resolver_enabled",
}

DEFAULTS: dict[str, int | bool | float] = {
    "candidate_pool": C.DEFAULT_CHAT_RAG_CANDIDATE_POOL,
    "final_chunks": C.DEFAULT_CHAT_RAG_FINAL_CHUNKS,
    "max_chunks_per_file": C.DEFAULT_CHAT_RAG_MAX_CHUNKS_PER_FILE,
    "rerank_enabled": C.DEFAULT_CHAT_RAG_RERANK_ENABLED,
    "rerank_max_pairs": C.DEFAULT_CHAT_RAG_RERANK_MAX_PAIRS,
    "query_rewrite_enabled": C.DEFAULT_CHAT_RAG_QUERY_REWRITE_ENABLED,
    "cache_ttl_seconds": C.DEFAULT_CHAT_RAG_CACHE_TTL_SECONDS,
    "semantic_cache_enabled": C.DEFAULT_CHAT_RAG_SEMANTIC_CACHE_ENABLED,
    "semantic_cache_threshold": C.DEFAULT_CHAT_RAG_SEMANTIC_CACHE_THRESHOLD,
    "history_max_turns": C.DEFAULT_CHAT_HISTORY_MAX_TURNS,
    "messages_per_hour": C.DEFAULT_CHAT_MESSAGES_PER_HOUR,
    "max_concurrent_streams": C.DEFAULT_CHAT_MAX_CONCURRENT_STREAMS,
    "retention_days": C.DEFAULT_CHAT_RETENTION_DAYS,
    "speaker_facet_content_scope": C.DEFAULT_CHAT_AGGREGATE_SPEAKER_FACET_CONTENT_SCOPE,
    "speaker_stats_enabled": C.DEFAULT_CHAT_AGGREGATE_SPEAKER_STATS_ENABLED,
    "map_tier_summaries": C.DEFAULT_CHAT_MAP_TIER_SUMMARIES,
    "speaker_resolver_enabled": C.DEFAULT_CHAT_SPEAKER_RESOLVER_ENABLED,
}


@dataclass(frozen=True)
class ChatSettings:
    """Resolved RAG knobs for one request."""

    candidate_pool: int = C.DEFAULT_CHAT_RAG_CANDIDATE_POOL
    final_chunks: int = C.DEFAULT_CHAT_RAG_FINAL_CHUNKS
    max_chunks_per_file: int = C.DEFAULT_CHAT_RAG_MAX_CHUNKS_PER_FILE
    rerank_enabled: bool = C.DEFAULT_CHAT_RAG_RERANK_ENABLED
    rerank_max_pairs: int = C.DEFAULT_CHAT_RAG_RERANK_MAX_PAIRS
    query_rewrite_enabled: bool = C.DEFAULT_CHAT_RAG_QUERY_REWRITE_ENABLED
    cache_ttl_seconds: int = C.DEFAULT_CHAT_RAG_CACHE_TTL_SECONDS
    semantic_cache_enabled: bool = C.DEFAULT_CHAT_RAG_SEMANTIC_CACHE_ENABLED
    semantic_cache_threshold: float = C.DEFAULT_CHAT_RAG_SEMANTIC_CACHE_THRESHOLD
    history_max_turns: int = C.DEFAULT_CHAT_HISTORY_MAX_TURNS
    messages_per_hour: int = C.DEFAULT_CHAT_MESSAGES_PER_HOUR
    max_concurrent_streams: int = C.DEFAULT_CHAT_MAX_CONCURRENT_STREAMS
    retention_days: int = C.DEFAULT_CHAT_RETENTION_DAYS
    #: W2.4. Score the speaker facet ("which speakers discussed X") by spoken
    #: content instead of the recording's title. Off by default: it changes
    #: what an existing mechanism answers.
    speaker_facet_content_scope: bool = C.DEFAULT_CHAT_AGGREGATE_SPEAKER_FACET_CONTENT_SCOPE
    #: W2.4. Answer "who talked the most" from exact per-speaker talk time in
    #: ``file_facts``, distinct from the attendance-style speaker facet. Off by
    #: default: a new shape, gated for rollout.
    speaker_stats_enabled: bool = C.DEFAULT_CHAT_AGGREGATE_SPEAKER_STATS_ENABLED
    #: #464. Prefer each file's LLM summary over its digest in the bounded-scope
    #: map tier (``chat/mapreduce.scope_digest_hits``) whenever the summary is
    #: FRESH — its stored ``source_fingerprint`` matches the file's current
    #: ``file_facts`` row. Off by default: on-by-default needs measured
    #: answer-quality evidence this flag does not yet have.
    map_tier_summaries: bool = C.DEFAULT_CHAT_MAP_TIER_SUMMARIES
    #: W2.2. Resolve a speaker named in the question text (e.g. "what did Dana
    #: say about pricing") against the caller's roster and, on a unique match
    #: paired with a speaker-verb frame, add a PARALLEL speaker-scoped chunk
    #: leg (`services/chat/speaker_resolver.py`). Off by default: a new,
    #: unmeasured retrieval shape.
    speaker_resolver_enabled: bool = C.DEFAULT_CHAT_SPEAKER_RESOLVER_ENABLED
    #: Ceiling on the answer, sent to the provider as max_tokens. ``None`` means
    #: "use whatever the LLM config derived", which is the community behaviour.
    max_output_tokens: int | None = None

    @property
    def revision(self) -> str:
        """Short digest of the retrieval-affecting knobs.

        Mixed into retrieval cache keys so an admin retune invalidates cached
        results instead of serving them under the old shape.
        """
        relevant = (
            self.candidate_pool,
            self.final_chunks,
            self.max_chunks_per_file,
            self.rerank_enabled,
            self.rerank_max_pairs,
        )
        digest = hashlib.sha256(repr(relevant).encode()).hexdigest()
        return digest[:12]

    def as_dict(self) -> dict:
        return asdict(self)


def _coerce(field: str, raw: str | None) -> int | bool | float:
    default = DEFAULTS[field]
    if raw is None:
        return default
    try:
        if isinstance(default, bool):
            return raw.strip().lower() in ("true", "1", "yes", "on")
        if isinstance(default, int):
            return int(raw)
        return float(raw)
    except (ValueError, TypeError):
        logger.warning("Invalid value %r for %s; using default", raw, SETTING_KEYS[field])
        return default


def get_chat_settings(db: Session) -> ChatSettings:
    """Resolve all chat knobs in a single SELECT (coded defaults for unset keys)."""
    from app.services.system_settings_service import get_settings_map

    try:
        stored = get_settings_map(db, list(SETTING_KEYS.values()))
    except Exception as exc:  # noqa: BLE001 — never fail a chat on a settings read
        logger.warning(f"Falling back to default chat settings: {exc}")
        return ChatSettings()

    values = {field: _coerce(field, stored.get(key)) for field, key in SETTING_KEYS.items()}
    return ChatSettings(**values)  # type: ignore[arg-type]


def apply_user_preferences(
    base: ChatSettings,
    *,
    final_chunks: int | None = None,
    rerank_enabled: bool | None = None,
) -> ChatSettings:
    """Narrow resolved chat settings by this user's own preferences.

    Same direction of travel as :func:`apply_tenant_limits`, and for the same
    reason: a preference may only ever **tighten** what the admin permits. A
    user can ask for fewer excerpts (faster and cheaper, at some recall) or turn
    reranking off for their own chats; they cannot raise either past the
    platform value, or the admin's cost controls would be advisory.

    Reranking is one-way on purpose: ``False`` disables it, ``True`` cannot
    enable it when the admin has it off, because the model may simply not be
    installed on that deployment.

    Args:
        base: Settings already narrowed by any tenant ceiling.
        final_chunks: The user's preferred excerpt count, or None to inherit.
        rerank_enabled: The user's reranking preference, or None to inherit.

    Returns:
        A copy of ``base`` with the preferences applied.
    """
    return replace(
        base,
        final_chunks=(
            base.final_chunks if final_chunks is None else min(base.final_chunks, final_chunks)
        ),
        rerank_enabled=(
            base.rerank_enabled
            if rerank_enabled is None
            else (base.rerank_enabled and rerank_enabled)
        ),
    )


def apply_tenant_limits(base: ChatSettings, organization_id: int | None) -> ChatSettings:
    """Narrow resolved chat settings by any per-tenant ceiling.

    A tenant limit can only ever **tighten** an admin's value, never widen it:
    every dimension takes the ``min`` of the two. That direction is the whole point
    — a cloud plan is a cap on what the deployment already permits, and a resolver
    that could raise a limit would let a tenant escape the operator's own settings.

    Returns ``base`` unchanged in the community edition, where the resolver is a
    no-op returning ``None``.
    """
    from app.core.tenant_limits import resolve_chat_limits

    limits = resolve_chat_limits(organization_id)
    if limits is None:
        return base

    def _tighten(current: int, override: int | None) -> int:
        return current if override is None else min(current, override)

    return replace(
        base,
        messages_per_hour=_tighten(base.messages_per_hour, limits.messages_per_hour),
        max_concurrent_streams=_tighten(base.max_concurrent_streams, limits.max_concurrent_streams),
        # Retrieved excerpts dominate input tokens in a RAG chat, so capping chunks
        # bounds cost at least as much as capping the answer does.
        final_chunks=_tighten(base.final_chunks, limits.max_retrieved_chunks),
        max_output_tokens=(
            limits.max_output_tokens
            if base.max_output_tokens is None
            else _tighten(base.max_output_tokens, limits.max_output_tokens)
        ),
    )
