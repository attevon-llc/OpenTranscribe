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
