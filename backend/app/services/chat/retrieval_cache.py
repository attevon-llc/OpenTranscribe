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
_CORPUS_VERSION_KEY = "chat:corpus:version"


def _normalize(query: str) -> str:
    return " ".join((query or "").lower().split())


def corpus_version() -> str:
    """Monotonic marker for "the searchable corpus as it is right now".

    Mixed into every cache key so that any change to indexed transcript content
    makes previously cached results **unreachable** rather than merely stale.
    That matters more than hit rate: without it, for up to the cache TTL an
    answer could quote a recording that has since been deleted or quarantined,
    and cite a link that now 404s.

    Deliberately GLOBAL rather than per-user. Deletion doesn't know the owner,
    and chat retrieves shared recordings — so the person who edits a transcript
    is frequently not the person whose cache must be invalidated. A global
    counter is the only granularity that is actually correct here; the cost is
    that any indexing write cools every chat cache, which is acceptable for a
    pure latency optimization with a 5-minute lifetime.

    Returns:
        The current version as a string ("0" when Redis is unreachable, which
        degrades to the previous always-same-key behaviour).
    """
    try:
        from app.core.redis import get_redis

        raw = get_redis().get(_CORPUS_VERSION_KEY)
        return raw.decode() if isinstance(raw, bytes) else str(raw or "0")
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Chat corpus version read failed: {exc}")
        return "0"


def bump_corpus_version() -> None:
    """Invalidate every cached retrieval, because indexed content changed.

    Called from the chunk indexing and deletion paths. Never raises — indexing
    must not fail because a chat cache marker could not be written (the cost of
    that failure is a stale cache entry for at most the TTL, not a broken
    pipeline).
    """
    try:
        from app.core.redis import get_redis

        get_redis().incr(_CORPUS_VERSION_KEY)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Chat corpus version bump failed: {exc}")


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
    corpus_rev: str | None = None,
) -> str:
    """Build the cache key for one retrieval.

    The key binds everything that can change the answer: who is asking, in which
    tenant, the normalized question, the resolved file scope, the admin settings
    revision, the retrieval mode, and the corpus version. Miss on any of them.
    """
    corpus = corpus_rev if corpus_rev is not None else corpus_version()
    material = f"{_normalize(query)}|{scope_digest}|{settings_rev}|{search_mode}|c{corpus}"
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


# ---------------------------------------------------------------------------
# Tier 2: semantic cache (opt-in)
#
# The exact cache above only helps when someone asks a byte-identical question.
# In practice people rephrase — "what did we decide about pricing" vs "what was
# the pricing decision" — which is a miss for tier 1 but the same retrieval.
# Tier 2 embeds the question and reuses a recent result whose embedding is
# near-identical.
#
# It is OFF by default because reuse is a judgement call: at a high enough
# threshold it is safe, but a threshold set too low would serve the wrong
# passages, and the failure is invisible (a plausible answer about the wrong
# thing). The cosine floor is admin-tunable and defaults to 0.97.
# ---------------------------------------------------------------------------

_SEMANTIC_KEY = "chat:semantic:{user_id}:{org}"
_SEMANTIC_HISTORY = 50


def _embed_query(text: str) -> list[float] | None:
    """Embed a question with the deployed ML Commons model (None on any failure)."""
    try:
        from app.services.opensearch_service import get_opensearch_client
        from app.services.search.ml_model_service import get_ml_model_service

        model_id = get_ml_model_service().get_active_model_id()
        client = get_opensearch_client()
        if not model_id or not client:
            return None

        response = client.transport.perform_request(
            "POST",
            f"/_plugins/_ml/models/{model_id}/_predict",
            body={
                "text_docs": [text],
                "return_number": True,
                "target_response": ["sentence_embedding"],
            },
        )
        output = (response.get("inference_results") or [{}])[0].get("output") or []
        for item in output:
            data = item.get("data")
            if isinstance(data, list) and data:
                return [float(x) for x in data]
    except Exception as exc:  # noqa: BLE001 — an enhancement, never a dependency
        logger.debug(f"Chat semantic cache embedding failed: {exc}")
    return None


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors (0.0 when degenerate)."""
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return float(dot / (norm_a * norm_b))


def find_semantic_match(
    *,
    user_id: int,
    organization_id: int | None,
    query: str,
    scope_digest: str,
    settings_rev: str,
    threshold: float,
) -> tuple[list[ChunkHit], list[float]] | None:
    """Find a recent near-identical question's cached retrieval.

    Entries are scoped by user, tenant, resolved scope AND settings revision, so
    a hit can only ever reuse results the same person would have got for the same
    files under the same configuration.

    Returns:
        ``(hits, embedding)`` on a hit; ``(_, embedding)`` is also returned on a
        miss via :func:`remember_semantic` so the caller need not re-embed —
        here, ``None`` means no usable match (or embedding was unavailable).
    """
    embedding = _embed_query(query)
    if embedding is None:
        return None

    # A reworded question must not reuse passages from a corpus that has
    # since changed, exactly like the exact-match tier.
    corpus = corpus_version()

    try:
        from app.core.redis import get_redis

        raw = get_redis().get(_SEMANTIC_KEY.format(user_id=user_id, org=organization_id or 0))
        entries = json.loads(raw) if raw else []
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Chat semantic cache read failed: {exc}")
        return None

    best_key: str | None = None
    best_score = 0.0
    for entry in entries:
        if (
            entry.get("scope") != scope_digest
            or entry.get("rev") != settings_rev
            or entry.get("corpus") != corpus
        ):
            continue
        score = _cosine(embedding, entry.get("vector") or [])
        if score > best_score:
            best_score = score
            best_key = entry.get("key")

    if best_key is None or best_score < threshold:
        return None

    hits = get_cached(best_key)
    if hits is None:
        return None  # the tier-1 entry expired under us

    logger.info("Chat semantic cache hit (cosine %.4f >= %.2f)", best_score, threshold)
    return hits, embedding


def remember_semantic(
    *,
    user_id: int,
    organization_id: int | None,
    query: str,
    cache_key_value: str,
    scope_digest: str,
    settings_rev: str,
    ttl_seconds: int,
    embedding: list[float] | None = None,
) -> None:
    """Record this question's embedding so a later rephrasing can reuse it."""
    if ttl_seconds <= 0:
        return
    vector = embedding if embedding is not None else _embed_query(query)
    if vector is None:
        return

    redis_key = _SEMANTIC_KEY.format(user_id=user_id, org=organization_id or 0)
    try:
        from app.core.redis import get_redis

        client = get_redis()
        raw = client.get(redis_key)
        entries = json.loads(raw) if raw else []
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Chat semantic cache read-before-write failed: {exc}")
        return

    entries.append(
        {
            "key": cache_key_value,
            "vector": vector,
            "scope": scope_digest,
            "rev": settings_rev,
            "corpus": corpus_version(),
        }
    )
    # Keep only the most recent N so the value stays a bounded size.
    entries = entries[-_SEMANTIC_HISTORY:]

    try:
        client.setex(redis_key, ttl_seconds, json.dumps(entries))
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Chat semantic cache write failed: {exc}")
