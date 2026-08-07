"""Cross-encoder reranking for retrieved chunks.

Retrieval (BM25 + vector, RRF-fused) optimizes recall: it casts a wide net and
ranks by signals computed independently for query and document. A cross-encoder
scores the *pair* jointly, which is markedly better at judging whether a specific
transcript passage actually answers the question — the difference between "this
chunk mentions the budget" and "this chunk states the budget decision".

Deliberately CPU-only and loaded in the **backend** container, never the GPU
worker: the GPU is the project's single transcription resource and must not be
occupied by interactive requests. The model is ~90MB; first use adds roughly
350-500MB RSS, so operators who don't want that can turn it off with
``chat.rag.rerank_enabled``. If the model isn't in the cache, reranking disables
itself with a warning rather than failing the chat, and retries periodically so a
cache that appears later is picked up.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from app.core import constants as C  # noqa: N812

logger = logging.getLogger(__name__)

_reranker: Any = None
_lock = threading.Lock()
# When the next load may be attempted. 0.0 = now. Set into the future after a
# failure so a broken load is not retried on every single chat message.
_retry_after = 0.0

# How long to wait before retrying a failed load. Long enough that a genuinely
# missing model costs one attempt every few minutes rather than one per request,
# short enough that a container which starts before its model-cache mount is
# ready recovers on its own within a few chats.
RETRY_COOLDOWN_S = 300.0


def get_reranker() -> Any:
    """Return the lazily-loaded CrossEncoder, or None when unavailable.

    Double-checked locking: several concurrent first-chats must not each load a
    copy of the weights.

    A failed load is retried after :data:`RETRY_COOLDOWN_S` rather than latched
    off for the life of the process. The load can fail for reasons that fix
    themselves — the model cache volume not mounted yet when the container
    starts, a transient error fetching weights — and latching turned any one of
    those into permanently degraded retrieval quality with no signal beyond a
    single startup warning. Silent, and invisible in every later log line.
    """
    global _reranker, _retry_after

    if _reranker is not None:
        return _reranker
    if time.monotonic() < _retry_after:
        return None

    with _lock:
        # Re-check both conditions: another thread may have loaded it, or lost
        # the same race and just started a fresh cooldown.
        if _reranker is not None:
            return _reranker
        if time.monotonic() < _retry_after:
            return None
        try:
            # Heavy optional import — kept inside the function so CPU-only and
            # model-less deployments can still import this module.
            from sentence_transformers import CrossEncoder

            _reranker = CrossEncoder(C.CHAT_RERANKER_MODEL, device="cpu", max_length=512)
            logger.info(f"Chat reranker loaded on CPU: {C.CHAT_RERANKER_MODEL}")
        except Exception as exc:  # noqa: BLE001
            _retry_after = time.monotonic() + RETRY_COOLDOWN_S
            logger.warning(
                f"Chat reranker unavailable ({C.CHAT_RERANKER_MODEL}): {exc}. "
                f"Reranking is disabled; retrieval order will be used as-is. "
                f"Retrying in {int(RETRY_COOLDOWN_S)}s. "
                "Run scripts/download-models.py to pre-fetch the model."
            )
            _reranker = None
    return _reranker


def reset_reranker_state() -> None:
    """Clear the cached model and cooldown. For tests only."""
    global _reranker, _retry_after
    with _lock:
        _reranker = None
        _retry_after = 0.0


def rerank(query: str, hits: list, *, max_pairs: int = 50) -> list:
    """Reorder ``hits`` by cross-encoder relevance to ``query``.

    Args:
        query: The (possibly rewritten) question.
        hits: Chunk hits from retrieval, in retrieval order.
        max_pairs: Ceiling on pairs scored — scoring is linear in pair count and
            this runs inside a request, so the tail of a large candidate pool is
            left in retrieval order rather than blowing the latency budget.

    Returns:
        Hits reordered best-first. Returns the input unchanged when the model is
        unavailable or scoring fails — degraded ranking beats a failed answer.
    """
    if not hits or len(hits) == 1:
        return hits

    model = get_reranker()
    if model is None:
        return hits

    head = hits[:max_pairs]
    tail = hits[max_pairs:]

    try:
        pairs = [(query, hit.content) for hit in head]
        scores = model.predict(pairs)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"Chat reranking failed, keeping retrieval order: {exc}")
        return hits

    scored = sorted(zip(head, scores, strict=False), key=lambda pair: float(pair[1]), reverse=True)
    reordered = []
    for hit, score in scored:
        hit.score = float(score)
        reordered.append(hit)
    return reordered + tail
