"""Field-level scoping for issue #760's `sources` parameter.

`HybridSearchService._get_search_fields`/`_build_highlight_fields`/
`_detect_keyword_match_fallback` gate which of the transcript leg's THREE
field selectors (content/title/speaker) participate in one `multi_match`
query. That `multi_match` is a `bool`/`should` clause — a hit matches if ANY
selected field matches — which is exactly the OR-union semantics decided for
#760 (J-760-10): a file matches if it matches ANY selected source. These
tests pin that at the field-selection layer, which is where the union
actually happens; `test_search_sources.py` pins the same property at the
endpoint layer (the transcript leg vs. the summary leg).

No OpenSearch needed — these are pure functions over dicts/lists.
"""

from __future__ import annotations

from app.services.search.hybrid_search_service import HybridSearchService


def _service() -> HybridSearchService:
    return HybridSearchService()


class TestGetSearchFieldsSourceGating:
    def test_none_sources_keeps_the_full_legacy_field_list(self):
        """Every unthreaded caller (sources=None) must see NO behavior change."""
        fields = _service()._get_search_fields(has_speaker_filter=False, sources=None)
        assert any(f.startswith("content") for f in fields)
        assert "title^2" in fields
        assert "speaker^3" in fields

    def test_content_only_excludes_title_and_speaker(self):
        fields = _service()._get_search_fields(
            has_speaker_filter=False, sources=frozenset({"content"})
        )
        assert any(f.startswith("content") for f in fields)
        assert "title^2" not in fields
        assert "speaker^3" not in fields

    def test_title_only_excludes_content_and_speaker(self):
        fields = _service()._get_search_fields(
            has_speaker_filter=False, sources=frozenset({"title"})
        )
        assert fields == ["title^2"]

    def test_speaker_only_excludes_content_and_title(self):
        fields = _service()._get_search_fields(
            has_speaker_filter=False, sources=frozenset({"speaker"})
        )
        assert fields == ["speaker^3"]

    def test_title_and_speaker_together_is_the_or_union_of_both(self):
        """Two sources selected -> a hit in title OR speaker must be returned.

        The `multi_match` this field list feeds is a `should` clause, so
        listing both fields IS the OR-union — there is no separate
        intersection step to defeat.
        """
        fields = _service()._get_search_fields(
            has_speaker_filter=False, sources=frozenset({"title", "speaker"})
        )
        assert set(fields) == {"title^2", "speaker^3"}

    def test_a_speaker_filter_still_drops_speaker_even_when_selected(self):
        """`has_speaker_filter` applies AFTER the source gate (§4.1)."""
        fields = _service()._get_search_fields(
            has_speaker_filter=True, sources=frozenset({"content", "title", "speaker"})
        )
        assert "speaker^3" not in fields
        assert "title^2" in fields


class TestBuildHighlightFieldsSourceGating:
    def test_none_sources_keeps_the_full_legacy_highlight_set(self):
        fields = _service()._build_highlight_fields(has_speaker_filter=False, sources=None)
        assert "content" in fields
        assert "title" in fields
        assert "speaker" in fields

    def test_title_only_highlights_only_title(self):
        fields = _service()._build_highlight_fields(
            has_speaker_filter=False, sources=frozenset({"title"})
        )
        assert set(fields) == {"title"}


class TestDetectKeywordMatchFallbackSourceGating:
    """The RRF+collapse fallback re-derives match_sources independently of
    highlights, on the DEFAULT search path — this is PC3/§4.4a, the part the
    original plan's superseded predecessor missed."""

    def test_deselecting_title_never_badges_a_title_match(self):
        import re

        inner_source = {"content": "nothing relevant", "title": "the budget report", "speaker": ""}
        pattern = re.compile(r"\bbudget\w{0,5}\b", re.IGNORECASE)
        word_patterns = [("budget", pattern, pattern)]
        match_sources: list[str] = []

        has_match = HybridSearchService._detect_keyword_match_fallback(
            inner_source, word_patterns, match_sources, frozenset({"content"})
        )

        assert has_match is False
        assert "title" not in match_sources

    def test_selecting_title_badges_a_title_match(self):
        import re

        inner_source = {"content": "nothing relevant", "title": "the budget report", "speaker": ""}
        pattern = re.compile(r"\bbudget\w{0,5}\b", re.IGNORECASE)
        word_patterns = [("budget", pattern, pattern)]
        match_sources: list[str] = []

        has_match = HybridSearchService._detect_keyword_match_fallback(
            inner_source, word_patterns, match_sources, frozenset({"title"})
        )

        assert has_match is True
        assert match_sources == ["title"]

    def test_none_sources_checks_every_field_legacy_behavior(self):
        import re

        inner_source = {"content": "nothing", "title": "the budget report", "speaker": ""}
        pattern = re.compile(r"\bbudget\w{0,5}\b", re.IGNORECASE)
        word_patterns = [("budget", pattern, pattern)]
        match_sources: list[str] = []

        has_match = HybridSearchService._detect_keyword_match_fallback(
            inner_source, word_patterns, match_sources, None
        )

        assert has_match is True
        assert match_sources == ["title"]
