"""The ``file_type`` search filter matched NOTHING before this fix (lane C1,
issue #463's document-search work surfaced it).

``content_type`` in the ``transcript_chunks`` index stores a FULL MIME type
(``audio/mpeg``, ``video/mp4``, ...), but ``GET /api/search``'s ``file_type``
query param documents itself as accepting the coarse buckets ``audio``/
``video`` — and that is exactly what the SPA sends. The filter this replaces,
``{"terms": {"content_type": file_type}}``, compared the literal string
``"audio"`` against ``"audio/mpeg"`` and could never match. It went
unnoticed because the filter is normally combined with a text query that
still matches plenty on its own.
"""

from __future__ import annotations

from app.services.search.hybrid_search_service import HybridSearchService
from app.services.search.hybrid_search_service import _file_type_filter_clause


def test_coarse_audio_bucket_becomes_a_mime_prefix_match():
    clause = _file_type_filter_clause(["audio"])
    assert clause == {
        "bool": {"should": [{"prefix": {"content_type": "audio/"}}], "minimum_should_match": 1}
    }


def test_coarse_video_bucket_becomes_a_mime_prefix_match():
    clause = _file_type_filter_clause(["video"])
    assert clause == {
        "bool": {"should": [{"prefix": {"content_type": "video/"}}], "minimum_should_match": 1}
    }


def test_both_buckets_together_are_ored():
    clause = _file_type_filter_clause(["audio", "video"])
    should = clause["bool"]["should"]
    assert {"prefix": {"content_type": "audio/"}} in should
    assert {"prefix": {"content_type": "video/"}} in should
    assert clause["bool"]["minimum_should_match"] == 1


def test_bucket_matching_is_case_insensitive():
    assert _file_type_filter_clause(["AUDIO"]) == _file_type_filter_clause(["audio"])


def test_a_literal_mime_type_is_matched_exactly_not_dropped():
    """A caller that already has a full MIME string must still get a working
    filter rather than the value being silently discarded."""
    clause = _file_type_filter_clause(["audio/mpeg"])
    assert clause == {
        "bool": {"should": [{"term": {"content_type": "audio/mpeg"}}], "minimum_should_match": 1}
    }


def test_the_old_bare_terms_clause_would_never_have_matched_a_real_mime_value():
    """Pin the regression directly: the OLD filter shape against a REAL
    content_type value, proving it could never have matched."""
    old_clause = {"terms": {"content_type": ["audio"]}}
    real_document_content_type = "audio/mpeg"
    assert real_document_content_type not in old_clause["terms"]["content_type"]


def test_build_filters_uses_the_mime_prefix_clause_for_a_real_value():
    """End-to-end through ``_build_filters``: a coarse 'audio' request must
    produce a clause that actually matches a real indexed MIME value."""
    service = HybridSearchService()
    filters = service._build_filters(1, None, None, None, None, file_type=["audio"])
    clause = next(
        f
        for f in filters
        if "bool" in f and any("content_type" in str(v) for v in f["bool"].get("should", []))
    )
    assert {"prefix": {"content_type": "audio/"}} in clause["bool"]["should"]

    # Simulate OpenSearch's own prefix semantics: a document with a REAL MIME
    # value must satisfy this clause the way a `prefix` query would.
    real_value = "audio/mpeg"
    prefixes = [v["prefix"]["content_type"] for v in clause["bool"]["should"] if "prefix" in v]
    assert any(real_value.startswith(p) for p in prefixes), (
        "the fixed filter must match a real audio/* content_type value"
    )


def test_no_file_type_filter_is_added_when_unset():
    service = HybridSearchService()
    filters = service._build_filters(1, None, None, None, None, file_type=None)
    assert not any("content_type" in str(f) for f in filters)
