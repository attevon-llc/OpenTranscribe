"""The per-process search response cache, and every input its key must name.

A cached page must name everything that decided its content: the query and filters,
the fusion pipeline, the corpus version (#666) and the redaction policy it was masked
under (#86), so no change to any of them can be answered from a stale entry.
"""

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from typing import TYPE_CHECKING

from app.core.constants import SEARCH_CACHE_MAX_SIZE
from app.core.constants import SEARCH_CACHE_TTL_SECONDS
from app.services.redaction.config import MASKABLE_CATEGORIES
from app.services.search.search_types import SearchResponse

if TYPE_CHECKING:  # pragma: no cover - import cost paid only by type checkers
    from app.services.redaction.config import EffectiveRedactionConfig

logger = logging.getLogger(__name__)


# Module-level search cache (OrderedDict for O(1) LRU eviction)
_search_cache: OrderedDict[str, tuple[float, SearchResponse]] = OrderedDict()
_search_cache_lock = threading.Lock()


def _make_cache_key(**kwargs) -> str:
    """Create a deterministic cache key from search params."""
    serializable = {k: v for k, v in sorted(kwargs.items()) if v is not None}
    raw = json.dumps(serializable, sort_keys=True, default=str)
    return hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()


def _search_corpus_version() -> str:
    """The same global "has indexed content changed" marker chat's retrieval cache uses.

    Issue #666: this process-local response cache used to be invalidated by
    ``clear_search_cache()``, a bare dict ``.clear()`` called from the reindex
    and model-switch coordinators — but those run in a CPU/embedding **worker**
    process, and searches are served from the **API** process, so the clear was
    a no-op in the process that actually mattered (it only ever helped a
    single-process dev deployment where the two happen to share one interpreter).
    A stale cache entry for content that has since been re-indexed could
    therefore be served for the rest of ``SEARCH_CACHE_TTL_SECONDS`` regardless
    of how many times ``clear_search_cache`` ran elsewhere.

    Reusing ``chat.retrieval_cache.corpus_version()`` rather than inventing a
    second counter: it is already bumped, cross-process via Redis, by every
    real content-changing write to the chunk plane
    (``chat.retrieval_cache.bump_corpus_version``, called from
    ``index_transcript_chunks`` and the rename-propagation tasks) — exactly the
    set of events that should also invalidate a cached search page. Folding it
    into the cache key means a version bump makes every previously cached key
    permanently unreachable rather than requiring anyone to remember to clear
    a cache living in a different process.

    Returns:
        The current version as a string; "0" (matching an unbumped corpus) if
        Redis is unreachable, so a read failure degrades to the pre-existing
        TTL-only behaviour rather than breaking search.
    """
    try:
        from app.services.chat.retrieval_cache import corpus_version

        return corpus_version()
    except Exception:  # noqa: BLE001 — a cache-key input must not break search
        logger.debug("Search corpus version read failed", exc_info=True)
        return "0"


def _resolve_redaction_config_for_cache(
    user_id: int, organization_id: int | None = None
) -> "EffectiveRedactionConfig | None":
    """Resolve the requesting user's redaction policy BEFORE the cache lookup.

    Must run here rather than only inside :meth:`_redact_snippets` — the config
    this returns is exactly what determines whether the response about to be
    cached is masked, so the cache key has to name it (see
    :func:`_redaction_policy_fingerprint`). Resolving it only at snippet-mask
    time, after the cache lookup, was the #86-adjacent gap: for up to
    ``SEARCH_CACHE_TTL_SECONDS`` after a user flips their redaction policy, a
    repeat of an already-cached query kept serving whichever masking state won
    the race to populate that key first — user A's masked page to user B under
    a different policy, or vice versa, since ``user_id`` alone was already in
    the key but the POLICY driving the masking was not.

    Args:
        user_id: The requesting user (matches ``_redact_snippets``'s subject).
        organization_id: The requester's active tenant scope, threaded into
            ``resolve_effective_config`` so a registered per-org redaction
            floor (issue #982/#987) is actually consulted (#988).

    Returns:
        The effective config, or ``None`` when it could not be resolved at all
        (DB unreachable, etc.) — never raises, so a config failure degrades to
        the "unresolvable" cache bucket rather than breaking the search request.
    """
    from app.db.session_utils import session_scope
    from app.services.redaction.config import resolve_effective_config

    try:
        with session_scope() as db:
            return resolve_effective_config(db, user_id, organization_id=organization_id)
    except Exception:  # noqa: BLE001 — a config read must not break search
        logger.exception("Redaction config unavailable while resolving the search cache key")
        return None


def _redaction_policy_fingerprint(cfg: "EffectiveRedactionConfig | None") -> str:
    """A short deterministic string naming exactly the masking state a cached page holds.

    Folded into the cache key so two requesting users under different redaction
    policies — or the same user before and after a policy change — can never be
    served each other's cached snippets. Scoped to the fields that can actually
    change :func:`snippet_redaction.mask_snippets`'s output on THIS surface:
    only ``pii``/``profanity``/``custom`` are maskable here at all (see
    ``MASKABLE_CATEGORIES`` — ``toxicity`` produces no spans on this path), and
    ``mask_snippets`` always forces ``style="label"`` for a preview regardless
    of the user's own style preference, so neither ``toxicity_threshold`` nor
    ``style`` can move the rendered snippet and neither is in the fingerprint.

    An unresolvable config gets its own fixed bucket rather than reusing any
    other value, so it can never collide with a real policy's fingerprint.

    Args:
        cfg: The resolved config, or None when it could not be resolved.

    Returns:
        A short opaque string safe to fold into `_make_cache_key`'s kwargs.
    """
    if cfg is None:
        return "unresolvable"
    active_categories = (
        sorted(set(cfg.enabled_categories) & MASKABLE_CATEGORIES) if cfg.enabled else []
    )
    payload = {
        "enabled": bool(active_categories),
        "categories": active_categories,
        "pii_entities": sorted(cfg.pii_entities),
        "custom_words": sorted(cfg.custom_words),
        "allowlist": sorted(cfg.allowlist),
    }
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()


def _get_cached_response(cache_key: str) -> SearchResponse | None:
    """Get a cached response if it exists and hasn't expired."""
    with _search_cache_lock:
        entry = _search_cache.get(cache_key)
        if entry is None:
            return None
        cached_time, cached_response = entry
        if (time.time() - cached_time) < SEARCH_CACHE_TTL_SECONDS:
            _search_cache.move_to_end(cache_key)  # Mark as recently used
            return cached_response
        else:
            del _search_cache[cache_key]
    return None


def _set_cached_response(cache_key: str, response: SearchResponse) -> None:
    """Cache a search response with TTL and O(1) LRU eviction."""
    with _search_cache_lock:
        if cache_key in _search_cache:
            _search_cache.move_to_end(cache_key)
        _search_cache[cache_key] = (time.time(), response)
        # Evict oldest (least recently used) entries if cache is full
        while len(_search_cache) > SEARCH_CACHE_MAX_SIZE:
            _search_cache.popitem(last=False)  # Remove oldest (first item)
