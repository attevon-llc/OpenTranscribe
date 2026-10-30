"""Chat retrieval-cache invalidation shared by every write path of the chunks index."""

import logging

logger = logging.getLogger(__name__)


def _invalidate_chat_retrieval_cache() -> None:
    """Mark the searchable corpus as changed so chat can't serve stale passages.

    RAG chat caches retrieval results for a few minutes. Without this, for the
    length of that window an answer could quote a recording that was just
    deleted, quarantined or re-transcribed — and cite a link that no longer
    resolves. Contained: indexing must never fail because a cache marker
    could not be written.
    """
    try:
        from app.services.chat.retrieval_cache import bump_corpus_version

        bump_corpus_version()
    except Exception:  # noqa: BLE001 — best-effort marker; indexing must not fail on it
        logger.debug("Could not bump chat corpus version", exc_info=True)
