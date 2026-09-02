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
from typing import TYPE_CHECKING
from typing import Any

from nltk.stem import SnowballStemmer

from app.core.config import settings
from app.core.constants import SEARCH_CACHE_MAX_SIZE
from app.core.constants import SEARCH_CACHE_TTL_SECONDS
from app.core.constants import SEARCH_DEFAULT_PAGE_SIZE
from app.core.constants import SEARCH_MAX_PAGE_SIZE
from app.core.constants import SEARCH_MAX_SNIPPETS_PER_FILE
from app.services.ingest_artifacts.index_mapping import chunk_plane_clause
from app.services.opensearch_service import get_opensearch_client
from app.services.opensearch_service import opensearch_client
from app.services.search.fusion import FusionConfig
from app.services.search.fusion import resolve_fusion
from app.services.search.fusion import search_pipeline_id
from app.services.search.indexing_service import ensure_chunks_index_exists
from app.services.search.indexing_service import ensure_search_pipeline_exists
from app.services.search.snippet_redaction import MASKABLE_CATEGORIES
from app.services.search.snippet_redaction import mask_snippets

if TYPE_CHECKING:  # pragma: no cover - import cost paid only by type checkers
    from app.services.redaction.config import EffectiveRedactionConfig

logger = logging.getLogger(__name__)


class _NotGiven:
    """Sentinel type for `_redact_snippets`'s `cfg` param, distinct from a real

    (already-resolved) `None`: `None` means "resolution was attempted and
    failed" (fail closed), while this type means "the caller did not resolve it
    at all, do it here" — the original self-resolving contract, kept for any
    caller of `_redact_snippets` other than `search()`'s own cache-key flow. A
    dedicated class (rather than a bare `object()`) so the parameter's type
    hint can name it explicitly instead of widening to `Any`.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<NOT_GIVEN>"


_NOT_GIVEN = _NotGiven()

#: What a snippet becomes when its policy cannot be applied. Withholding the
#: preview is a fail-closed choice: an unmasked one is a policy bypass, and the
#: result itself (title, timestamps, ranking) is unaffected.
WITHHELD_SNIPPET = "[redacted — masking unavailable]"


def _withhold_snippets(occurrences: list) -> None:
    """Replace every snippet with the withheld placeholder."""
    for occ in occurrences:
        occ.snippet = WITHHELD_SNIPPET


# Module-level caches for index/pipeline existence checks.
#
# ``_verified_pipelines`` is a SET of pipeline ids, not a bool. Since #363 a
# request may name its own fusion strategy, and each strategy is a separate
# OpenSearch search pipeline — a single "the pipeline is verified" flag would
# have let the FIRST strategy a process saw certify every later one, so the
# second arm of an A/B would attach a pipeline id that had never been created
# and OpenSearch would run the query **unfused**. That is a plausible number,
# not an error. ``reset_infrastructure_state`` clears the set.
_index_verified = False
_verified_pipelines: set[str] = set()
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

#: ISO 639-1 code -> Snowball language name, for the codes Snowball actually supports.
#: ``media_file.language`` (and therefore ``SearchHit.language``) holds an ISO code, while
#: ``SnowballStemmer.languages`` is keyed by English name, so the two never met and every
#: language stemmed as English. Codes absent here have no Snowball stemmer at all — Chinese,
#: Japanese and Korean are not stemmed languages — and resolve to the ``None`` branch below,
#: which skips stemming rather than applying a wrong one.
_SNOWBALL_BY_ISO = {
    "ar": "arabic",
    "da": "danish",
    "de": "german",
    "en": "english",
    "es": "spanish",
    "fi": "finnish",
    "fr": "french",
    "hu": "hungarian",
    "it": "italian",
    "nb": "norwegian",
    "nl": "dutch",
    "nn": "norwegian",
    "no": "norwegian",
    "pt": "portuguese",
    "ro": "romanian",
    "ru": "russian",
    "sv": "swedish",
}


def snowball_language_for(code: str | None) -> str | None:
    """Resolve an ISO 639-1 language code to a Snowball stemmer name.

    Args:
        code: ISO 639-1 code from a chunk document, e.g. ``"es"``. May carry a region
            suffix (``"pt-BR"``) or be empty/None on a file that never got detection.

    Returns:
        The Snowball language name, or ``None`` when the language has no Snowball
        stemmer. ``None`` means **do not stem** — falling back to English produces
        confident nonsense (``"hablando"`` -> ``"habland"`` under the English rules),
        which is worse than the unstemmed word because it then matches nothing.
    """
    if not code:
        return None
    base = code.strip().lower().replace("_", "-").split("-", 1)[0]
    return _SNOWBALL_BY_ISO.get(base)


@functools.lru_cache(maxsize=4096)
def _get_word_stem(word: str, language: str = "english") -> str:
    """Get stem using NLTK SnowballStemmer - matches OpenSearch snowball filter.

    Args:
        word: Word to stem.
        language: Snowball language NAME (not an ISO code) — resolve one with
            :func:`snowball_language_for` first. Unknown names fall back to English,
            which is only correct because every caller resolves beforehand.

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
    stem_language: str | None = "english",
) -> bool:
    """Check if a word should be highlighted based on semantic or stem matching.

    Args:
        word: The word to check.
        similar_words_set: Pre-computed set of semantically similar words.
        query_words: Lowercase query words (length >= 3).
        query_stems: Stemmed versions of query words.
        query_prefixes: Prefixes derived from query words.
        stem_language: Snowball language name for the document being highlighted, or
            ``None`` for a language with no stemmer (the word is then its own stem).

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
    word_stem = _get_word_stem(word_lower, stem_language) if stem_language else word_lower
    if word_stem in query_stems:
        return True

    # Check prefix matching
    if _matches_query_prefix(word_lower, word_stem, query_prefixes):
        return True

    # Check if word starts with any query word
    return _matches_query_word_start(word_lower, query_words)


@dataclass
class QueryHighlightContext:
    """Pre-computed query analysis for efficient semantic highlighting.

    The context is per **language**, not per query: stems only match when the query and
    the document are stemmed by the same rules, and a result page can mix languages.
    """

    query_words: list[str]
    query_stems: list[str]
    query_prefixes: list[str]
    stem_language: str | None = "english"

    @classmethod
    def from_query(cls, query: str, language: str | None = "en") -> "QueryHighlightContext":
        """Build context from a query string, computing stems/prefixes once.

        Args:
            query: The raw query string.
            language: ISO 639-1 code of the documents this context will highlight.
                ``None`` or an unstemmed language means the words are their own stems.
        """
        stem_language = snowball_language_for(language)
        query_words = [w.lower() for w in query.split() if len(w) >= 2]
        query_stems = [
            _get_word_stem(w, stem_language) if stem_language else w for w in query_words
        ]
        query_prefixes = [w[: max(4, len(w) - 2)] for w in query_words if len(w) >= 4]
        return cls(
            query_words=query_words,
            query_stems=query_stems,
            query_prefixes=query_prefixes,
            stem_language=stem_language,
        )


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
    if highlight_ctx is None:
        highlight_ctx = QueryHighlightContext.from_query(query)
    query_words = highlight_ctx.query_words
    query_stems = highlight_ctx.query_stems
    query_prefixes = highlight_ctx.query_prefixes
    stem_language = highlight_ctx.stem_language

    # Process snippet word by word, preserving non-word characters
    result = []
    current_pos = 0
    word_pattern = re.compile(r"\b([\w]+)\b", re.UNICODE)

    for match in word_pattern.finditer(snippet):
        result.append(snippet[current_pos : match.start()])
        word = match.group(1)
        if _should_highlight_word(
            word, similar_words_set, query_words, query_stems, query_prefixes, stem_language
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


def _resolve_redaction_config_for_cache(user_id: int) -> "EffectiveRedactionConfig | None":
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

    Returns:
        The effective config, or ``None`` when it could not be resolved at all
        (DB unreachable, etc.) — never raises, so a config failure degrades to
        the "unresolvable" cache bucket rather than breaking the search request.
    """
    from app.db.session_utils import session_scope
    from app.services.redaction.config import resolve_effective_config

    try:
        with session_scope() as db:
            return resolve_effective_config(db, user_id)
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


# A single OpenSearch `terms` clause is bounded by `index.max_terms_count`
# (65536 by default); quarantine is expected to be rare, so this cap is a
# defensive ceiling, not a normal operating limit. Exceeding it degrades to
# excluding only the oldest-quarantined files rather than failing the whole
# facet request.
_QUARANTINED_UUID_CAP = 10_000


def _quarantined_file_uuids() -> list[str]:
    """Every currently-quarantined file's uuid, for excluding facets built from it.

    Quarantine (``takedown_service.quarantine_file``) is Postgres-only — it never
    writes to OpenSearch — so a facet-aggregation query has no field of its own to
    filter on and must resolve the exclusion set here first. Not scoped to a
    caller: it feeds a ``must_not`` against a query already scoped to what that
    caller can see (``accessible_user_ids``), so a global list only ever narrows
    that intersection, never widens what a caller could learn.

    Returns:
        Quarantined file uuids as strings, capped at ``_QUARANTINED_UUID_CAP``.
        Empty (never raises) if the DB is unreachable — an aggregation request
        must not break because this best-effort exclusion could not run.
    """
    from app.db.session_utils import session_scope
    from app.models.media import MediaFile

    try:
        with session_scope() as db:
            rows = (
                db.query(MediaFile.uuid)
                .filter(MediaFile.is_quarantined.is_(True))
                .order_by(MediaFile.quarantined_at.desc().nullslast())
                .limit(_QUARANTINED_UUID_CAP)
                .all()
            )
            return [str(row[0]) for row in rows]
    except Exception:  # noqa: BLE001 — best-effort; see docstring
        logger.exception("Could not resolve quarantined file uuids for facet exclusion")
        return []


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


#: Coarse buckets the SPA's ``file_type`` filter sends today, mapped to the MIME
#: family prefix that actually appears in the indexed ``content_type`` field
#: (``audio/mpeg``, ``video/mp4``, ...). See :func:`_file_type_filter_clause`.
_FILE_TYPE_MIME_PREFIXES: dict[str, str] = {"audio": "audio/", "video": "video/"}


def _file_type_filter_clause(file_type: list[str]) -> dict[str, Any]:
    """Match ``content_type`` against coarse file-type filters (issue #463 lane).

    ``content_type`` stores a FULL MIME type — ``audio/mpeg``, ``video/mp4`` — but
    ``/api/search``'s ``file_type`` query param documents itself as accepting
    ``audio``/``video`` and that is exactly what the SPA sends. The filter this
    replaced, ``{"terms": {"content_type": file_type}}``, compares the literal
    string ``"audio"`` against ``"audio/mpeg"``: it can never match, so every
    file-type filter ever applied silently returned zero results. It went
    unnoticed because the filter is normally combined with a text query that
    still matches plenty on its own — a filter that excludes everything and a
    filter that was never applied look identical from the result page.

    Each requested value becomes a ``prefix`` match on its MIME family. A value
    that is not one of the two known coarse buckets is treated as a literal MIME
    string/prefix instead of being dropped, so a caller that already has a full
    MIME type (``audio/mpeg``) still gets an exact match rather than silently
    losing the filter a second way.

    Args:
        file_type: Non-empty list of coarse buckets and/or literal MIME values.

    Returns:
        A ``bool``/``should`` clause suitable for a ``filter`` array entry.
    """
    should: list[dict[str, Any]] = []
    for value in file_type:
        prefix = _FILE_TYPE_MIME_PREFIXES.get(value.lower())
        if prefix:
            should.append({"prefix": {"content_type": prefix}})
        else:
            should.append({"term": {"content_type": value}})
    return {"bool": {"should": should, "minimum_should_match": 1}}


def ensure_fusion_pipeline(fusion: FusionConfig | None = None) -> str:
    """Ensure ``fusion``'s search pipeline exists, and return the id to attach.

    Verification is cached **per pipeline id** so a process that has served RRF
    still creates the normalization pipeline the first time one is asked for.
    Attaching a ``search_pipeline`` that does not exist is not an error in
    OpenSearch's eyes at the point the id is chosen — it fails at query time, or
    worse, an earlier arm's pipeline is already there and the run silently
    measures it.

    Args:
        fusion: The requested strategy, or None for the configured default.

    Returns:
        The pipeline id to pass as the ``search_pipeline`` request parameter.
    """
    cfg = resolve_fusion(fusion)
    pipeline_id = search_pipeline_id(cfg)
    if pipeline_id in _verified_pipelines:
        return pipeline_id
    with _state_lock:
        if pipeline_id not in _verified_pipelines:
            ensure_search_pipeline_exists(cfg)
            # Recorded even when creation failed, matching the pre-#363 flag: a
            # cluster that cannot hold a pipeline is not fixed by asking it once
            # per request, and search degrades to unfused rather than stalling.
            _verified_pipelines.add(pipeline_id)
    return pipeline_id


def _ensure_infrastructure(fusion: FusionConfig | None = None) -> str:
    """Ensure the OpenSearch index and this request's search pipeline exist.

    Args:
        fusion: The requested strategy, or None for the configured default.

    Returns:
        The pipeline id to attach to this request.
    """
    global _index_verified
    if not _index_verified:
        with _state_lock:
            if not _index_verified:
                ensure_chunks_index_exists()
                _index_verified = True
    return ensure_fusion_pipeline(fusion)


def reset_infrastructure_state() -> None:
    """Reset index/pipeline verification state. Call after index recreation.

    Clears **every** verified pipeline id, not just the default one. A stale id
    left behind here is exactly the "cached infrastructure state that survived a
    config change" failure this repo keeps hitting: the next request would
    attach a pipeline nobody re-checked.
    """
    global _index_verified
    with _state_lock:
        _index_verified = False
        _verified_pipelines.clear()
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
        organization_id: int | None = None,
        file_uuid: str | None = None,
        fusion: FusionConfig | None = None,
    ) -> SearchResponse:
        """Execute hybrid search and return grouped results.

        Args:
            query: Search query text.
            user_id: Current user ID for filtering.
            fusion: Hybrid fusion strategy for **this request** (#363). None uses
                the configured default. The resolved pipeline id is part of the
                response cache key, so two strategies cannot serve each other's
                cached page.
            organization_id: Active org id (None = personal). Adds the tenant gate
                so cross-org transcript content never surfaces.
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

        # Redaction policy resolved BEFORE the cache lookup, not after (#86-
        # adjacent fix): the response this method is about to cache gets masked
        # under this policy, so the cache key must name it. Resolving it only at
        # `_redact_snippets` time — the pre-fix behaviour — meant a policy change
        # (a user flips masking, or the admin floor changes) took up to
        # `SEARCH_CACHE_TTL_SECONDS` to take effect on a repeated query, and two
        # policies could collide on one key if `user_id` were ever reused for a
        # tenant-shared cache in the future.
        redaction_cfg = _resolve_redaction_config_for_cache(user_id)
        policy_fingerprint = _redaction_policy_fingerprint(redaction_cfg)

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
            organization_id=organization_id,
            file_uuid=file_uuid,
            fusion_pipeline=search_pipeline_id(fusion),
            redaction_policy=policy_fingerprint,
        )
        cached = _get_cached_response(cache_key)
        if cached:
            return cached

        pipeline_id = _ensure_infrastructure(fusion)

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
            organization_id=organization_id,
            file_uuid=file_uuid,
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
            search_pipeline=pipeline_id,
        )

        # Read-time content redaction of snippets, for whichever of PII / profanity /
        # custom words this user's policy masks. Applied before caching so the
        # cache (keyed on user_id AND the policy fingerprint above) holds the
        # masked version. `redaction_cfg` is the SAME resolution used for the
        # cache key above — passed through rather than re-resolved, so the
        # config that decided the key is provably the config that did the
        # masking (one DB round trip, not two, and no window for the two to
        # silently disagree).
        self._redact_snippets(result, user_id, redaction_cfg)

        # Cache the response — but NOT if it fell back to BM25-only due to
        # a transient error, so the next request retries hybrid properly.
        if not result._fell_back_to_bm25:
            _set_cached_response(cache_key, result)
        else:
            logger.info("Skipping cache for BM25-fallback response (query='%s')", query)

        return result

    def _redact_snippets(
        self,
        result: SearchResponse,
        user_id: int,
        cfg: "EffectiveRedactionConfig | None | _NotGiven" = _NOT_GIVEN,
    ) -> None:
        """Mask this page's snippets under the requesting user's redaction policy.

        Snippets come out of the ``transcript_chunks`` index, which stores
        transcript text **UNREDACTED**, so they carry whatever the recording
        carried — including PII. This used to mask ``profanity`` and ``custom``
        only, on the stated grounds that "this path never carries PII spans";
        that was false in the way that matters, and a user whose policy masks
        **only** ``pii`` therefore had every snippet rendered verbatim, on a
        surface that spans collection and group shares (issue #86).

        The masking itself lives in ``snippet_redaction`` — ``<mark>`` handling,
        batching and the detector gate are all its problem. This method owns two
        decisions:

        - **Where the session ends.** ``cfg`` is resolved by the CALLER
          (``search()``, via ``_resolve_redaction_config_for_cache`` — it needs
          the same value to build the cache key, so resolving it a second time
          here would both duplicate a DB round trip and open a window for the
          two resolutions to disagree). That session already closed before this
          method runs, so a ~1 s Presidio pass never holds ``ACCESS SHARE`` open
          — the defect ``scripts/audit-session-lifetime.py`` exists to catch.
        - **Failing CLOSED, once.** Neither an unresolvable config (``cfg is
          None``) nor an unavailable detector may render an unmasked preview, so
          both withhold the page's snippet text. Results, counts and ranking are
          unaffected. Which detector failures count is
          ``redaction.config.blocking_detector_failures``, not a second rule
          written here: a broken Presidio must not cost their snippets to a user
          who never asked for PII masking.

        Args:
            result: The response whose snippets get masked in place.
            user_id: The requesting user (kept for the log line's subject; the
                actual policy is ``cfg`, already resolved for this user).
            cfg: The pre-resolved effective config, or ``None`` when it could
                not be resolved at all. Omitting the argument entirely
                (``_NOT_GIVEN``, the default) resolves it here instead — kept
                for callers outside ``search()``'s own cache-key flow, so this
                method's original resolve-it-yourself contract still holds for
                anyone driving it directly.
        """
        if isinstance(cfg, _NotGiven):
            cfg = _resolve_redaction_config_for_cache(user_id)

        occurrences = [
            occ
            for hit in getattr(result, "results", []) or []
            for occ in getattr(hit, "occurrences", []) or []
            if getattr(occ, "snippet", None)
        ]

        if cfg is None:
            if occurrences:
                logger.warning(
                    "Snippet redaction config unavailable for user %s; withholding snippet text",
                    user_id,
                )
                _withhold_snippets(occurrences)
            return

        if not occurrences:
            return
        if not cfg.enabled or not (set(cfg.enabled_categories) & MASKABLE_CATEGORIES):
            return

        try:
            masked = mask_snippets([occ.snippet for occ in occurrences], cfg)
        except Exception:  # noqa: BLE001 — never break search on redaction failure
            logger.exception("Snippet masking failed; withholding snippet text")
            _withhold_snippets(occurrences)
            return

        for occ, text in zip(occurrences, masked, strict=True):
            occ.snippet = text

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
        organization_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """Get auto-complete suggestions.

        Args:
            prefix: Search prefix text.
            user_id: Current user ID.
            limit: Maximum number of suggestions.
            organization_id: Active org id (None = personal) — tenant gate.

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

        from app.services.search.tenant_scope import org_filter_clauses

        scope_filter = [
            {"terms": {"accessible_user_ids": [user_id]}},
            *org_filter_clauses(organization_id),
            # Addendum G3: this reader builds its own filter list and so does not
            # inherit `_build_filters`' chunk-plane gate. Without the clause a
            # digest section pollutes title autocomplete and contributes a bogus
            # speaker bucket — derived text offered as if somebody had said it.
            chunk_plane_clause(),
        ]

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
                            "filter": scope_filter,
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
                            "filter": scope_filter,
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

    def get_available_filters(
        self, user_id: int, organization_id: int | None = None, is_admin: bool = False
    ) -> dict[str, Any]:
        """Return available filter options for the current user.

        Args:
            user_id: Current user ID.
            organization_id: Active org id (None = personal) — tenant gate.
            is_admin: When True, skip the quarantine exclusion — matches the
                admin review bypass ``search.py``'s ``_drop_quarantined_search_hits``
                already applies on the results page beside this endpoint.

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

        from app.services.search.tenant_scope import org_filter_clauses

        query_filter: list[dict[str, Any]] = [
            {"terms": {"accessible_user_ids": [user_id]}},
            *org_filter_clauses(organization_id),
            # Addendum G3: facet counts are per-document, so
            # digest sections would inflate every speaker and
            # tag bucket by a file-shaped amount.
            chunk_plane_clause(),
        ]
        query_must_not: list[dict[str, Any]] = []

        # A quarantine (takedown) is Postgres-only — takedown_service.quarantine_file
        # never touches OpenSearch — so without this, a quarantined file's speaker
        # names, tag names and upload-time date range keep appearing in these facets
        # for everyone who had access before the takedown, including the file's own
        # owner (who takedown_service.is_hidden_for says must not see it at all).
        # search.py's search_summaries already post-filters quarantined HITS off a
        # results page; there is no equivalent hit list here to post-filter, since
        # this endpoint returns aggregated buckets, not documents — so the exclusion
        # has to be built into the aggregation query itself. Quarantine is rare and
        # the OpenSearch `terms` clause is naturally bounded, unlike the general
        # accessible-file set, so excluding by quarantined uuid (rather than trying
        # to enumerate every accessible-and-non-quarantined uuid) keeps this cheap.
        quarantined_uuids = [] if is_admin else _quarantined_file_uuids()
        if quarantined_uuids:
            query_must_not.append({"terms": {"file_uuid": quarantined_uuids}})

        try:
            response = opensearch_client.search(
                index=index_name,
                body={
                    "size": 0,
                    "query": {
                        "bool": {
                            "filter": query_filter,
                            **({"must_not": query_must_not} if query_must_not else {}),
                        }
                    },
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
        organization_id: int | None = None,
        file_uuid: str | None = None,
        file_uuids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Build OpenSearch filter clauses.

        The ``accessible_user_ids`` term scopes to the caller; ``organization_id``
        adds the default-deny tenant gate (org term when set, else exclude any
        org-stamped doc). Community-edition invariance: ``organization_id`` is
        always None and docs are org-less, so the personal gate is a no-op.

        ``file_uuid`` scopes results to a single file — used by the in-page
        transcript find bar so it can list every match across the whole
        (paginated) transcript, including segments not yet loaded in the browser.

        ``file_uuids`` scopes to an explicit set of files — used by RAG chat,
        whose scope (files / collections / tags) is resolved to file uuids in
        Postgres first, so share and quarantine semantics are decided there
        rather than trusting the denormalized fields on the OpenSearch doc.
        """
        from app.services.search.tenant_scope import org_filter_clauses

        filters: list[dict[str, Any]] = [{"terms": {"accessible_user_ids": [user_id]}}]
        filters.extend(org_filter_clauses(organization_id))
        # The chunk plane, compatibility-armed. Search results are somebody's own
        # words; a digest is derived text and must not surface as if it were a
        # quote (addendum G6 — it would also carry NEITHER of the two read-time
        # masking treatments, since both are keyed to the shape they expect).
        # Stage 4's router is what adds the digest leg, deliberately and
        # separately, with its own citation shape.
        filters.append(chunk_plane_clause())

        if file_uuid:
            filters.append({"term": {"file_uuid": file_uuid}})
        if file_uuids is not None:
            # An empty resolved scope must match nothing, NOT everything.
            filters.append({"terms": {"file_uuid": list(file_uuids)}})
        if speakers:
            filters.append({"terms": {"speaker": speakers}})
        if tags:
            filters.append({"terms": {"tags": tags}})
        if file_type:
            filters.append(_file_type_filter_clause(file_type))
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

    def count_matches(
        self,
        query: str,
        user_id: int,
        file_uuid: str | None = None,
        organization_id: int | None = None,
    ) -> int:
        """Count transcript chunks matching ``query`` (optionally within one file).

        A deliberately lightweight path for the in-page transcript find bar. It runs a
        single ``size=0`` OpenSearch query — no query embedding, no RRF pipeline, no
        snippet extraction, no highlighting — so it stays cheap even when many users
        search at once (FastAPI runs this sync call in its threadpool and OpenSearch
        serves the searches concurrently). Returns the exact matching-chunk count
        (``track_total_hits``), used purely as a "matches exist beyond the loaded
        window" signal for progressive loading.
        """
        clean = (query or "").strip()
        if not clean:
            return 0
        client = get_opensearch_client()
        if not client:
            return 0

        _ensure_infrastructure()
        filters = self._build_filters(
            user_id,
            None,
            None,
            None,
            None,
            file_uuid=file_uuid,
            organization_id=organization_id,
        )
        text_query = self._build_text_query(clean, ["content", "content.exact"])
        body = {
            "size": 0,
            "track_total_hits": True,
            "query": {"bool": {"filter": filters, "must": [text_query]}},
        }
        try:
            resp = client.search(index=settings.OPENSEARCH_CHUNKS_INDEX, body=body)
        except Exception as exc:  # noqa: BLE001 — degrade to "unknown" on any OS error
            logger.warning(f"count_matches failed: {exc}")
            return 0

        total = resp.get("hits", {}).get("total", {})
        if isinstance(total, dict):
            return int(total.get("value", 0))
        return int(total or 0)

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

        Single-word queries use fuzziness for typo tolerance plus an exact
        match boost so precise hits still outrank fuzzy ones.  Multi-word
        queries add cross-field matching (terms can match different fields),
        phrase proximity with slop, AND a typo-tolerant clause that requires
        every term to fuzzily match (see below) rather than any one of them.
        Quoted phrases bypass fuzziness entirely.

        An OR-fuzzy clause is single-word-only on purpose (issue #606): OpenSearch's
        ``fuzziness: AUTO`` on a multi-term ``multi_match`` with the default OR
        operator matches if ANY one term fuzzily matches ANY token in the field (no
        "all terms" requirement), so a two-word query where only one word happens to
        have a same-length, edit-distance-2 near neighbour elsewhere in the corpus
        scores a full "keyword match" for the whole phrase — even when the other word
        has zero support in that document. Measured: the stemmed query term "explor"
        (from "exploration") sits at Levenshtein distance 2 from the unrelated
        stemmed term "export" (both 6 characters, so within ``fuzziness: AUTO``'s
        tolerance and past ``prefix_length: 2``'s "ex" prefix requirement), so
        "space exploration" fuzzy-matched every chunk containing "export" in a
        file with no mention of "space" at all — a false keyword hit that
        outranked the genuinely topical (semantic-only) result and every other
        candidate.

        Multi-word queries therefore get a SECOND, additive fuzzy clause instead
        (issue #606 follow-up finding 2) that forces ``operator: "and"`` alongside
        ``fuzziness: AUTO`` — every term must fuzzily match *something* in the same
        field before the clause can fire, which closes the #606 false-positive class
        (a single lucky near-neighbour can no longer carry the whole query) while
        still tolerating a genuine typo in any one word. This clause is additive
        alongside cross-field and phrase-slop, never a replacement for them:
        replacing the OR clause with an AND-fuzzy one measured 0 results for a
        legitimate non-typo multi-word query on the live index, because requiring
        every term to satisfy the *same* fuzzy leg is stricter than either
        cross-field OR or phrase-slop alone.

        Args:
            query: Search query text.
            search_fields: Fields to search.

        Returns:
            Query clause dict.
        """
        if not (query and query.strip()):
            return {"match_all": {}}

        # Word count for the multi-word decision must come from the RAW query
        # split, not a length-filtered one: filtering short tokens (e.g. "e",
        # "x") before counting can make a genuinely multi-word query like
        # "x exploration" read as single-word once its short token is
        # dropped, wrongly re-enabling the single-word fuzzy clause below and
        # reopening the exact false-positive class #606 fixed (issue #606
        # follow-up finding 1).
        is_phrase_query = query.startswith('"') and query.endswith('"')
        is_multi_word = len(query.split()) > 1

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
            if not is_multi_word:
                # Primary: best_fields with AUTO fuzziness for typo tolerance.
                # Single-word only — see the docstring for why a multi-word
                # query must not get this clause.
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
            if is_multi_word:
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
                # Typo tolerance for multi-word queries (issue #606 follow-up
                # finding 2): additive alongside cross-field/phrase above, never
                # a replacement — see the docstring for why `operator: "and"`
                # rather than plain fuzziness is required here.
                #
                # prefix_length 1, not the single-word clause's 2: measured live,
                # a same-length adjacent-letter transposition near the start of a
                # word (e.g. "space" -> "sapce") changes BOTH of the first two
                # characters, so prefix_length 2 demands an exact match on a
                # prefix the typo itself corrupted and silently excludes exactly
                # the typo class this clause exists to catch. prefix_length 1
                # (first character only) still found real fuzzy candidates in
                # every case measured, and the `operator: "and"` requirement
                # above is what keeps false positives down here, not the prefix.
                should_clauses.append(
                    {
                        "multi_match": {
                            "query": query,
                            "fields": search_fields,
                            "type": "best_fields",
                            "operator": "and",
                            "fuzziness": "AUTO",
                            "prefix_length": 1,
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
    ) -> tuple[dict[str, Any], bool]:
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
            Tuple of (OpenSearch search body dict with collapse configuration,
            whether the caller must attach the ``search_pipeline`` param — see
            the starvation check below for why this is not simply ``use_neural``).
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
                neural_clause = {
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
                }

                # A fully-starved keyword leg (zero BM25 matches — the normal
                # case for a genuinely semantic query, see `_build_text_query`)
                # must NOT be handed to the hybrid `search_pipeline` (issue
                # #606). OpenSearch 3.4's `collapse` + hybrid RRF
                # (`score-ranker-processor`) silently returns a WRONG,
                # QUERY-INDEPENDENT ranking whenever one of the two hybrid
                # legs matches nothing — measured directly: two unrelated
                # queries ("space exploration", "artificial intelligence"),
                # both with zero keyword hits, produced byte-identical
                # collapsed scores and file ordering, dominated by a file with
                # no topical relevance to either. The bug is specific to
                # `collapse` — the same starved-leg RRF fusion is correct at
                # the raw chunk level (no collapse), and a plain neural-only
                # collapse query (no `hybrid` wrapper at all) is also correct.
                # The pre-check below is a `count` (no scoring, no fetch), not
                # a second scored `search`, so it stays cheap.
                if self._bm25_leg_is_starved(text_query_clause, filters):
                    body = {
                        "size": 0,  # Placeholder — set by _apply_sort_clause
                        "query": neural_clause,
                        "collapse": collapse_config,
                        "highlight": {"fields": highlight_fields},
                        "_source": {"excludes": ["embedding"]},
                        "track_total_hits": False,
                    }
                    body["size"] = self._apply_sort_clause(
                        body, sort_by, sort_order, page, page_size, use_search_pipeline=False
                    )
                    return body, False

                body = {
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
                                neural_clause,
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
                return body, True

        # BM25-only collapse
        return (
            self._build_collapsed_bm25_body(
                query,
                filters,
                page,
                page_size,
                has_speaker_filter,
                highlight_fields,
                sort_by,
                sort_order,
            ),
            False,
        )

    def _bm25_leg_is_starved(
        self, text_query_clause: dict[str, Any], filters: list[dict[str, Any]]
    ) -> bool:
        """Whether the BM25/keyword leg matches zero chunk documents.

        A cheap ``count`` (no scoring, no fetch) against the same clause
        ``_build_collapsed_search_body`` would otherwise put in the hybrid
        query's keyword leg. See issue #606: this check exists specifically
        to route around a broken collapse+hybrid-RRF combination when the
        answer is yes.

        Args:
            text_query_clause: The clause `_build_text_query` produced.
            filters: OpenSearch filter clauses (tenant scope, quarantine, etc).
                Callers on this path already include `chunk_plane_clause()`
                (`_build_filters`), but this count explicitly ANDs it in again
                itself rather than trusting that — a reader of this index must
                decide its own plane, not inherit the caller's (#403 Stage 3
                addendum G3/G4; `tests/unit/test_chunk_plane_compat_arm.py`).

        Returns:
            True if the keyword leg would match nothing. Fails open to False
            (i.e. "assume it matches something, use the normal hybrid path")
            on any error — a starvation *false negative* only costs the
            OpenSearch-level bug being observed as narrow scores again as
            already documented (`artificial intelligence`'s prior skip);
            fail-closed here would instead risk a spurious neural-only
            fallback for every query if `count` itself is unhealthy.
        """
        client = get_opensearch_client()
        if not client:
            return False
        try:
            resp = client.count(
                index=settings.OPENSEARCH_CHUNKS_INDEX,
                body={
                    "query": {
                        "bool": {
                            "must": [text_query_clause],
                            "filter": [*filters, chunk_plane_clause()],
                        }
                    }
                },
            )
            return int(resp.get("count", 0)) == 0
        except Exception as e:  # noqa: BLE001 — best-effort pre-check, see docstring
            logger.debug(f"BM25 starvation pre-check failed, assuming non-empty: {e}")
            return False

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
        language: str | None = None,
    ) -> tuple[list[SearchOccurrence], str, list[str], int, int, float]:
        """Convert inner hits into SearchOccurrence objects.

        Handles the case where OpenSearch hybrid queries with RRF normalization
        + collapse produce inner hits with score=0.0 and no highlights. In this
        case, query terms are checked against content text manually.

        Args:
            inner_hit_list: The inner hits of one collapsed file.
            outer_score: The collapsed hit's score, used when inner scores are lost.
            query: The raw query string.
            language: ISO 639-1 code of THIS file, so the keyword fallback stems the
                query the way the document was analyzed. Defaulting to English made
                the fallback match nothing on a non-English file.

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
            stem_language = snowball_language_for(language)
            for qw in query_words:
                qw_stem = _get_word_stem(qw, stem_language) if stem_language else qw
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
            ) = self._process_inner_hits(
                inner_hits_data.get("hits", []),
                outer_score,
                query,
                source.get("language"),
            )

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
                threshold = settings.SEARCH_SEMANTIC_HIGH_CONFIDENCE
                semantic_confidence = "high" if best_score >= threshold else "low"

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
                inner_data.get("hits", []), 1.0, query, src.get("language")
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
            threshold = settings.SEARCH_SEMANTIC_HIGH_CONFIDENCE
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
        # One context per LANGUAGE on the page, not one per page: a stem only matches
        # when query and document were stemmed by the same rules, and a mixed-language
        # library returns a mixed-language page.
        contexts: dict[str, QueryHighlightContext] = {}
        sem_words: set[str] = set()
        for hit in results:
            ctx = contexts.get(hit.language)
            if ctx is None:
                ctx = contexts[hit.language] = QueryHighlightContext.from_query(query, hit.language)
            for occ in hit.occurrences:
                if not occ.has_keyword_match:
                    occ.snippet = _add_semantic_highlights(occ.snippet, query, sem_words, ctx)

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
        search_pipeline: str,
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
            search_pipeline: The fusion pipeline id resolved for this request
                (#363). Passed down rather than re-read from settings so an A/B
                arm cannot lose its strategy on the way to the wire.

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
                search_pipeline=search_pipeline,
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
                params={"search_pipeline": search_pipeline},
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
                search_pipeline=search_pipeline,
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
        search_pipeline: str,
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
            search_pipeline: The fusion pipeline id resolved for this request
                (#363), attached only when the body actually has two legs to
                fuse.

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
                search_pipeline=search_pipeline,
            )

        client = get_opensearch_client()
        if not client:
            return self._empty_response(query, page, page_size)

        # Build collapsed search body
        t_build = time.time()
        search_body, needs_search_pipeline = self._build_collapsed_search_body(
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

        # Execute with search pipeline if using hybrid. NOT simply `use_neural`
        # (issue #606): a fully keyword-starved query is routed to a pure
        # neural-only body with no `hybrid` wrapper to fuse, so no pipeline is
        # attached for it either — see `_build_collapsed_search_body`.
        t_opensearch = time.time()
        response: dict[str, Any] | None = None
        fell_back_to_bm25 = False
        try:
            if not client:
                return self._empty_response(query, page, page_size)
            search_params: dict[str, Any] = {}
            if needs_search_pipeline:
                search_params["search_pipeline"] = search_pipeline
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
        self._apply_semantic_highlights(result.results, query)
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
