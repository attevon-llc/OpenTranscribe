"""Snippet highlighting: HTML sanitizing, language-aware stemming and semantic marks.

Everything that decides which words of a snippet carry ``<mark>`` tags, including the
keyword fallback for the hybrid RRF + collapse path, which strips OpenSearch's own
inner-hit highlights.
"""

import functools
import html as html_module
import re
import threading
from dataclasses import dataclass
from typing import Any

from nltk.stem import SnowballStemmer

from app.services.search.search_types import SearchHit


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


def _detect_keyword_match_fallback(
    inner_source: dict[str, Any],
    word_patterns: list[tuple[str, re.Pattern[str], re.Pattern[str]]],
    match_sources: list[str],
    sources: frozenset[str] | None = None,
) -> bool:
    """Detect keyword matches using word-boundary regex when highlights are lost.

    The RRF collapse bug in OpenSearch strips inner hit highlights.
    This fallback checks content/title/speaker using word-boundary patterns
    with stemming to avoid false positives (e.g. "art" matching "artificial").

    Args:
        inner_source: Inner hit _source dict.
        word_patterns: Pre-compiled (word, pattern, stem_pattern) tuples.
        match_sources: Mutable list of match sources to update.
        sources: Issue #760 — the requested source set. `None` means every
            unthreaded caller (full set). This fallback re-derives
            `match_sources` independently of highlights and runs on the
            DEFAULT RRF+collapse path, so it must honour the same gate
            `_get_search_fields` applies or it re-adds a deselected source
            (PC3/4.4a).

    Returns:
        True if keyword match was detected.
    """
    fields = [
        (inner_source.get("content", ""), "content"),
        (inner_source.get("title", ""), "title"),
        (inner_source.get("speaker", ""), "speaker"),
    ]
    if sources is not None:
        fields = [(text, name) for text, name in fields if name in sources]
    has_match = False
    for _qw, pattern, stem_pattern in word_patterns:
        for field_text, source_name in fields:
            if pattern.search(field_text) or stem_pattern.search(field_text):
                has_match = True
                if source_name not in match_sources:
                    match_sources.append(source_name)
        if has_match:
            break
    return has_match


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


def _apply_semantic_highlights(results: list[SearchHit], query: str) -> None:
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
