"""The text side of a transcript search query: operators, fields, BM25 clauses, highlights."""

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


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


def _build_highlight_fields(
    has_speaker_filter: bool,
    use_exact: bool = False,
    sources: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Build highlight field configuration shared by all search paths.

    Args:
        has_speaker_filter: Whether a speaker filter is active.
        use_exact: If True, use content.exact instead of content for BM25-only mode.
        sources: Issue #760 — mirrors `_get_search_fields`'s gating. Threaded
            mainly for symmetry with the query fields (OpenSearch's
            `require_field_match: true` default already means an unqueried
            field produces no highlight regardless).

    Returns:
        Highlight fields dict for OpenSearch.
    """
    want_content = sources is None or "content" in sources
    want_title = sources is None or "title" in sources
    want_speaker = sources is None or "speaker" in sources

    content_field = "content.exact" if use_exact else "content"
    fields: dict[str, Any] = {}
    if want_content:
        fields[content_field] = {
            "pre_tags": ["<mark>"],
            "post_tags": ["</mark>"],
            "fragment_size": 200,
            "number_of_fragments": 3,
        }
    if want_title:
        fields["title"] = {
            "pre_tags": ["<mark>"],
            "post_tags": ["</mark>"],
            "number_of_fragments": 0,
        }
    if want_speaker and not has_speaker_filter:
        fields["speaker"] = {
            "pre_tags": ["<mark>"],
            "post_tags": ["</mark>"],
            "number_of_fragments": 0,
        }
    return fields


def _unshingled_fields(search_fields: list[str]) -> list[str]:
    """Drop the shingled ``content`` field when ``content.exact`` is also searched.

    ``content`` indexes filler shingles (``remot control _`` for a removed stopword).
    A fuzzy match can land on one, and the highlighter then marks its whole offset
    range, running past the matched phrase. ``content.exact`` still carries the typo
    tolerance; the non-fuzzy clauses keep the stemmed ``content`` field.
    """
    has_exact = any(f.split("^")[0] == "content.exact" for f in search_fields)
    if not has_exact:
        return search_fields
    return [f for f in search_fields if f.split("^")[0] != "content"]


def _build_text_query(
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
    (issue #606 follow-up) that forces ``operator: "and"`` alongside
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
    # follow-up).
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
            # Typo tolerance for multi-word queries (issue #606 follow-up):
            # additive alongside cross-field/phrase above, never
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
                        "fields": _unshingled_fields(search_fields),
                        "type": "best_fields",
                        "operator": "and",
                        "fuzziness": "AUTO",
                        "prefix_length": 1,
                    }
                }
            )

    return {"bool": {"should": should_clauses, "minimum_should_match": 1}}


def _get_search_fields(
    has_speaker_filter: bool,
    use_exact: bool = False,
    sources: frozenset[str] | None = None,
) -> list[str]:
    """Get search fields based on speaker filter, mode, and source selection.

    Args:
        has_speaker_filter: Whether a speaker filter is active.
        use_exact: If True, use content.exact instead of content.
        sources: Issue #760 — which of {content, title, speaker} the caller
            asked for. `None` means "every unthreaded caller" and keeps the
            historical full field list (back-compat). An explicit set gates
            each field group; the `has_speaker_filter` rule is applied
            AFTER the gate, so a speaker filter still drops `speaker^3`
            even when `speaker` is a selected source.

    Returns:
        List of boosted field names.
    """
    want_content = sources is None or "content" in sources
    want_title = sources is None or "title" in sources
    want_speaker = sources is None or "speaker" in sources

    content_field = "content.exact^3" if use_exact else "content^3"
    content_exact = "content.exact^2" if not use_exact else None

    fields: list[str] = []
    if want_content:
        fields.append(content_field)
        if content_exact:
            fields.append(content_exact)
    if want_title:
        fields.append("title^2")
    if want_speaker and not has_speaker_filter:
        fields.append("speaker^3")
    return fields
