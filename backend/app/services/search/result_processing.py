"""Turning collapsed OpenSearch responses into ranked, paginated ``SearchHit`` pages."""

import logging
import re
import time
from typing import Any

from app.core.config import settings
from app.services.search.highlighting import _detect_keyword_match_fallback
from app.services.search.highlighting import _extract_highlighted_field
from app.services.search.highlighting import _extract_snippet_and_match_type
from app.services.search.highlighting import _generate_synthetic_snippet
from app.services.search.highlighting import _get_word_stem
from app.services.search.highlighting import snowball_language_for
from app.services.search.search_types import SearchHit
from app.services.search.search_types import SearchOccurrence
from app.services.search.search_types import SearchResponse

logger = logging.getLogger(__name__)


def _sort_and_paginate(
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
        h.keyword_occurrences if h.keyword_occurrences > 0 else h.total_occurrences for h in grouped
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


def _process_inner_hits(
    inner_hit_list: list[dict[str, Any]],
    outer_score: float,
    query: str = "",
    language: str | None = None,
    sources: frozenset[str] | None = None,
    assume_keyword_match: bool = False,
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
        assume_keyword_match: Every inner hit is a keyword match whether or
            not it carries a highlight. True only for a BM25-only body with a
            non-empty query, whose every hit matched the text query by
            construction — the split path (#1064) fetches highlights for the
            displayed page only, so the rest of the groups arrive without them.

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
        has_keyword_match = bool(highlight) or assume_keyword_match

        # Hybrid + collapse fallback: when inner hits lose scores and
        # highlights (OpenSearch RRF limitation), detect keyword matches
        # using word-boundary regex with stemming.
        if not has_keyword_match and inner_score == 0.0 and word_patterns and outer_score > 0:
            has_keyword_match = _detect_keyword_match_fallback(
                inner_source,
                word_patterns,
                match_sources,
                sources,
            )
            # Use outer score as fallback since inner scores are lost
            # to the RRF collapse bug.
            inner_score = outer_score

        snippet, match_type = _extract_snippet_and_match_type(inner_source, highlight)
        speaker_highlighted = _extract_highlighted_field(highlight, "speaker")

        # Synthetic highlights: when RRF collapse strips OpenSearch highlights
        # from keyword matches, generate <mark> tags using word patterns.
        # Not for an assumed match: its real highlights come later, for the
        # page only, and regex-marking every over-fetched segment first was
        # most of the split path's Python time (#1064).
        if has_keyword_match and not highlight and word_patterns and not assume_keyword_match:
            content = inner_source.get("content", "")
            if content:
                synthetic = _generate_synthetic_snippet(content, word_patterns)
                if synthetic:
                    snippet = synthetic

        if not title_highlighted:
            title_highlighted = _extract_highlighted_field(highlight, "title")

        # Track match sources from OpenSearch highlights. Gated on the
        # requested source set (issue #760, PC3/4.4a): a deselected field
        # is never queried, so OpenSearch cannot highlight it — but guard
        # explicitly anyway rather than depend on that being the only path.
        want_content = sources is None or "content" in sources
        want_title = sources is None or "title" in sources
        want_speaker = sources is None or "speaker" in sources
        if (
            want_content
            and ("content" in highlight or "content.exact" in highlight)
            and "content" not in match_sources
        ):
            match_sources.append("content")
        if want_title and "title" in highlight and "title" not in match_sources:
            match_sources.append("title")
        if want_speaker and "speaker" in highlight and "speaker" not in match_sources:
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


def _process_collapsed_results(
    response: dict[str, Any],
    query: str,
    is_fused_rrf: bool = False,
    sources: frozenset[str] | None = None,
    assume_keyword_match: bool = False,
) -> tuple[list[SearchHit], int]:
    """Process collapsed OpenSearch response into SearchHit objects.

    Each outer hit represents one file group. Inner hits contain the matching
    segments for that file.

    Args:
        response: OpenSearch response with collapse + inner_hits.
        query: Original search query for highlight classification.
        is_fused_rrf: Whether `response` came from the fused hybrid RRF
            query (`_build_collapsed_search_body`'s `hybrid` body, with a
            search pipeline attached). `SEARCH_SEMANTIC_HIGH_CONFIDENCE`
            was tuned against RRF scores (~0.016 at rank 1); on any other
            path (plain BM25, or the `_bm25_leg_is_starved` raw-cosinesimil
            arm) that same threshold reads "high" for nearly every result
            (issue #698). Confidence is only labelled when this is True —
            everywhere else `semantic_confidence` stays "".
        assume_keyword_match: Passed to `_process_inner_hits`; see there.

    Returns:
        Tuple of (list of SearchHit, estimated total_files).
    """
    outer_hits = response.get("hits", {}).get("hits", [])
    # Use cardinality agg when available (BM25-only path), otherwise
    # fall back to the number of collapsed groups returned (hybrid path
    # omits aggs to work around OpenSearch 3.4 RRF + aggs crash).
    total_files_agg = response.get("aggregations", {}).get("total_files", {}).get("value") or len(
        outer_hits
    )

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
        ) = _process_inner_hits(
            inner_hits_data.get("hits", []),
            outer_score,
            query,
            source.get("language"),
            sources,
            assume_keyword_match=assume_keyword_match,
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
            if is_fused_rrf:
                threshold = settings.SEARCH_SEMANTIC_HIGH_CONFIDENCE
                semantic_confidence = "high" if best_score >= threshold else "low"

        # Detect metadata speaker match — belongs to the Speaker pill
        # (issue #760, PC4.4b): gate on `speaker` being in the requested
        # source set, or a Speaker-deselected request still badges a hit
        # as a speaker match.
        if query_lower and (sources is None or "speaker" in sources):
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

    _normalize_relevance_percent(results)
    return results, int(total_files_agg)
