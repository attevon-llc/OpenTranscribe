"""Retrieval caching for chat.

Retrieval is the slowest non-LLM stage (OpenSearch round trip + optional
cross-encoder scoring). Repeat questions are common in practice — a user
rephrasing, regenerating an answer, or several people asking the same thing about
a shared meeting — so a short-lived cache removes a visible chunk of latency.

Keys bind everything that changes the result: user, tenant, normalized query,
resolved scope and the admin settings revision. A retune or a scope change
therefore misses rather than serving stale shape. Cached entries are
**post-retrieval, pre-masking**: masking is per-user policy applied downstream,
so a cache hit never bypasses redaction.

Every failure degrades to a miss — the cache can never be the reason a chat fails.
"""

from __future__ import annotations

import hashlib
import json
import logging

from app.services.search.chunk_retrieval import ChunkHit

logger = logging.getLogger(__name__)

_KEY = "chat:retr:{user_id}:{org}:{digest}"


def _normalize(query: str) -> str:
    return " ".join((query or "").lower().split())


def scope_hash(file_uuids: list[str] | None) -> str:
    """Stable digest of a resolved scope (``None`` = all accessible)."""
    if file_uuids is None:
        return "all"
    return hashlib.sha256(",".join(sorted(file_uuids)).encode()).hexdigest()[:16]


def cache_key(
    *,
    user_id: int,
    organization_id: int | None,
    query: str,
    scope_digest: str,
    settings_rev: str,
    search_mode: str,
) -> str:
    """Build the cache key for one retrieval."""
    material = f"{_normalize(query)}|{scope_digest}|{settings_rev}|{search_mode}"
    digest = hashlib.sha256(material.encode()).hexdigest()[:32]
    return _KEY.format(user_id=user_id, org=organization_id or 0, digest=digest)


def get_cached(key: str) -> list[ChunkHit] | None:
    """Return cached hits for ``key``, or None on miss/error."""
    try:
        from app.core.redis import get_redis

        raw = get_redis().get(key)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Chat retrieval cache read failed: {exc}")
        return None

    if not raw:
        return None
    try:
        payload = json.loads(raw)
        return [ChunkHit.from_cache_dict(item) for item in payload]
    except Exception as exc:  # noqa: BLE001 — a corrupt entry is just a miss
        logger.debug(f"Discarding unreadable chat retrieval cache entry: {exc}")
        return None


def set_cached(key: str, hits: list[ChunkHit], ttl_seconds: int) -> None:
    """Store hits under ``key``. No-op when caching is disabled (ttl <= 0)."""
    if ttl_seconds <= 0 or not hits:
        return
    try:
        from app.core.redis import get_redis

        payload = json.dumps([hit.to_cache_dict() for hit in hits])
        get_redis().setex(key, ttl_seconds, payload)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Chat retrieval cache write failed: {exc}")


def invalidate_user(user_id: int) -> int:
    """Drop every cached retrieval for one user (e.g. after new content indexes).

    Uses SCAN rather than KEYS so a large keyspace doesn't block Redis.
    """
    pattern = _KEY.format(user_id=user_id, org="*", digest="*")
    removed = 0
    try:
        from app.core.redis import get_redis

        client = get_redis()
        for key in client.scan_iter(match=pattern, count=200):
            client.delete(key)
            removed += 1
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Chat retrieval cache invalidation failed: {exc}")
    return removed
