"""Hybrid BM25 + vector search service using OpenSearch 3.4 native features."""

import functools
import hashlib
import html as html_module
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from nltk.stem import SnowballStemmer

from app.core.config import settings
from app.core.constants import SEARCH_CACHE_MAX_SIZE
from app.core.constants import SEARCH_CACHE_TTL_SECONDS
from app.core.constants import SEARCH_DEFAULT_PAGE_SIZE
from app.core.constants import SEARCH_MAX_PAGE_SIZE
from app.core.constants import SEARCH_MAX_SNIPPETS_PER_FILE
from app.services.opensearch_service import get_opensearch_client
from app.services.opensearch_service import opensearch_client
from app.services.search.indexing_service import ensure_chunks_index_exists
from app.services.search.indexing_service import ensure_search_pipeline_exists

logger = logging.getLogger(__name__)

# Module-level caches for index/pipeline existence checks
_index_verified = False
_pipeline_verified = False
_neural_search_available: bool | None = None
_neural_search_check_time: float = 0.0
_NEURAL_SEARCH_CACHE_TTL: float = 120.0  # Re-check every 2 minutes (success)
_NEURAL_SEARCH_FAILURE_TTL: float = 30.0  # Re-check every 30 seconds (failure)

# Lock for module-level state mutations
_state_lock = threading.Lock()


def _sanitize_html(text: str) -> str:
    """Strip all HTML tags except <mark> and </mark> to prevent XSS.

    OpenSearch highlights wrap matched terms in <mark> tags, but the surrounding
    content from indexed transcripts could contain injected HTML/JS.
    """
    if not text:
        return text
    # Strip null bytes first to prevent placeholder injection
    text = text.replace("\x00", "")
    # Temporarily replace allowed <mark> tags with placeholders
    text = text.replace('<mark class="semantic">', "\x00MARK_SEM_OPEN\x00")
    text = text.replace("<mark>", "\x00MARK_OPEN\x00")
    text = text.replace("</mark>", "\x00MARK_CLOSE\x00")
    # Strip all remaining HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    # Unescape existing entities before re-escaping to prevent double-escape
    text = html_module.unescape(text)
    # Escape HTML entities in the remaining text
    text = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )
    # Restore allowed <mark> tags
    text = text.replace("\x00MARK_SEM_OPEN\x00", '<mark class="semantic">')
    text = text.replace("\x00MARK_OPEN\x00", "<mark>")
    text = text.replace("\x00MARK_CLOSE\x00", "</mark>")
    return text


# Language-aware stemmer cache
_stemmers: dict[str, SnowballStemmer] = {}
_stemmer_lock = threading.Lock()


@functools.lru_cache(maxsize=4096)
def _get_word_stem(word: str, language: str = "english") -> str:
    """Get stem using NLTK SnowballStemmer - matches OpenSearch snowball filter.

    Args:
        word: Word to stem.
        language: Language for stemming (default: english).

    Returns:
        Stemmed word.
    """
    lang = language.lower() if language.lower() in SnowballStemmer.languages else "english"
    if lang not in _stemmers:
        with _stemmer_lock:
            if lang not in _stemmers:
                _stemmers[lang] = SnowballStemmer(lang)
    return str(_stemmers[lang].stem(word.lower()))


def _matches_query_prefix(word_lower: str, word_stem: str, query_prefixes: list[str]) -> bool:
    """Check if word or its stem matches any query prefix."""
    return any(
        word_lower.startswith(prefix) or word_stem.startswith(prefix) for prefix in query_prefixes
    )


def _matches_query_word_start(word_lower: str, query_words: list[str]) -> bool:
    """Check if word starts with any query word."""
    return any(word_lower.startswith(qw) for qw in query_words)


def _should_highlight_word(
    word: str,
    similar_words_set: set[str],
    query_words: list[str],
    query_stems: list[str],
    query_prefixes: list[str],
) -> bool:
    """Check if a word should be highlighted based on semantic or stem matching.

    Args:
        word: The word to check.
        similar_words_set: Pre-computed set of semantically similar words.
        query_words: Lowercase query words (length >= 3).
        query_stems: Stemmed versions of query words.
        query_prefixes: Prefixes derived from query words.

    Returns:
        True if the word should be highlighted.
    """
    word_lower = word.lower()

    # Check semantic similarity (from pre-computed set)
    if word_lower in similar_words_set:
        return True

    # Check direct word match
    if word_lower in query_words:
        return True

    # Fallback: stem matching
    word_stem = _get_word_stem(word_lower)
    if word_stem in query_stems:
        return True

    # Check prefix matching
    if _matches_query_prefix(word_lower, word_stem, query_prefixes):
        return True

    # Check if word starts with any query word
    return _matches_query_word_start(word_lower, query_words)


@dataclass
class QueryHighlightContext:
    """Pre-computed query analysis for efficient semantic highlighting."""

    query_words: list[str]
    query_stems: list[str]
    query_prefixes: list[str]

    @classmethod
    def from_query(cls, query: str) -> "QueryHighlightContext":
        """Build context from a query string, computing stems/prefixes once."""
        query_words = [w.lower() for w in query.split() if len(w) >= 2]
        query_stems = [_get_word_stem(w) for w in query_words]
        query_prefixes = [w[: max(4, len(w) - 2)] for w in query_words if len(w) >= 4]
        return cls(query_words=query_words, query_stems=query_stems, query_prefixes=query_prefixes)


def _add_semantic_highlights(
    snippet: str,
    query: str,
    similar_words_set: set[str] | None = None,
    highlight_ctx: QueryHighlightContext | None = None,
) -> str:
    """Highlight semantically similar words in snippet using <mark class='semantic'>.

    For semantic-only hits, OpenSearch returns no <mark> tags. This function
    highlights words that are semantically similar to the query.

    Args:
        snippet: The snippet text (may contain HTML entities but no <mark> tags).
        query: The original search query string.
        similar_words_set: Pre-computed set of similar words (for efficiency).
        highlight_ctx: Pre-computed query analysis to avoid redundant stemming.

    Returns:
        Snippet with semantically similar words wrapped in <mark class="semantic"> tags.
    """
    if not query or not snippet:
        return snippet

    if similar_words_set is None:
        similar_words_set = set()

    # Use pre-computed context if available, otherwise compute
    if highlight_ctx is not None:
        query_words = highlight_ctx.query_words
        query_stems = highlight_ctx.query_stems
        query_prefixes = highlight_ctx.query_prefixes
    else:
        query_words = [w.lower() for w in query.split() if len(w) >= 2]
        query_stems = [_get_word_stem(w) for w in query_words]
        query_prefixes = [w[: max(4, len(w) - 2)] for w in query_words if len(w) >= 4]

    # Process snippet word by word, preserving non-word characters
    result = []
    current_pos = 0
    word_pattern = re.compile(r"\b([\w]+)\b", re.UNICODE)

    for match in word_pattern.finditer(snippet):
        result.append(snippet[current_pos : match.start()])
        word = match.group(1)
        if _should_highlight_word(
            word, similar_words_set, query_words, query_stems, query_prefixes
        ):
            result.append(f'<mark class="semantic">{word}</mark>')
        else:
            result.append(word)
        current_pos = match.end()

    result.append(snippet[current_pos:])
    return "".join(result)


def _parse_query_operators(raw_query: str) -> tuple[str, dict[str, str]]:
    """Parse inline operators from query string.

    Supports: speaker:"Name" or speaker:Name
    Returns: (clean_query, operators_dict)

    Examples:
        'speaker:"Joe Rogan" china' -> ('china', {'speaker': 'Joe Rogan'})
        'speaker:SPEAKER_00 warp' -> ('warp', {'speaker': 'SPEAKER_00'})
        'just plain text' -> ('just plain text', {})
    """
    operators: dict[str, str] = {}
    # Match speaker:"quoted name" or speaker:single_word
    pattern = r'speaker:(?:"([^"]+)"|(\S+))'
    match = re.search(pattern, raw_query, re.IGNORECASE)
    if match:
        speaker_name = match.group(1) or match.group(2)
        operators["speaker"] = speaker_name
        # Remove the operator from query text
        clean = re.sub(pattern, "", raw_query, count=1, flags=re.IGNORECASE).strip()
        # Collapse multiple spaces
        clean = re.sub(r"\s+", " ", clean).strip()
        logger.info(f"PARSE: raw='{raw_query}' -> clean='{clean}', speaker='{speaker_name}'")
    else:
        clean = raw_query
    return clean, operators


def _extract_snippet_and_match_type(
    source: dict[str, Any],
    highlight: dict[str, Any],
) -> tuple[str, str]:
    """Extract the display snippet and match type from a search hit.

    Args:
        source: The _source dict from the OpenSearch hit.
        highlight: The highlight dict from the OpenSearch hit.

    Returns:
        Tuple of (sanitized snippet text, match type string).
    """
    if "content" in highlight or "content.exact" in highlight:
        content_highlights = highlight.get("content") or highlight.get("content.exact", [])
        snippet = " ... ".join(content_highlights)
        match_type = "content"
    elif "title" in highlight:
        snippet = source.get("content", "")[:200]
        match_type = "title"
    elif "speaker" in highlight:
        snippet = source.get("content", "")[:200]
        match_type = "speaker"
    else:
        snippet = source.get("content", "")[:200]
        match_type = "content"
    return _sanitize_html(snippet), match_type


def _extract_highlighted_field(
    highlight: dict[str, Any],
    field: str,
) -> str:
    """Extract and sanitize a highlighted field value.

    Args:
        highlight: The highlight dict from the OpenSearch hit.
        field: Field name to extract (e.g., "title", "speaker").

    Returns:
        Sanitized highlighted string, or empty string if not present.
    """
    if field in highlight:
        return _sanitize_html(" ".join(highlight[field]))
    return ""


@dataclass
class SearchOccurrence:
    """A single matching snippet within a file."""

    snippet: str
    speaker: str
    start_time: float
    end_time: float
    chunk_index: int
    score: float
    match_type: str = "content"  # "content", "title", or "speaker"
    speaker_highlighted: str = ""  # Speaker name with <mark> tags if matched
    has_keyword_match: bool = True  # False for semantic-only hits (no highlights)
    highlight_type: str = "keyword"  # "keyword" or "semantic"


@dataclass
class SearchHit:
    """A file-level search result with multiple occurrences."""

    file_uuid: str
    file_id: int
    title: str
    speakers: list[str]
    tags: list[str]
    upload_time: str
    language: str
    content_type: str = ""
    relevance_score: float = 0.0
    occurrences: list[SearchOccurrence] = field(default_factory=list)
    total_occurrences: int = 0
    title_highlighted: str = ""  # Title with <mark> tags if matched
    keyword_occurrences: int = 0  # Count of hits with actual keyword highlights
    semantic_only: bool = False  # True if no keyword matches, only semantic
    semantic_confidence: str = ""  # "", "high", or "low" for semantic-only hits
    match_sources: list[str] = field(
        default_factory=list
    )  # e.g. ["content", "title", "speaker", "semantic"]
    relevance_percent: int = 0  # 0-100 relevance confidence for display
    duration: float = 0.0  # Duration in seconds
    file_size: int = 0  # File size in bytes
    semantic_occurrences: int = 0  # Count of semantic-only occurrences
    has_both_match_types: bool = False  # True if file has both keyword AND semantic matches


@dataclass
class SearchResponse:
    """Complete search response."""

    query: str
    results: list[SearchHit]
    total_results: int
    total_files: int
    page: int
    page_size: int
    total_pages: int
    search_time_ms: float
    filters_applied: dict[str, Any] = field(default_factory=dict)
    search_mode: str = "hybrid"
    _fell_back_to_bm25: bool = False  # Internal flag — skip caching if True


# Module-level search cache (OrderedDict for O(1) LRU eviction)
_search_cache: OrderedDict[str, tuple[float, SearchResponse]] = OrderedDict()
_search_cache_lock = threading.Lock()


def _make_cache_key(**kwargs) -> str:
    """Create a deterministic cache key from search params."""
    serializable = {k: v for k, v in sorted(kwargs.items()) if v is not None}
    raw = json.dumps(serializable, sort_keys=True, default=str)
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


def clear_search_cache() -> None:
    """Clear the entire search cache. Called after reindex or model switch."""
    with _search_cache_lock:
        _search_cache.clear()
    logger.info("Search cache cleared")


def reset_neural_search_state() -> None:
    """Reset the neural search availability state.

    Call this when switching models or after configuration changes.
    """
    global _neural_search_available
    global _neural_search_check_time
    _neural_search_available = None
    _neural_search_check_time = 0.0
    logger.info("Neural search state reset")


def _append_range_filter(
    filters: list[dict[str, Any]],
    field: str,
    gte_value: Any | None,
    lte_value: Any | None,
) -> None:
    """Append a range filter clause if at least one bound is provided.

    Args:
        filters: List of filter clauses to append to.
        field: OpenSearch field name for the range.
        gte_value: Lower bound (inclusive), or None.
        lte_value: Upper bound (inclusive), or None.
    """
    if gte_value is None and lte_value is None:
        return
    range_clause: dict[str, Any] = {}
    if gte_value is not None:
        range_clause["gte"] = gte_value
    if lte_value is not None:
        range_clause["lte"] = lte_value
    filters.append({"range": {field: range_clause}})


def _ensure_infrastructure() -> None:
    """Ensure the OpenSearch index and search pipeline exist (checked once)."""
    global _index_verified, _pipeline_verified
    if _index_verified and _pipeline_verified:
        return
    with _state_lock:
        if not _index_verified:
            ensure_chunks_index_exists()
            _index_verified = True
        if not _pipeline_verified:
            ensure_search_pipeline_exists()
            _pipeline_verified = True


def reset_infrastructure_state() -> None:
    """Reset index/pipeline verification state. Call after index recreation."""
    global _index_verified, _pipeline_verified
    with _state_lock:
        _index_verified = False
        _pipeline_verified = False
    logger.info("Infrastructure verification state reset")


def _collect_filters_applied(
    speakers: list[str] | None = None,
    tags: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    file_type: list[str] | None = None,
    collection_id: int | None = None,
    language: str | None = None,
    title_filter: str | None = None,
) -> dict[str, Any]:
    """Build a dict of non-None filter values for the response metadata."""
    candidates: list[tuple[str, Any]] = [
        ("speakers", speakers),
        ("tags", tags),
        ("date_from", date_from),
        ("date_to", date_to),
        ("file_type", file_type),
        ("collection_id", collection_id),
        ("language", language),
        ("title_filter", title_filter),
    ]
    return {key: value for key, value in candidates if value is not None}


class HybridSearchService:
    """Executes hybrid BM25 + vector search with RRF via OpenSearch 3.4 native pipeline."""

    def search(
        self,
        query: str,
        user_id: int,
        page: int = 1,
        page_size: int = SEARCH_DEFAULT_PAGE_SIZE,
        speakers: list[str] | None = None,
        tags: list[str] | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        sort_by: str = "relevance",
        sort_order: str = "desc",
        search_mode: str = "hybrid",
        file_type: list[str] | None = None,
        collection_id: int | None = None,
        min_duration: float | None = None,
        max_duration: float | None = None,
        min_file_size: int | None = None,
        max_file_size: int | None = None,
        language: str | None = None,
        title_filter: str | None = None,
    ) -> SearchResponse:
        """Execute hybrid search and return grouped results.

        Args:
            query: Search query text.
            user_id: Current user ID for filtering.
            page: Page number (1-indexed).
            page_size: Results per page.
            speakers: Optional speaker filter list.
            tags: Optional tag filter list.
            date_from: Optional start date filter (ISO format).
            date_to: Optional end date filter (ISO format).
            sort_by: Sort field - relevance, upload_time, completed_at, filename, duration, file_size.
            sort_order: Sort direction - asc or desc.
            title_filter: Optional filename/title substring filter.

        Returns:
            SearchResponse with grouped results.
        """
        client = get_opensearch_client()
        if not client:
            logger.warning("OpenSearch client not initialized")
            return self._empty_response(query, page, page_size)

        start_time = time.time()
        page_size = min(page_size, SEARCH_MAX_PAGE_SIZE)

        # Check cache
        cache_key = _make_cache_key(
            query=query,
            user_id=user_id,
            page=page,
            page_size=page_size,
            speakers=speakers,
            tags=tags,
            date_from=date_from,
            date_to=date_to,
            sort_by=sort_by,
            sort_order=sort_order,
            search_mode=search_mode,
            file_type=file_type,
            collection_id=collection_id,
            min_duration=min_duration,
            max_duration=max_duration,
            min_file_size=min_file_size,
            max_file_size=max_file_size,
            language=language,
            title_filter=title_filter,
        )
        cached = _get_cached_response(cache_key)
        if cached:
            return cached

        _ensure_infrastructure()

        # Parse inline query operators (e.g., speaker:"Joe Rogan" china)
        clean_query, operators = _parse_query_operators(query)
        if "speaker" in operators:
            speakers = list(speakers or []) + [operators["speaker"]]
        search_query = clean_query.strip() if clean_query else ""

        # Debug logging
        logger.info(
            f"SEARCH: original='{query}', clean='{clean_query}', search_query='{search_query}', speakers={speakers}"
        )

        # Build filters
        filters = self._build_filters(
            user_id,
            speakers,
            tags,
            date_from,
            date_to,
            file_type=file_type,
            collection_id=collection_id,
            min_duration=min_duration,
            max_duration=max_duration,
            min_file_size=min_file_size,
            max_file_size=max_file_size,
            language=language,
            title_filter=title_filter,
        )
        filters_applied = _collect_filters_applied(
            speakers=speakers,
            tags=tags,
            date_from=date_from,
            date_to=date_to,
            file_type=file_type,
            collection_id=collection_id,
            language=language,
            title_filter=title_filter,
        )

        # Determine search capabilities
        query_embedding, use_hybrid, use_neural = self._generate_query_embedding(
            search_query, search_mode
        )
        has_speaker_filter = bool(speakers)

        result = self._search_with_collapse(
            query=query,
            search_query=search_query,
            filters=filters,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
            search_mode=search_mode,
            filters_applied=filters_applied,
            start_time=start_time,
            has_speaker_filter=has_speaker_filter,
            use_neural=use_neural,
        )

        # Read-time content redaction of snippets (profanity + custom words). This is
        # the cheap, model-free path that runs safely in the API process; PII masking
        # of short snippets is not applied here (it needs the redaction worker's models)
        # — the full transcript view + exports remain the enforced PII boundary.
        # Applied before caching so the per-user cache (cache_key includes user_id)
        # holds the masked version.
        self._redact_snippets(result, user_id)

        # Cache the response — but NOT if it fell back to BM25-only due to
        # a transient error, so the next request retries hybrid properly.
        if not result._fell_back_to_bm25:
            _set_cached_response(cache_key, result)
        else:
            logger.info("Skipping cache for BM25-fallback response (query='%s')", query)

        return result

    def _redact_snippets(self, result: SearchResponse, user_id: int) -> None:
        """Mask profanity/custom words in search snippets per the user's redaction config.

        HTML-safe: the wordlist regex only matches word-boundary alphanumeric runs, never
        ``<``/``>``, so ``<mark>`` highlight tags are preserved. Label style is used for
        previews regardless of the user's chosen style.
        """
        try:
            from app.db.session_utils import session_scope
            from app.services.redaction.config import resolve_effective_config

            with session_scope() as db:
                cfg = resolve_effective_config(db, user_id)
        except Exception as exc:  # noqa: BLE001 — never break search on redaction failure
            logger.debug("Snippet redaction config unavailable: %s", exc)
            return

        if not cfg.enabled:
            return
        cats = cfg.enabled_categories & {"profanity", "custom"}
        if not cats:
            return

        from app.services.redaction.detectors import wordlist
        from app.services.redaction.spans import apply_redactions

        for hit in getattr(result, "results", []) or []:
            for occ in getattr(hit, "occurrences", []) or []:
                snippet = getattr(occ, "snippet", "") or ""
                if not snippet:
                    continue
                spans = []
                if "profanity" in cats:
                    spans += wordlist.find_profanity_spans(snippet, None, cfg.allowlist)
                if "custom" in cats and cfg.custom_words:
                    spans += wordlist.find_custom_spans(
                        snippet, cfg.custom_words, None, cfg.allowlist
                    )
                if spans:
                    masked, _ = apply_redactions(
                        snippet, spans, style="label", enabled_categories=cats
                    )
                    occ.snippet = masked

    def _check_neural_search_available(self) -> bool:
        """Check if neural search is available in OpenSearch.

        Caches the result to avoid repeated checks. Success is cached for
        _NEURAL_SEARCH_CACHE_TTL (120s); failure is cached for the shorter
        _NEURAL_SEARCH_FAILURE_TTL (30s) so the system recovers quickly
        from transient errors.

        Returns:
            True if neural search is available and a model is deployed.
        """
        global _neural_search_available
        global _neural_search_check_time

        if _neural_search_available is not None:
            ttl = (
                _NEURAL_SEARCH_CACHE_TTL if _neural_search_available else _NEURAL_SEARCH_FAILURE_TTL
            )
            if time.time() - _neural_search_check_time < ttl:
                return _neural_search_available
            # TTL expired — reset to force re-check
            _neural_search_available = None

        with _state_lock:
            # Double-check after acquiring lock
            if _neural_search_available is not None:
                ttl = (
                    _NEURAL_SEARCH_CACHE_TTL
                    if _neural_search_available
                    else _NEURAL_SEARCH_FAILURE_TTL
                )
                if time.time() - _neural_search_check_time < ttl:
                    return _neural_search_available
                _neural_search_available = None

            if not settings.OPENSEARCH_NEURAL_SEARCH_ENABLED:
                _neural_search_available = False
                return False

            try:
                from .ml_model_service import get_ml_model_service

                ml_service = get_ml_model_service()
                model_id = ml_service.get_active_model_id()
                _neural_search_available = model_id is not None
                _neural_search_check_time = time.time()
                if _neural_search_available:
                    logger.info(f"Neural search available with model: {model_id}")
                else:
                    logger.info("Neural search not available - no deployed model")
                return _neural_search_available
            except Exception as e:
                logger.warning(f"Could not check neural search availability: {e}")
                _neural_search_available = False
                _neural_search_check_time = time.time()
                return False

    def _get_neural_model_id(self) -> str | None:
        """Get the active neural model ID.

        Returns:
            Model ID string or None if not available.
        """
        try:
            from .ml_model_service import get_ml_model_service

            ml_service = get_ml_model_service()
            return ml_service.get_active_model_id()
        except Exception as e:
            logger.warning(f"Could not get neural model ID: {e}")
            return None

    def _generate_query_embedding(
        self,
        query: str,
        search_mode: str,
    ) -> tuple[None, bool, bool]:
        """Check if hybrid/neural search should be used.

        Neural search generates embeddings server-side in OpenSearch,
        so no client-side embedding is needed.

        Args:
            query: Search query text.
            search_mode: Search mode - "keyword" skips semantic search.

        Returns:
            Tuple of (None, whether hybrid mode is active, whether to use neural query).
        """
        if search_mode == "keyword":
            return None, False, False

        # Check if neural search is available
        if self._check_neural_search_available():
            # Neural mode: OpenSearch generates embeddings server-side
            return None, True, True

        # Neural search not available, fall back to BM25-only
        logger.warning("Neural search not available, using BM25-only mode")
        return None, False, False

    def _sort_and_paginate(
        self,
        query: str,
        grouped: list[SearchHit],
        sort_by: str,
        sort_order: str,
        search_mode: str,
        page: int,
        page_size: int,
        filters_applied: dict[str, Any],
        start_time: float,
    ) -> SearchResponse:
        """Sort grouped results, paginate, and build SearchResponse.

        Results are sorted by the requested field in unified order.
        RRF scores already account for both keyword and semantic signals.
        For relevance sort, sort_order is ignored (always by score desc).
        """
        is_ascending = sort_order == "asc"

        if sort_by == "relevance":
            # Unified relevance sort: RRF scores already combine both signals
            grouped.sort(key=lambda h: -h.relevance_score)
        elif sort_by == "upload_time":
            grouped.sort(
                key=lambda h: h.upload_time or "",
                reverse=not is_ascending,
            )
        elif sort_by == "completed_at":
            # completed_at is not in the search index; fall back to upload_time
            logger.debug(
                "Sort by completed_at using upload_time fallback (completed_at not in search index)"
            )
            grouped.sort(
                key=lambda h: h.upload_time or "",
                reverse=not is_ascending,
            )
        elif sort_by == "filename":
            # Sort by title (case-insensitive)
            grouped.sort(
                key=lambda h: (h.title or "").lower(),
                reverse=not is_ascending,
            )
        elif sort_by == "duration":
            grouped.sort(key=lambda h: h.duration, reverse=not is_ascending)
        elif sort_by == "file_size":
            grouped.sort(key=lambda h: h.file_size, reverse=not is_ascending)

        total_files = len(grouped)
        total_pages = max(1, (total_files + page_size - 1) // page_size)
        start_idx = (page - 1) * page_size
        page_results = grouped[start_idx : start_idx + page_size]

        # Total results uses keyword_occurrences for files with keyword matches,
        # total_occurrences for semantic-only files
        total_results = sum(
            h.keyword_occurrences if h.keyword_occurrences > 0 else h.total_occurrences
            for h in grouped
        )
        elapsed_ms = round((time.time() - start_time) * 1000, 1)

        return SearchResponse(
            query=query,
            results=page_results,
            total_results=total_results,
            total_files=total_files,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            search_time_ms=elapsed_ms,
            filters_applied=filters_applied,
            search_mode=search_mode,
        )

    def get_suggestions(
        self,
        prefix: str,
        user_id: int,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Get auto-complete suggestions.

        Args:
            prefix: Search prefix text.
            user_id: Current user ID.
            limit: Maximum number of suggestions.

        Returns:
            List of suggestion dicts with type, text, and optional metadata.
        """
        if not opensearch_client:
            return []

        index_name = settings.OPENSEARCH_CHUNKS_INDEX

        global _index_verified
        if not _index_verified:
            try:
                if not opensearch_client.indices.exists(index=index_name):
                    return []
                _index_verified = True
            except Exception:
                return []

        suggestions = []

        try:
            # Multi-search for title and speaker suggestions
            msearch_body = [
                # Title matches
                {"index": index_name},
                {
                    "size": 4,
                    "query": {
                        "bool": {
                            "must": [{"match_phrase_prefix": {"title": prefix}}],
                            "filter": [{"terms": {"accessible_user_ids": [user_id]}}],
                        }
                    },
                    "_source": ["title", "file_uuid"],
                    "collapse": {"field": "file_uuid"},
                },
                # Speaker matches
                {"index": index_name},
                {
                    "size": 0,
                    "query": {
                        "bool": {
                            "must": [{"prefix": {"speaker": {"value": prefix.lower()}}}],
                            "filter": [{"terms": {"accessible_user_ids": [user_id]}}],
                        }
                    },
                    "aggs": {"speakers": {"terms": {"field": "speaker", "size": 4}}},
                },
            ]

            response = opensearch_client.msearch(body=msearch_body)

            # Process title matches
            if len(response.get("responses", [])) > 0:
                title_resp = response["responses"][0]
                for hit in title_resp.get("hits", {}).get("hits", []):
                    source = hit["_source"]
                    suggestions.append(
                        {
                            "type": "title",
                            "text": source["title"],
                            "file_uuid": source.get("file_uuid"),
                        }
                    )

            # Process speaker matches
            if len(response.get("responses", [])) > 1:
                speaker_resp = response["responses"][1]
                buckets = (
                    speaker_resp.get("aggregations", {}).get("speakers", {}).get("buckets", [])
                )
                for bucket in buckets:
                    suggestions.append(
                        {
                            "type": "speaker",
                            "text": bucket["key"],
                            "count": bucket["doc_count"],
                        }
                    )

        except Exception as e:
            logger.error(f"Error getting suggestions: {e}")

        return suggestions[:limit]

    def get_available_filters(self, user_id: int) -> dict[str, Any]:
        """Return available filter options for the current user.

        Args:
            user_id: Current user ID.

        Returns:
            Dict with speakers, tags, and date_range.
        """
        if not opensearch_client:
            return {"speakers": [], "tags": [], "date_range": {}}

        index_name = settings.OPENSEARCH_CHUNKS_INDEX

        global _index_verified
        if not _index_verified:
            try:
                if not opensearch_client.indices.exists(index=index_name):
                    return {"speakers": [], "tags": [], "date_range": {}}
                _index_verified = True
            except Exception:
                return {"speakers": [], "tags": [], "date_range": {}}

        try:
            response = opensearch_client.search(
                index=index_name,
                body={
                    "size": 0,
                    "query": {"terms": {"accessible_user_ids": [user_id]}},
                    "aggs": {
                        "speakers": {"terms": {"field": "speaker", "size": 100}},
                        "tags": {"terms": {"field": "tags", "size": 100}},
                        "date_range": {"stats": {"field": "upload_time"}},
                    },
                },
            )

            aggs = response.get("aggregations", {})
            speakers = [
                {"name": b["key"], "count": b["doc_count"]}
                for b in aggs.get("speakers", {}).get("buckets", [])
            ]
            tags = [
                {"name": b["key"], "count": b["doc_count"]}
                for b in aggs.get("tags", {}).get("buckets", [])
            ]
            date_stats = aggs.get("date_range", {})

            return {
                "speakers": speakers,
                "tags": tags,
                "date_range": {
                    "min": date_stats.get("min_as_string"),
                    "max": date_stats.get("max_as_string"),
                },
            }
        except Exception as e:
            logger.error(f"Error getting filters: {e}")
            return {"speakers": [], "tags": [], "date_range": {}}

    def _build_filters(
        self,
        user_id: int,
        speakers: list[str] | None,
        tags: list[str] | None,
        date_from: str | None,
        date_to: str | None,
        file_type: list[str] | None = None,
        collection_id: int | None = None,
        min_duration: float | None = None,
        max_duration: float | None = None,
        min_file_size: int | None = None,
        max_file_size: int | None = None,
        language: str | None = None,
        title_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Build OpenSearch filter clauses."""
        filters: list[dict[str, Any]] = [{"terms": {"accessible_user_ids": [user_id]}}]

        if speakers:
            filters.append({"terms": {"speaker": speakers}})
        if tags:
            filters.append({"terms": {"tags": tags}})
        if file_type:
            filters.append({"terms": {"content_type": file_type}})
        if collection_id is not None:
            filters.append({"term": {"collection_ids": collection_id}})
        if language:
            filters.append({"term": {"language": language}})
        if title_filter:
            # Escape wildcard special characters to prevent injection
            escaped = title_filter.replace("\\", "\\\\").replace("*", "\\*").replace("?", "\\?")
            filters.append(
                {"wildcard": {"title": {"value": f"*{escaped.lower()}*", "case_insensitive": True}}}
            )

        # Range filters for date, duration, and file size
        _append_range_filter(filters, "upload_time", date_from, date_to)
        _append_range_filter(filters, "duration", min_duration, max_duration)
        _append_range_filter(filters, "file_size", min_file_size, max_file_size)

        return filters

    def _build_highlight_fields(
        self,
        has_speaker_filter: bool,
        use_exact: bool = False,
    ) -> dict[str, Any]:
        """Build highlight field configuration shared by all search paths.

        Args:
            has_speaker_filter: Whether a speaker filter is active.
            use_exact: If True, use content.exact instead of content for BM25-only mode.

        Returns:
            Highlight fields dict for OpenSearch.
        """
        content_field = "content.exact" if use_exact else "content"
        fields: dict[str, Any] = {
            content_field: {
                "pre_tags": ["<mark>"],
                "post_tags": ["</mark>"],
                "fragment_size": 200,
                "number_of_fragments": 3,
            },
            "title": {
                "pre_tags": ["<mark>"],
                "post_tags": ["</mark>"],
                "number_of_fragments": 0,
            },
        }
        if not has_speaker_filter:
            fields["speaker"] = {
                "pre_tags": ["<mark>"],
                "post_tags": ["</mark>"],
                "number_of_fragments": 0,
            }
        return fields

    def _build_text_query(
        self,
        query: str,
        search_fields: list[str],
    ) -> dict[str, Any]:
        """Build an adaptive text query with fuzziness and cross-field support.

        Single signal queries use fuzziness for typo tolerance plus an exact
        match boost so precise hits still outrank fuzzy ones.  Multi-word
        queries add cross-field matching (terms can match different fields)
        and phrase proximity with slop.  Quoted phrases bypass fuzziness.

        Args:
            query: Search query text.
            search_fields: Fields to search.

        Returns:
            Query clause dict.
        """
        if not (query and query.strip()):
            return {"match_all": {}}

        words = [w for w in query.split() if len(w) >= 2]
        is_phrase_query = query.startswith('"') and query.endswith('"')

        should_clauses: list[dict[str, Any]] = []

        if is_phrase_query:
            # Exact phrase: no fuzziness, phrase match only
            should_clauses.append(
                {
                    "multi_match": {
                        "query": query.strip('"'),
                        "fields": search_fields,
                        "type": "phrase",
                    }
                }
            )
        else:
            # Primary: best_fields with AUTO fuzziness for typo tolerance
            should_clauses.append(
                {
                    "multi_match": {
                        "query": query,
                        "fields": search_fields,
                        "type": "best_fields",
                        "fuzziness": "AUTO",
                        "prefix_length": 2,
                    }
                }
            )
            # Exact match boost — precise hits outrank fuzzy matches
            should_clauses.append(
                {
                    "multi_match": {
                        "query": query,
                        "fields": search_fields,
                        "type": "best_fields",
                        "boost": 1.5,
                    }
                }
            )
            if len(words) > 1:
                # Cross-field: different words can match different fields
                should_clauses.append(
                    {
                        "multi_match": {
                            "query": query,
                            "fields": search_fields,
                            "type": "cross_fields",
                            "operator": "or",
                            "boost": 0.8,
                        }
                    }
                )
                # Phrase proximity with slop for near-matches
                should_clauses.append(
                    {
                        "multi_match": {
                            "query": query,
                            "fields": search_fields,
                            "type": "phrase",
                            "slop": 3,
                            "boost": 2.0,
                        }
                    }
                )

        return {"bool": {"should": should_clauses, "minimum_should_match": 1}}

    def _get_search_fields(
        self,
        has_speaker_filter: bool,
        use_exact: bool = False,
    ) -> list[str]:
        """Get search fields based on speaker filter and mode.

        Args:
            has_speaker_filter: Whether a speaker filter is active.
            use_exact: If True, use content.exact instead of content.

        Returns:
            List of boosted field names.
        """
        content_field = "content.exact^3" if use_exact else "content^3"
        content_exact = "content.exact^2" if not use_exact else None
        if has_speaker_filter:
            fields = [content_field]
            if content_exact:
                fields.append(content_exact)
            fields.append("title^2")
            return fields
        fields = [content_field]
        if content_exact:
            fields.append(content_exact)
        fields.extend(["title^2", "speaker^3"])
        return fields

    @staticmethod
    def _apply_sort_clause(
        body: dict[str, Any],
        sort_by: str,
        sort_order: str,
        page: int,
        page_size: int,
        use_search_pipeline: bool = False,
    ) -> int:
        """Apply sort and pagination to a search body.

        For relevance sorts, OpenSearch's default _score ordering is used and
        pagination is handled client-side via over-fetch. For non-relevance sorts
        with a search pipeline (hybrid/RRF), over-fetch is used because the RRF
        normalization pipeline does not support mixing _score with other sort
        criteria. For BM25-only non-relevance sorts, native sort and pagination
        are applied server-side.

        Args:
            body: Search body dict (modified in place).
            sort_by: Sort field name.
            sort_order: Sort direction ("asc" or "desc").
            page: Page number (1-indexed).
            page_size: Results per page.
            use_search_pipeline: If True, a search pipeline (RRF) is active
                and _score cannot be mixed with other sort criteria.

        Returns:
            The outer_size to use for the query.
        """
        if sort_by == "relevance" or use_search_pipeline:
            # Dynamic over-fetch: scale with page depth for full coverage.
            # Page 1/20 → 200, page 5/20 → 500, capped at SEARCH_MAX_OVERFETCH.
            min_fetch = page * page_size
            over_fetch = max(page_size * 10, min_fetch + page_size * 5)
            return min(over_fetch, settings.SEARCH_MAX_OVERFETCH)

        # BM25-only: server-side sort with _score as tiebreaker is safe
        sort_map = {
            "upload_time": "upload_time",
            "completed_at": "upload_time",
            "filename": "title.keyword",
            "duration": "duration",
            "file_size": "file_size",
        }
        sort_field = sort_map.get(sort_by, "upload_time")
        body["sort"] = [
            {sort_field: {"order": sort_order}},
            {"_score": {"order": "desc"}},
        ]
        body["from"] = (page - 1) * page_size
        return page_size

    def _build_collapsed_search_body(
        self,
        query: str,
        filters: list[dict[str, Any]],
        page: int,
        page_size: int,
        has_speaker_filter: bool,
        use_neural: bool,
        sort_by: str = "relevance",
        sort_order: str = "desc",
    ) -> dict[str, Any]:
        """Build a search body with native collapse + inner_hits.

        OpenSearch groups results by file_uuid server-side, returning only
        the top N groups with their inner segments. This eliminates the need
        to over-fetch thousands of chunks and group them in Python.

        Args:
            query: Search query text.
            filters: OpenSearch filter clauses.
            page: Page number (1-indexed).
            page_size: Results per page (number of collapsed groups).
            has_speaker_filter: Whether a speaker filter is active.
            use_neural: Whether to use neural query (server-side embedding).
            sort_by: Sort field.
            sort_order: Sort direction.

        Returns:
            OpenSearch search body dict with collapse configuration.
        """
        search_fields = self._get_search_fields(has_speaker_filter)
        text_query_clause = self._build_text_query(query, search_fields)
        highlight_fields = self._build_highlight_fields(has_speaker_filter)

        # Inner hits: top segments per file group
        inner_hits_config: dict[str, Any] = {
            "name": "segments",
            "size": SEARCH_MAX_SNIPPETS_PER_FILE,
            "sort": [{"_score": {"order": "desc"}}],
            "highlight": {"fields": highlight_fields},
            "_source": {"excludes": ["embedding"]},
        }

        collapse_config: dict[str, Any] = {
            "field": "file_uuid",
            "inner_hits": inner_hits_config,
            "max_concurrent_group_searches": settings.SEARCH_COLLAPSE_MAX_CONCURRENT,
        }

        if use_neural and query and query.strip():
            model_id = self._get_neural_model_id()
            if not model_id:
                logger.warning(
                    "Neural model_id lookup returned None during query construction; "
                    "falling through to BM25-only for query='%s'",
                    query,
                )
            if model_id:
                body: dict[str, Any] = {
                    "size": 0,  # Placeholder — set by _apply_sort_clause
                    "query": {
                        "hybrid": {
                            "queries": [
                                {
                                    "bool": {
                                        "must": [text_query_clause],
                                        "filter": filters,
                                    }
                                },
                                {
                                    "bool": {
                                        "must": [
                                            {
                                                "neural": {
                                                    "embedding": {
                                                        "query_text": query,
                                                        "model_id": model_id,
                                                        "k": settings.SEARCH_RRF_WINDOW_SIZE,
                                                    }
                                                }
                                            }
                                        ],
                                        "filter": filters,
                                    }
                                },
                            ]
                        }
                    },
                    "collapse": collapse_config,
                    "highlight": {"fields": highlight_fields},
                    "_source": {"excludes": ["embedding"]},
                    "track_total_hits": False,
                    # NOTE: Do NOT add "aggs" here. OpenSearch 3.4 has a bug
                    # where cardinality aggregations combined with hybrid query
                    # + collapse + RRF search_pipeline triggers an
                    # ArrayIndexOutOfBoundsException in score-ranker-processor.
                    # total_files is computed from collapsed results instead.
                }
                body["size"] = self._apply_sort_clause(
                    body, sort_by, sort_order, page, page_size, use_search_pipeline=True
                )
                return body

        # BM25-only collapse
        return self._build_collapsed_bm25_body(
            query,
            filters,
            page,
            page_size,
            has_speaker_filter,
            highlight_fields,
            sort_by,
            sort_order,
        )

    def _build_collapsed_bm25_body(
        self,
        query: str,
        filters: list[dict[str, Any]],
        page: int,
        page_size: int,
        has_speaker_filter: bool,
        highlight_fields: dict[str, Any] | None = None,
        sort_by: str = "relevance",
        sort_order: str = "desc",
    ) -> dict[str, Any]:
        """Build a BM25-only search body with native collapse.

        Used when neural search is unavailable but collapse is supported.

        Args:
            query: Search query text.
            filters: OpenSearch filter clauses.
            page: Page number (1-indexed).
            page_size: Results per page.
            has_speaker_filter: Whether a speaker filter is active.
            highlight_fields: Pre-built highlight config (reuses caller's if provided).
            sort_by: Sort field.
            sort_order: Sort direction.

        Returns:
            OpenSearch search body dict.
        """
        search_fields = self._get_search_fields(has_speaker_filter, use_exact=True)
        text_query_clause = self._build_text_query(query, search_fields)

        # Always rebuild highlight fields with use_exact=True to match BM25
        # search fields (content.exact). OpenSearch's unified highlighter uses
        # require_field_match=true by default, so highlight fields must match
        # the fields being queried.
        bm25_highlight_fields = self._build_highlight_fields(has_speaker_filter, use_exact=True)

        inner_hits_config: dict[str, Any] = {
            "name": "segments",
            "size": SEARCH_MAX_SNIPPETS_PER_FILE,
            "sort": [{"_score": {"order": "desc"}}],
            "highlight": {"fields": bm25_highlight_fields},
            "_source": {"excludes": ["embedding"]},
        }

        collapse_config: dict[str, Any] = {
            "field": "file_uuid",
            "inner_hits": inner_hits_config,
            "max_concurrent_group_searches": settings.SEARCH_COLLAPSE_MAX_CONCURRENT,
        }

        body: dict[str, Any] = {
            "size": 0,  # Placeholder — set by _apply_sort_clause
            "query": {
                "bool": {
                    "must": [text_query_clause],
                    "filter": filters,
                }
            },
            "collapse": collapse_config,
            "highlight": {"fields": bm25_highlight_fields},
            "_source": {"excludes": ["embedding"]},
            "track_total_hits": False,
            "aggs": {
                "total_files": {"cardinality": {"field": "file_uuid", "precision_threshold": 10000}}
            },
        }
        body["size"] = self._apply_sort_clause(body, sort_by, sort_order, page, page_size)
        return body

    @staticmethod
    def _detect_keyword_match_fallback(
        inner_source: dict[str, Any],
        word_patterns: list[tuple[str, re.Pattern[str], re.Pattern[str]]],
        match_sources: list[str],
    ) -> bool:
        """Detect keyword matches using word-boundary regex when highlights are lost.

        The RRF collapse bug in OpenSearch strips inner hit highlights.
        This fallback checks content/title/speaker using word-boundary patterns
        with stemming to avoid false positives (e.g. "art" matching "artificial").

        Args:
            inner_source: Inner hit _source dict.
            word_patterns: Pre-compiled (word, pattern, stem_pattern) tuples.
            match_sources: Mutable list of match sources to update.

        Returns:
            True if keyword match was detected.
        """
        has_match = False
        for _qw, pattern, stem_pattern in word_patterns:
            for field_text, source_name in [
                (inner_source.get("content", ""), "content"),
                (inner_source.get("title", ""), "title"),
                (inner_source.get("speaker", ""), "speaker"),
            ]:
                if pattern.search(field_text) or stem_pattern.search(field_text):
                    has_match = True
                    if source_name not in match_sources:
                        match_sources.append(source_name)
            if has_match:
                break
        return has_match

    @staticmethod
    def _generate_synthetic_snippet(
        content: str,
        word_patterns: list[tuple[str, re.Pattern[str], re.Pattern[str]]],
    ) -> str | None:
        """Generate a highlighted snippet when RRF collapse strips OpenSearch highlights.

        Args:
            content: Raw content text from the inner hit source.
            word_patterns: Pre-compiled (word, pattern, stem_pattern) tuples.

        Returns:
            Sanitized snippet with <mark> tags, or None if no match found.
        """
        for _qw, wp, sp in word_patterns:
            m = wp.search(content) or sp.search(content)
            if m:
                start = max(0, m.start() - 100)
                end = min(len(content), m.end() + 100)
                window = content[start:end]
                for _qw2, wp2, _sp2 in word_patterns:
                    window = wp2.sub(r"<mark>\g<0></mark>", window)
                prefix = "..." if start > 0 else ""
                suffix = "..." if end < len(content) else ""
                return _sanitize_html(prefix + window + suffix)
        return None

    def _process_inner_hits(
        self,
        inner_hit_list: list[dict[str, Any]],
        outer_score: float,
        query: str = "",
    ) -> tuple[list[SearchOccurrence], str, list[str], int, int, float]:
        """Convert inner hits into SearchOccurrence objects.

        Handles the case where OpenSearch hybrid queries with RRF normalization
        + collapse produce inner hits with score=0.0 and no highlights. In this
        case, query terms are checked against content text manually.

        Returns:
            Tuple of (occurrences, title_highlighted, match_sources,
            keyword_count, semantic_count, best_score).
        """
        occurrences: list[SearchOccurrence] = []
        keyword_count = 0
        semantic_count = 0
        title_highlighted = ""
        match_sources: list[str] = []
        best_score = outer_score

        # Pre-compile word-boundary patterns for keyword detection (hybrid fallback).
        # Uses word boundaries + stemming instead of substring 'in' to prevent
        # false positives like "art" matching "artificial".
        query_words = [w.lower() for w in query.split() if len(w) >= 2] if query else []
        word_patterns: list[tuple[str, re.Pattern[str], re.Pattern[str]]] = []
        if query_words:
            for qw in query_words:
                qw_stem = _get_word_stem(qw)
                word_patterns.append(
                    (
                        qw,
                        re.compile(rf"\b{re.escape(qw)}\w{{0,5}}\b", re.IGNORECASE),
                        re.compile(rf"\b{re.escape(qw_stem)}\w{{0,5}}\b", re.IGNORECASE),
                    )
                )

        for inner_hit in inner_hit_list:
            inner_source = inner_hit.get("_source", {})
            inner_score = inner_hit.get("_score", 0.0) or 0.0
            highlight = inner_hit.get("highlight", {})
            has_keyword_match = bool(highlight)

            # Hybrid + collapse fallback: when inner hits lose scores and
            # highlights (OpenSearch RRF limitation), detect keyword matches
            # using word-boundary regex with stemming.
            if not has_keyword_match and inner_score == 0.0 and word_patterns and outer_score > 0:
                has_keyword_match = self._detect_keyword_match_fallback(
                    inner_source,
                    word_patterns,
                    match_sources,
                )
                # Use outer score as fallback since inner scores are lost
                # to the RRF collapse bug.
                inner_score = outer_score

            snippet, match_type = _extract_snippet_and_match_type(inner_source, highlight)
            speaker_highlighted = _extract_highlighted_field(highlight, "speaker")

            # Synthetic highlights: when RRF collapse strips OpenSearch highlights
            # from keyword matches, generate <mark> tags using word patterns.
            if has_keyword_match and not highlight and word_patterns:
                content = inner_source.get("content", "")
                if content:
                    synthetic = self._generate_synthetic_snippet(content, word_patterns)
                    if synthetic:
                        snippet = synthetic

            if not title_highlighted:
                title_highlighted = _extract_highlighted_field(highlight, "title")

            # Track match sources from OpenSearch highlights
            if (
                "content" in highlight or "content.exact" in highlight
            ) and "content" not in match_sources:
                match_sources.append("content")
            if "title" in highlight and "title" not in match_sources:
                match_sources.append("title")
            if "speaker" in highlight and "speaker" not in match_sources:
                match_sources.append("speaker")

            if has_keyword_match:
                keyword_count += 1
            else:
                semantic_count += 1

            occurrences.append(
                SearchOccurrence(
                    snippet=snippet,
                    speaker=inner_source.get("speaker", ""),
                    start_time=inner_source.get("start_time", 0.0),
                    end_time=inner_source.get("end_time", 0.0),
                    chunk_index=inner_source.get("chunk_index", 0),
                    score=inner_score,
                    match_type=match_type,
                    speaker_highlighted=speaker_highlighted,
                    has_keyword_match=has_keyword_match,
                    highlight_type="keyword" if has_keyword_match else "semantic",
                )
            )
            if inner_score > best_score:
                best_score = inner_score

        return (
            occurrences,
            title_highlighted,
            match_sources,
            keyword_count,
            semantic_count,
            best_score,
        )

    @staticmethod
    def _normalize_relevance_percent(results: list[SearchHit]) -> None:
        """Normalize relevance_percent across results (20-99% range, +5% dual-match bonus)."""
        if not results:
            return
        all_scores = [h.relevance_score for h in results]
        score_min, score_max = min(all_scores), max(all_scores)
        score_range = score_max - score_min
        for h in results:
            if score_range > 0:
                pct = (h.relevance_score - score_min) / score_range
                h.relevance_percent = int(20 + pct * 79)
            else:
                h.relevance_percent = 70
            if h.has_both_match_types:
                h.relevance_percent = min(99, h.relevance_percent + 5)

    @staticmethod
    def _apply_semantic_demotion(grouped: list[SearchHit]) -> None:
        """Soft-demote low-scoring semantic-only results so they rank last.

        Never removes results — only halves relevance_score and sets
        semantic_confidence to "low" for results below the demotion threshold.
        """
        semantic_hits = [h for h in grouped if h.semantic_only]
        if not semantic_hits:
            return
        best_semantic = max(h.relevance_score for h in semantic_hits)
        min_semantic = settings.SEARCH_HYBRID_MIN_SCORE
        semantic_range = best_semantic - min_semantic
        if semantic_range <= 0:
            return
        demotion_threshold = min_semantic + semantic_range * settings.SEARCH_SEMANTIC_SUPPRESS_RATIO
        for h in grouped:
            if h.semantic_only and h.relevance_score < demotion_threshold:
                h.relevance_score *= 0.5
                h.semantic_confidence = "low"

    def _backfill_starved_groups(
        self,
        client: Any,
        grouped: list["SearchHit"],
        search_query: str,
        filters: list[dict[str, Any]],
        page: int,
        page_size: int,
        has_speaker_filter: bool,
        sort_by: str,
        sort_order: str,
        query: str,
    ) -> list["SearchHit"]:
        """Backfill file groups starved out of the hybrid RRF rank window.

        Runs the plain BM25 collapse query (immune to window starvation) and
        appends any file groups the hybrid pass missed. Backfilled hits get
        relevance scores rescaled strictly below the lowest hybrid score —
        BM25 raw scores live on a different scale than RRF scores and must
        never outrank the hybrid-ranked results.

        Best-effort: any failure returns the hybrid groups unchanged.
        """
        try:
            bm25_body = self._build_collapsed_bm25_body(
                search_query,
                filters,
                page,
                page_size,
                has_speaker_filter,
                sort_by=sort_by,
                sort_order=sort_order,
            )
            bm25_response = client.search(
                index=settings.OPENSEARCH_CHUNKS_INDEX,
                body=bm25_body,
            )
        except Exception as e:
            logger.warning(f"BM25 group backfill failed for query='{query}': {e}")
            return grouped

        bm25_grouped, _ = self._process_collapsed_results(bm25_response, query)
        seen = {hit.file_uuid for hit in grouped}
        new_hits = [hit for hit in bm25_grouped if hit.file_uuid not in seen]
        if not new_hits:
            return grouped

        # Rescale below the hybrid floor, preserving BM25 relative order
        floor = min((hit.relevance_score for hit in grouped), default=0.0)
        for i, hit in enumerate(new_hits):
            hit.relevance_score = floor - (i + 1) * 1e-6

        logger.info(
            f"Backfilled {len(new_hits)} file groups starved from the hybrid "
            f"window for query='{query}' (hybrid returned {len(grouped)})"
        )
        return grouped + new_hits

    def _process_collapsed_results(
        self,
        response: dict[str, Any],
        query: str,
    ) -> tuple[list[SearchHit], int]:
        """Process collapsed OpenSearch response into SearchHit objects.

        Each outer hit represents one file group. Inner hits contain the matching
        segments for that file.

        Args:
            response: OpenSearch response with collapse + inner_hits.
            query: Original search query for highlight classification.

        Returns:
            Tuple of (list of SearchHit, estimated total_files).
        """
        outer_hits = response.get("hits", {}).get("hits", [])
        # Use cardinality agg when available (BM25-only path), otherwise
        # fall back to the number of collapsed groups returned (hybrid path
        # omits aggs to work around OpenSearch 3.4 RRF + aggs crash).
        total_files_agg = response.get("aggregations", {}).get("total_files", {}).get(
            "value"
        ) or len(outer_hits)

        results: list[SearchHit] = []
        query_lower = query.lower().strip() if query else ""

        for outer_hit in outer_hits:
            source = outer_hit.get("_source", {})
            outer_score = outer_hit.get("_score", 0.0) or 0.0

            file_uuid = source.get("file_uuid", "")
            if not file_uuid:
                continue

            # Extract inner hits metadata
            inner_hits_data = outer_hit.get("inner_hits", {}).get("segments", {}).get("hits", {})
            inner_total = inner_hits_data.get("total", {})
            total_occurrences = (
                inner_total.get("value", 0)
                if isinstance(inner_total, dict)
                else int(inner_total)
                if inner_total
                else 0
            )

            # Build occurrences from inner hits
            (
                occurrences,
                title_highlighted,
                match_sources,
                keyword_count,
                semantic_count,
                best_score,
            ) = self._process_inner_hits(inner_hits_data.get("hits", []), outer_score, query)

            if not occurrences:
                continue

            occurrences.sort(
                key=lambda o: -(o.score + (0.001 if o.has_keyword_match else 0)),
            )

            # Determine semantic-only status
            is_semantic_only = keyword_count == 0
            semantic_confidence = ""
            if is_semantic_only:
                if "semantic" not in match_sources:
                    match_sources.append("semantic")
                semantic_high_threshold = getattr(
                    settings, "SEARCH_SEMANTIC_HIGH_CONFIDENCE", 0.015
                )
                semantic_confidence = "high" if best_score >= semantic_high_threshold else "low"

            # Detect metadata speaker match
            if query_lower:
                for speaker_name in source.get("speakers", []):
                    speaker_lower = speaker_name.lower()
                    if query_lower in speaker_lower or speaker_lower in query_lower:
                        if "metadata_speaker" not in match_sources:
                            match_sources.append("metadata_speaker")
                        break

            has_both = keyword_count > 0 and semantic_count > 0

            results.append(
                SearchHit(
                    file_uuid=file_uuid,
                    file_id=source.get("file_id", 0),
                    title=source.get("title", ""),
                    speakers=source.get("speakers", []),
                    tags=source.get("tags", []),
                    upload_time=source.get("upload_time", ""),
                    language=source.get("language", ""),
                    content_type=source.get("content_type", ""),
                    relevance_score=best_score,
                    occurrences=occurrences,
                    total_occurrences=max(total_occurrences, len(occurrences)),
                    title_highlighted=title_highlighted,
                    keyword_occurrences=keyword_count,
                    semantic_only=is_semantic_only,
                    semantic_confidence=semantic_confidence,
                    match_sources=match_sources,
                    duration=source.get("duration") or 0.0,
                    file_size=source.get("file_size") or 0,
                    semantic_occurrences=semantic_count,
                    has_both_match_types=has_both,
                )
            )

        self._normalize_relevance_percent(results)
        return results, int(total_files_agg)

    @staticmethod
    def _bucket_metadata(bucket: dict[str, Any]) -> dict[str, Any]:
        """Extract file metadata from a Phase 1 terms-aggregation bucket.

        Defined as a static method (not a closure) to avoid B023 lint errors
        and to ensure the bucket reference is properly bound.

        Args:
            bucket: Single bucket from the 'by_file' terms aggregation.

        Returns:
            Dict with title, language, content_type, speakers, tags,
            duration, file_size, upload_time, file_id.
        """

        def _first(agg: str) -> str:
            kw_b = bucket.get(agg, {}).get("buckets", [])
            return str(kw_b[0].get("key", "")) if kw_b else ""

        def _all(agg: str) -> list[str]:
            return [str(b.get("key", "")) for b in bucket.get(agg, {}).get("buckets", [])]

        return {
            "title": _first("title_kw"),
            "language": _first("language_kw"),
            "content_type": _first("content_type_kw"),
            "speakers": _all("speakers_kw"),
            "tags": _all("tags_kw"),
            "duration": float(bucket.get("max_duration", {}).get("value") or 0.0),
            "file_size": int(bucket.get("max_file_size", {}).get("value") or 0),
            "upload_time": str(bucket.get("max_upload_time", {}).get("value_as_string") or ""),
            "file_id": int(bucket.get("min_file_id", {}).get("value") or 0),
        }

    def _phase2_lookup(
        self,
        phase2_resp: dict[str, Any],
        query: str,
    ) -> dict[str, dict[str, Any]]:
        """Build a file_uuid → hit-details lookup from a Phase 2 BM25 response.

        Args:
            phase2_resp: OpenSearch collapse query response.
            query: Original query string (for inner-hit processing).

        Returns:
            Dict mapping file_uuid to occurrence/highlight details.
        """
        lookup: dict[str, dict[str, Any]] = {}
        for outer_hit in phase2_resp.get("hits", {}).get("hits", []):
            src = outer_hit.get("_source", {})
            fid = src.get("file_uuid", "")
            if not fid:
                continue
            inner_data = outer_hit.get("inner_hits", {}).get("segments", {}).get("hits", {})
            occs, title_hl, msrcs, kw_cnt, sem_cnt, score = self._process_inner_hits(
                inner_data.get("hits", []), 1.0, query
            )
            lookup[fid] = {
                "occurrences": occs,
                "title_highlighted": title_hl,
                "match_sources": msrcs,
                "keyword_count": kw_cnt,
                "semantic_count": sem_cnt,
                "best_score": score,
            }
        return lookup

    @staticmethod
    def _sort_buckets(
        sort_by: str,
        sort_order: str,
        buckets: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Sort aggregation buckets by the requested metadata field.

        Args:
            sort_by: Field to sort by (duration, file_size, upload_time, filename).
            sort_order: 'asc' or 'desc'.
            buckets: Raw aggregation bucket list from Phase 1.

        Returns:
            Sorted list of buckets.
        """

        def _key(b: dict[str, Any]) -> Any:
            if sort_by == "duration":
                return b.get("max_duration", {}).get("value") or 0.0
            if sort_by == "file_size":
                return b.get("max_file_size", {}).get("value") or 0
            if sort_by in ("upload_time", "completed_at"):
                return b.get("max_upload_time", {}).get("value_as_string") or ""
            if sort_by == "filename":
                kw_b = b.get("title_kw", {}).get("buckets", [])
                return (kw_b[0].get("key") or "").lower() if kw_b else ""
            return 0

        return sorted(buckets, key=_key, reverse=sort_order != "asc")

    def _build_search_hit_from_bucket(
        self,
        bucket: dict[str, Any],
        p2: dict[str, Any] | None,
        query_lower: str,
    ) -> SearchHit | None:
        """Merge Phase 1 metadata and Phase 2 highlights into a SearchHit.

        Args:
            bucket: Phase 1 aggregation bucket for one file.
            p2: Phase 2 lookup entry for the same file (may be None).
            query_lower: Lower-cased query for speaker-match detection.

        Returns:
            SearchHit, or None if the file has no usable occurrences.
        """
        meta = self._bucket_metadata(bucket)
        file_uuid = bucket["key"]
        title = meta["title"]

        if p2 and p2["occurrences"]:
            occurrences = p2["occurrences"]
            title_highlighted = p2["title_highlighted"] or title
            match_sources: list[str] = list(p2["match_sources"])
            keyword_count: int = p2["keyword_count"]
            semantic_count: int = p2["semantic_count"]
            best_score: float = p2["best_score"]
        else:
            occurrences = []
            title_highlighted = title
            match_sources = ["semantic"]
            keyword_count = 0
            semantic_count = 1
            best_score = 0.5

        if not occurrences:
            return None

        speakers: list[str] = meta["speakers"]
        if query_lower:
            for speaker_name in speakers:
                ql_in_sp = query_lower in speaker_name.lower()
                sp_in_ql = speaker_name.lower() in query_lower
                if (ql_in_sp or sp_in_ql) and "metadata_speaker" not in match_sources:
                    match_sources.append("metadata_speaker")
                    break

        is_semantic_only = keyword_count == 0
        semantic_confidence = ""
        if is_semantic_only:
            if "semantic" not in match_sources:
                match_sources.append("semantic")
            threshold = getattr(settings, "SEARCH_SEMANTIC_HIGH_CONFIDENCE", 0.010)
            semantic_confidence = "high" if best_score >= threshold else "low"

        has_both = keyword_count > 0 and semantic_count > 0
        total_occurrences = max(len(occurrences), keyword_count + semantic_count)

        return SearchHit(
            file_uuid=file_uuid,
            file_id=meta["file_id"],
            title=title,
            speakers=speakers,
            tags=meta["tags"],
            upload_time=meta["upload_time"],
            language=meta["language"],
            content_type=meta["content_type"],
            relevance_score=best_score,
            occurrences=occurrences,
            total_occurrences=total_occurrences,
            title_highlighted=title_highlighted,
            keyword_occurrences=keyword_count,
            semantic_only=is_semantic_only,
            semantic_confidence=semantic_confidence,
            match_sources=match_sources,
            duration=meta["duration"],
            file_size=meta["file_size"],
            semantic_occurrences=semantic_count,
            has_both_match_types=has_both,
        )

    def _apply_semantic_highlights(self, results: list[SearchHit], query: str) -> None:
        """Apply semantic highlights to semantic-only occurrences in-place.

        Args:
            results: List of SearchHit objects to mutate.
            query: Original query string.
        """
        if not query:
            return
        highlight_ctx = QueryHighlightContext.from_query(query)
        sem_words: set[str] = set()
        for hit in results:
            for occ in hit.occurrences:
                if not occ.has_keyword_match:
                    occ.snippet = _add_semantic_highlights(
                        occ.snippet, query, sem_words, highlight_ctx
                    )

    def _search_with_two_phase(
        self,
        query: str,
        search_query: str,
        filters: list[dict[str, Any]],
        page: int,
        page_size: int,
        sort_by: str,
        sort_order: str,
        search_mode: str,
        filters_applied: dict[str, Any],
        start_time: float,
        has_speaker_filter: bool,
    ) -> SearchResponse:
        """Two-phase search for non-relevance sorts with hybrid mode.

        Phase 1: Hybrid aggregation to discover ALL matching file UUIDs with
        their metadata. Eliminates the 200-file cap for metadata-field sorts.

        Phase 2: BM25 collapse on current page's file UUIDs to get proper
        snippet highlights and occurrence details.

        This is the industry-standard pattern used by large-scale search
        systems when combining semantic ranking with metadata-field sorting.

        Args:
            query: Original query (for display/caching).
            search_query: Cleaned query (operators removed).
            filters: OpenSearch filter clauses.
            page: Page number (1-indexed).
            page_size: Results per page.
            sort_by: Non-relevance sort field.
            sort_order: Sort direction.
            search_mode: Search mode string.
            filters_applied: Filter metadata for response.
            start_time: Timestamp for elapsed time.
            has_speaker_filter: Whether a speaker filter is active.

        Returns:
            SearchResponse with correctly sorted and paginated results.
        """
        client = get_opensearch_client()
        if not client:
            return self._empty_response(query, page, page_size)

        model_id = self._get_neural_model_id()
        if not model_id:
            # Fall back to single-phase BM25
            return self._search_with_collapse(
                query=query,
                search_query=search_query,
                filters=filters,
                page=page,
                page_size=page_size,
                sort_by=sort_by,
                sort_order=sort_order,
                search_mode=search_mode,
                filters_applied=filters_applied,
                start_time=start_time,
                has_speaker_filter=has_speaker_filter,
                use_neural=False,
            )

        search_fields = self._get_search_fields(has_speaker_filter)
        text_query_clause = self._build_text_query(search_query, search_fields)

        # ── Phase 1: Hybrid file discovery ──────────────────────────────────
        # OpenSearch 3.4 bug: aggs + hybrid + RRF pipeline triggers
        # ArrayIndexOutOfBoundsException.  Use a collapse-based approach
        # instead: fetch all matching file_uuids via collapse (no inner_hits,
        # lightweight), then extract metadata in Phase 2.
        t_p1 = time.time()
        phase1_body: dict[str, Any] = {
            "query": {
                "hybrid": {
                    "queries": [
                        {
                            "bool": {
                                "must": [text_query_clause],
                                "filter": filters,
                            }
                        },
                        {
                            "bool": {
                                "must": [
                                    {
                                        "neural": {
                                            "embedding": {
                                                "query_text": search_query or query,
                                                "model_id": model_id,
                                                "k": settings.SEARCH_RRF_WINDOW_SIZE,
                                            }
                                        }
                                    }
                                ],
                                "filter": filters,
                            }
                        },
                    ]
                }
            },
            "size": 5000,
            "track_total_hits": False,
            "_source": [
                "file_uuid",
                "file_id",
                "title",
                "speakers",
                "tags",
                "upload_time",
                "language",
                "content_type",
                "duration",
                "file_size",
            ],
            "collapse": {"field": "file_uuid"},
        }

        try:
            phase1_resp = client.search(
                index=settings.OPENSEARCH_CHUNKS_INDEX,
                body=phase1_body,
                params={"search_pipeline": settings.OPENSEARCH_SEARCH_PIPELINE},
            )
        except Exception as e:
            logger.warning(f"Two-phase Phase 1 failed, falling back to single-phase: {e}")
            return self._search_with_collapse(
                query=query,
                search_query=search_query,
                filters=filters,
                page=page,
                page_size=page_size,
                sort_by=sort_by,
                sort_order=sort_order,
                search_mode=search_mode,
                filters_applied=filters_applied,
                start_time=start_time,
                has_speaker_filter=has_speaker_filter,
                use_neural=False,
            )

        p1_ms = round((time.time() - t_p1) * 1000)

        # Convert collapsed hits to pseudo-bucket format for _sort_buckets
        # and _bucket_metadata compatibility.
        collapsed_hits = phase1_resp.get("hits", {}).get("hits", [])
        buckets = []
        for hit in collapsed_hits:
            src = hit.get("_source", {})
            fuid = src.get("file_uuid", "")
            if not fuid:
                continue
            buckets.append(
                {
                    "key": fuid,
                    "title_kw": {
                        "buckets": [{"key": src.get("title", "")}] if src.get("title") else []
                    },
                    "language_kw": {
                        "buckets": [{"key": src.get("language", "")}] if src.get("language") else []
                    },
                    "content_type_kw": {
                        "buckets": [{"key": src.get("content_type", "")}]
                        if src.get("content_type")
                        else []
                    },
                    "speakers_kw": {"buckets": [{"key": s} for s in (src.get("speakers") or [])]},
                    "tags_kw": {"buckets": [{"key": t} for t in (src.get("tags") or [])]},
                    "max_duration": {"value": src.get("duration") or 0.0},
                    "max_file_size": {"value": src.get("file_size") or 0},
                    "max_upload_time": {"value_as_string": src.get("upload_time") or ""},
                    "min_file_id": {"value": src.get("file_id") or 0},
                }
            )
        if not buckets:
            return self._sort_and_paginate(
                query,
                [],
                sort_by,
                sort_order,
                search_mode,
                page,
                page_size,
                filters_applied,
                start_time,
            )

        sorted_buckets = self._sort_buckets(sort_by, sort_order, buckets)

        total_files = len(sorted_buckets)
        total_pages = max(1, (total_files + page_size - 1) // page_size)
        start_idx = (page - 1) * page_size
        page_buckets = sorted_buckets[start_idx : start_idx + page_size]
        page_file_uuids = [b["key"] for b in page_buckets]

        if not page_file_uuids:
            elapsed_ms = round((time.time() - start_time) * 1000, 1)
            return SearchResponse(
                query=query,
                results=[],
                total_results=0,
                total_files=total_files,
                page=page,
                page_size=page_size,
                total_pages=total_pages,
                search_time_ms=elapsed_ms,
                filters_applied=filters_applied,
                search_mode=search_mode,
            )

        # ── Phase 2: BM25 collapse on page file UUIDs ────────────────────────
        # Fetch highlighted snippets for just the current page's files.
        t_p2 = time.time()
        highlight_fields = self._build_highlight_fields(has_speaker_filter, use_exact=True)
        bm25_fields = self._get_search_fields(has_speaker_filter, use_exact=True)
        p2_text_query = self._build_text_query(search_query, bm25_fields)

        phase2_body: dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [p2_text_query],
                    "filter": [{"terms": {"file_uuid": page_file_uuids}}],
                }
            },
            "collapse": {
                "field": "file_uuid",
                "inner_hits": {
                    "name": "segments",
                    "size": SEARCH_MAX_SNIPPETS_PER_FILE,
                    "sort": [{"_score": {"order": "desc"}}],
                    "highlight": {"fields": highlight_fields},
                    "_source": {"excludes": ["embedding"]},
                },
                "max_concurrent_group_searches": settings.SEARCH_COLLAPSE_MAX_CONCURRENT,
            },
            "size": len(page_file_uuids),
            "_source": {"excludes": ["embedding"]},
            "track_total_hits": False,
        }

        try:
            phase2_resp = client.search(
                index=settings.OPENSEARCH_CHUNKS_INDEX,
                body=phase2_body,
            )
        except Exception as e:
            logger.warning(f"Two-phase Phase 2 failed: {e}")
            phase2_resp = {"hits": {"hits": []}}

        p2_ms = round((time.time() - t_p2) * 1000)

        # Build lookup: file_uuid → (occurrences, title_highlighted, match_sources, ...)
        p2_hits_by_uuid = self._phase2_lookup(phase2_resp, query)

        # ── Merge Phase 1 metadata + Phase 2 highlights ──────────────────────
        query_lower = query.lower().strip() if query else ""
        results: list[SearchHit] = []
        for bucket in page_buckets:
            hit = self._build_search_hit_from_bucket(
                bucket, p2_hits_by_uuid.get(bucket["key"]), query_lower
            )
            if hit is not None:
                results.append(hit)

        self._normalize_relevance_percent(results)
        self._apply_semantic_highlights(results, query)

        elapsed_ms = round((time.time() - start_time) * 1000, 1)
        total_results = sum(
            h.keyword_occurrences if h.keyword_occurrences > 0 else h.total_occurrences
            for h in results
        )

        logger.info(
            f"TWO-PHASE SEARCH: p1={p1_ms}ms p2={p2_ms}ms "
            f"total_files={total_files} page_files={len(results)} sort={sort_by} query='{query}'"
        )

        return SearchResponse(
            query=query,
            results=results,
            total_results=total_results,
            total_files=total_files,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            search_time_ms=elapsed_ms,
            filters_applied=filters_applied,
            search_mode=search_mode,
        )

    def _search_with_collapse(
        self,
        query: str,
        search_query: str,
        filters: list[dict[str, Any]],
        page: int,
        page_size: int,
        sort_by: str,
        sort_order: str,
        search_mode: str,
        filters_applied: dict[str, Any],
        start_time: float,
        has_speaker_filter: bool,
        use_neural: bool,
    ) -> SearchResponse:
        """Execute search using native collapse + inner_hits.

        High-level orchestrator: builds collapsed query, executes it, processes
        results, applies semantic suppression, and returns SearchResponse.

        For non-relevance sorts, OpenSearch handles sorting and pagination
        server-side via native `sort` and `from` parameters. For relevance
        sorts, results are over-fetched and paginated client-side.

        Args:
            query: Original query (for display/caching).
            search_query: Cleaned query (operators removed).
            filters: OpenSearch filter clauses.
            page: Page number (1-indexed).
            page_size: Results per page.
            sort_by: Sort field.
            sort_order: Sort direction.
            search_mode: Search mode string.
            filters_applied: Filter metadata for response.
            start_time: Timestamp for elapsed time calculation.
            has_speaker_filter: Whether a speaker filter is active.
            use_neural: Whether to use neural query.

        Returns:
            SearchResponse with grouped results.
        """
        # For non-relevance sorts with hybrid mode: use two-phase approach to
        # avoid the over-fetch cap and ensure ALL matching files are sorted.
        if sort_by != "relevance" and use_neural:
            return self._search_with_two_phase(
                query=query,
                search_query=search_query,
                filters=filters,
                page=page,
                page_size=page_size,
                sort_by=sort_by,
                sort_order=sort_order,
                search_mode=search_mode,
                filters_applied=filters_applied,
                start_time=start_time,
                has_speaker_filter=has_speaker_filter,
            )

        client = get_opensearch_client()
        if not client:
            return self._empty_response(query, page, page_size)

        # Build collapsed search body
        t_build = time.time()
        search_body = self._build_collapsed_search_body(
            search_query,
            filters,
            page,
            page_size,
            has_speaker_filter,
            use_neural,
            sort_by,
            sort_order,
        )
        build_ms = round((time.time() - t_build) * 1000)

        # Execute with search pipeline if using hybrid
        t_opensearch = time.time()
        response: dict[str, Any] | None = None
        fell_back_to_bm25 = False
        try:
            if not client:
                return self._empty_response(query, page, page_size)
            search_params: dict[str, Any] = {}
            if use_neural:
                search_params["search_pipeline"] = settings.OPENSEARCH_SEARCH_PIPELINE
            response = client.search(
                index=settings.OPENSEARCH_CHUNKS_INDEX,
                body=search_body,
                params=search_params,
            )
        except Exception as e:
            if use_neural:
                # Retry once before falling back — transient errors are common
                logger.warning(f"Hybrid search failed (attempt 1), retrying: {e}")
                try:
                    response = client.search(
                        index=settings.OPENSEARCH_CHUNKS_INDEX,
                        body=search_body,
                        params=search_params,
                    )
                except Exception:
                    # Retry failed — fall back to BM25-only so users
                    # still get results instead of an empty page.
                    logger.warning(
                        "Hybrid search retry failed, falling back to BM25 for query='%s'",
                        query,
                    )
                    fell_back_to_bm25 = True
                    try:
                        fallback_body = self._build_collapsed_bm25_body(
                            search_query,
                            filters,
                            page,
                            page_size,
                            has_speaker_filter,
                            sort_by=sort_by,
                            sort_order=sort_order,
                        )
                        response = client.search(
                            index=settings.OPENSEARCH_CHUNKS_INDEX,
                            body=fallback_body,
                        )
                    except Exception as e3:
                        logger.error(f"BM25 fallback also failed: {e3}")
                        return self._empty_response(query, page, page_size)
            else:
                logger.error(f"Collapsed search failed: {e}")
                return self._empty_response(query, page, page_size)

        opensearch_ms = round((time.time() - t_opensearch) * 1000)

        if response is None:
            return self._empty_response(query, page, page_size)

        # Process collapsed results
        t_process = time.time()
        grouped, total_files_est = self._process_collapsed_results(response, query)
        process_ms = round((time.time() - t_process) * 1000)

        # Group-starvation backfill: with hybrid+RRF, collapse can only return
        # files present in the rank window. A query that densely matches ONE
        # file's chunks (e.g. a speaker name hitting every chunk's speaker^3
        # metadata on a labeled file) fills the window with that single file
        # and starves every other group — "Joe Rogan" returned 1 file from a
        # 2,500-file library. When the hybrid pass returns fewer groups than a
        # page, backfill missing groups from the BM25 collapse query (which
        # discovers groups normally); hybrid-ranked hits keep their positions,
        # backfilled keyword groups rank strictly below them.
        if use_neural and not fell_back_to_bm25 and search_query and len(grouped) < page_size:
            grouped = self._backfill_starved_groups(
                client=client,
                grouped=grouped,
                search_query=search_query,
                filters=filters,
                page=page,
                page_size=page_size,
                has_speaker_filter=has_speaker_filter,
                sort_by=sort_by,
                sort_order=sort_order,
                query=query,
            )

        self._apply_semantic_demotion(grouped)

        # Sort and paginate — always client-side for consistent behavior.
        # Hybrid/RRF over-fetches results (sort clause omitted to avoid pipeline
        # incompatibility), and BM25-only server-side sort may not cover all files
        # after semantic suppression. Unified path keeps logic simple.
        t_sort = time.time()
        result = self._sort_and_paginate(
            query,
            grouped,
            sort_by,
            sort_order,
            search_mode,
            page,
            page_size,
            filters_applied,
            start_time,
        )
        result.total_files = len(grouped)
        result.total_pages = max(1, (result.total_files + page_size - 1) // page_size)
        sort_ms = round((time.time() - t_sort) * 1000)

        # Deferred semantic highlighting for current page
        t_highlight = time.time()
        if query:
            highlight_ctx = QueryHighlightContext.from_query(query)
            sem_words: set[str] = set()
            for page_hit in result.results:
                for occ in page_hit.occurrences:
                    if not occ.has_keyword_match:
                        occ.snippet = _add_semantic_highlights(
                            occ.snippet, query, sem_words, highlight_ctx
                        )
        highlight_ms = round((time.time() - t_highlight) * 1000)

        total_ms = round((time.time() - start_time) * 1000)
        logger.info(
            f"COLLAPSE SEARCH TIMING: build={build_ms}ms opensearch={opensearch_ms}ms "
            f"process={process_ms}ms highlighting={highlight_ms}ms sort={sort_ms}ms "
            f"total={total_ms}ms files={len(grouped)} query='{query}'"
        )

        if fell_back_to_bm25:
            result._fell_back_to_bm25 = True

        return result

    def _empty_response(self, query: str, page: int, page_size: int) -> SearchResponse:
        """Return an empty search response."""
        return SearchResponse(
            query=query,
            results=[],
            total_results=0,
            total_files=0,
            page=page,
            page_size=page_size,
            total_pages=0,
            search_time_ms=0.0,
        )
