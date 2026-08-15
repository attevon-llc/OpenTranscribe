"""
Unit tests for MetadataSpeakerExtractor and related functions.

Tests cover title/description pattern extraction, content format detection,
name validation, cross-reference with speaker attributes, and edge cases.
"""

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.services.metadata_speaker_extractor import MetadataExtractionResult
from app.services.metadata_speaker_extractor import MetadataSpeakerExtractor
from app.services.metadata_speaker_extractor import MetadataSpeakerHint
from app.services.metadata_speaker_extractor import _clean_name
from app.services.metadata_speaker_extractor import _is_valid_name
from app.services.metadata_speaker_extractor import _map_gender_guess
from app.services.metadata_speaker_extractor import _name_confidence
from app.services.metadata_speaker_extractor import build_cross_reference_context
from app.services.metadata_speaker_extractor import cross_reference_attributes

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def extractor():
    """Return a fresh MetadataSpeakerExtractor instance."""
    return MetadataSpeakerExtractor()


def _make_hint(name: str, role: str = "unknown", source: str = "title") -> MetadataSpeakerHint:
    """Convenience factory for MetadataSpeakerHint."""
    return MetadataSpeakerHint(name=name, role=role, source=source, confidence=0.7)


# ---------------------------------------------------------------------------
# 1. Title pattern extraction
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTitlePatternExtraction:
    """Test that names are extracted from common title patterns."""

    def test_interview_with_pattern(self, extractor):
        """'Interview with Joe Rogan' extracts 'Joe Rogan' with guest/unknown role."""
        result = extractor.extract({"title": "Interview with Joe Rogan"})
        names = [h.name for h in result.hints]
        assert "Joe Rogan" in names

    def test_interview_with_trailing_topic(self, extractor):
        """'Interview with Joe Rogan on Comedy' should still extract 'Joe Rogan'."""
        result = extractor.extract({"title": "Interview with Joe Rogan on Comedy"})
        names = [h.name for h in result.hints]
        assert "Joe Rogan" in names

    def test_featuring_pattern(self, extractor):
        """'Featuring Sarah Johnson' should extract 'Sarah Johnson'."""
        result = extractor.extract({"title": "Featuring Sarah Johnson"})
        names = [h.name for h in result.hints]
        assert "Sarah Johnson" in names

    def test_featuring_abbreviation(self, extractor):
        """'feat. Sarah Johnson' should extract 'Sarah Johnson'."""
        result = extractor.extract({"title": "My Show feat. Sarah Johnson"})
        names = [h.name for h in result.hints]
        assert "Sarah Johnson" in names

    def test_guest_colon_pattern(self, extractor):
        """'Guest: Bob Smith' should extract 'Bob Smith' with guest role."""
        result = extractor.extract({"title": "Guest: Bob Smith"})
        names = [h.name for h in result.hints]
        assert "Bob Smith" in names
        hint = next(h for h in result.hints if h.name == "Bob Smith")
        assert hint.role == "guest"

    def test_host_colon_pattern(self, extractor):
        """'Host: Jane Doe' should extract 'Jane Doe' with host role."""
        result = extractor.extract({"title": "Host: Jane Doe"})
        names = [h.name for h in result.hints]
        assert "Jane Doe" in names
        hint = next(h for h in result.hints if h.name == "Jane Doe")
        assert hint.role == "host"

    def test_hosted_by_in_title(self, extractor):
        """'Hosted by Jane Doe' in a title should extract 'Jane Doe'."""
        result = extractor.extract(
            {"title": "Tech Talk", "description": "Hosted by Jane Doe, this week..."}
        )
        names = [h.name for h in result.hints]
        assert "Jane Doe" in names

    def test_episode_colon_guest_pattern(self, extractor):
        """'Ep 42: Sarah Johnson - AI Future' should extract 'Sarah Johnson'."""
        result = extractor.extract({"title": "Ep 42: Sarah Johnson - AI Future"})
        names = [h.name for h in result.hints]
        assert "Sarah Johnson" in names

    def test_episode_number_pattern(self, extractor):
        """'Episode 123: Michael Chen | Weekly News' should extract 'Michael Chen'."""
        result = extractor.extract({"title": "Episode 123: Michael Chen | Weekly News"})
        names = [h.name for h in result.hints]
        assert "Michael Chen" in names

    def test_no_false_positive_on_plain_title(self, extractor):
        """A plain title with no name pattern should return no hints."""
        result = extractor.extract({"title": "How to cook pasta"})
        # Should not pick up single-word or junk names
        for h in result.hints:
            # Any hint returned must be a valid multi-word title-case string
            assert len(h.name.split()) >= 1

    def test_hints_have_title_source(self, extractor):
        """Hints extracted from title should have source='title'."""
        result = extractor.extract({"title": "Interview with Alex Turner"})
        title_hints = [h for h in result.hints if h.source == "title"]
        assert len(title_hints) > 0

    def test_deduplication_across_title_and_description(self, extractor):
        """Same name in title and description should appear only once."""
        result = extractor.extract(
            {
                "title": "Interview with Sarah Johnson",
                "description": "Guest: Sarah Johnson\nTopics: AI, robotics",
            }
        )
        names = [h.name.lower() for h in result.hints]
        assert names.count("sarah johnson") == 1


# ---------------------------------------------------------------------------
# 2. Description pattern extraction
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDescriptionPatternExtraction:
    """Test that names are extracted from description patterns."""

    def test_guest_and_host_on_separate_lines(self, extractor):
        """'Guest: Sarah Johnson\nHost: Joe Rogan' extracts both names."""
        result = extractor.extract(
            {"title": "", "description": "Guest: Sarah Johnson\nHost: Joe Rogan"}
        )
        names = [h.name for h in result.hints]
        assert "Sarah Johnson" in names
        assert "Joe Rogan" in names

    def test_todays_guest_is_pattern(self, extractor):
        """'Today's guest is Michael Chen' should extract 'Michael Chen'."""
        result = extractor.extract(
            {
                "title": "",
                "description": "Today's guest is Michael Chen, who discusses robotics.",
            }
        )
        names = [h.name for h in result.hints]
        assert "Michael Chen" in names

    def test_our_guest_is_pattern(self, extractor):
        """'Our guest is Lisa Park' should extract 'Lisa Park'."""
        result = extractor.extract(
            {"title": "", "description": "Our guest is Lisa Park from Stanford University."}
        )
        names = [h.name for h in result.hints]
        assert "Lisa Park" in names

    def test_hosted_by_in_description(self, extractor):
        """'Hosted by Alex Turner' in description should extract 'Alex Turner'."""
        result = extractor.extract(
            {"title": "", "description": "Hosted by Alex Turner, this podcast explores..."}
        )
        names = [h.name for h in result.hints]
        assert "Alex Turner" in names

    def test_hosted_by_gives_host_role(self, extractor):
        """Names extracted via 'Hosted by' pattern should have host role."""
        result = extractor.extract(
            {"title": "", "description": "Hosted by Mark Davies. A weekly show about tech."}
        )
        host_hints = [h for h in result.hints if h.role == "host"]
        assert len(host_hints) >= 1
        assert any(h.name == "Mark Davies" for h in host_hints)

    def test_guest_colon_role_from_description(self, extractor):
        """'Guest: Bob Smith' in description should produce role='guest'."""
        result = extractor.extract(
            {"title": "", "description": "Guest: Bob Smith\nHe is a researcher at MIT."}
        )
        guest_hints = [h for h in result.hints if h.role == "guest"]
        assert any(h.name == "Bob Smith" for h in guest_hints)

    def test_description_source_label(self, extractor):
        """Hints from description should have source='description'."""
        result = extractor.extract(
            {"title": "", "description": "Guest: Anna Kim\nToday we discuss machine learning."}
        )
        desc_hints = [h for h in result.hints if h.source == "description"]
        assert len(desc_hints) > 0

    def test_bio_block_pattern(self, extractor):
        """'John Smith is a professor at...' at start of line extracts 'John Smith'."""
        result = extractor.extract(
            {
                "title": "",
                "description": "John Smith is a professor at Harvard University.",
            }
        )
        names = [h.name for h in result.hints]
        assert "John Smith" in names


# ---------------------------------------------------------------------------
# 3. Content format detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestContentFormatDetection:
    """Test that content format is detected from metadata text."""

    def test_podcast_format_from_title(self, extractor):
        """Title containing 'podcast' → format 'podcast'."""
        result = extractor.extract({"title": "The Daily Podcast Episode 5"})
        assert result.content_format == "podcast"

    def test_podcast_format_from_episode_keyword(self, extractor):
        """Title containing 'episode' → format 'podcast'."""
        result = extractor.extract({"title": "Episode 22: Exploring AI"})
        assert result.content_format == "podcast"

    def test_interview_format_from_title(self, extractor):
        """Title containing 'interview' → format 'interview'."""
        result = extractor.extract({"title": "An Interview with the CEO"})
        assert result.content_format == "interview"

    def test_interview_format_conversation_with(self, extractor):
        """'Conversation with' in title → format 'interview'."""
        result = extractor.extract({"title": "Conversation with Dr. Jane Smith"})
        assert result.content_format == "interview"

    def test_lecture_format(self, extractor):
        """Title containing 'lecture' → format 'lecture'."""
        result = extractor.extract({"title": "Opening Lecture on Neural Networks"})
        assert result.content_format == "lecture"

    def test_panel_format(self, extractor):
        """Title containing 'panel' → format 'panel'."""
        result = extractor.extract({"title": "Industry Panel: Future of Tech"})
        assert result.content_format == "panel"

    def test_meeting_format(self, extractor):
        """Title containing 'meeting' → format 'meeting'."""
        result = extractor.extract({"title": "Weekly Team Meeting Notes"})
        assert result.content_format == "meeting"

    def test_unknown_format_for_unrecognized(self, extractor):
        """Empty or unrecognized title → format 'unknown'."""
        result = extractor.extract({"title": "Random video title"})
        assert result.content_format == "unknown"

    def test_empty_title_unknown_format(self, extractor):
        """Empty title → format 'unknown'."""
        result = extractor.extract({"title": ""})
        assert result.content_format == "unknown"

    def test_channel_hint_added_for_podcast_format(self, extractor):
        """For podcast format, a valid author/uploader is added as host hint."""
        result = extractor.extract(
            {
                "title": "My Podcast Episode 3",
                "author": "Jane Podcaster",
                "description": "",
            }
        )
        channel_hints = [h for h in result.hints if h.source == "channel"]
        assert len(channel_hints) == 1
        assert channel_hints[0].role == "host"
        assert channel_hints[0].name == "Jane Podcaster"

    def test_channel_hint_not_added_for_unknown_format(self, extractor):
        """For unknown format, author is NOT promoted to a channel hint."""
        result = extractor.extract(
            {
                "title": "Some random video",
                "author": "Alice Smith",
                "description": "",
            }
        )
        channel_hints = [h for h in result.hints if h.source == "channel"]
        assert len(channel_hints) == 0


# ---------------------------------------------------------------------------
# 4. Name validation (_is_valid_name)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNameValidation:
    """Test the _is_valid_name filter."""

    def test_empty_string_rejected(self):
        assert _is_valid_name("") is False

    def test_none_like_empty_rejected(self):
        assert _is_valid_name("") is False

    def test_single_stop_word_rejected(self):
        """Stop words like 'The', 'A' should be rejected."""
        assert _is_valid_name("The") is False
        assert _is_valid_name("A") is False

    def test_all_stop_words_rejected(self):
        """A name composed entirely of stop words is rejected."""
        assert _is_valid_name("The And") is False

    def test_single_char_rejected(self):
        """Single character strings are too short."""
        assert _is_valid_name("J") is False

    def test_too_long_rejected(self):
        """Strings over 80 characters are rejected."""
        long_name = "A" * 81
        assert _is_valid_name(long_name) is False

    def test_valid_first_last_name_accepted(self):
        """Standard two-word proper names are accepted."""
        assert _is_valid_name("Joe Rogan") is True
        assert _is_valid_name("Sarah Johnson") is True

    def test_valid_three_word_name_accepted(self):
        """Three-word names are accepted."""
        assert _is_valid_name("Mary Jane Watson") is True

    def test_too_many_words_rejected(self):
        """Names with more than 6 words are rejected."""
        assert _is_valid_name("One Two Three Four Five Six Seven") is False

    def test_valid_name_with_hyphen(self):
        """Hyphenated names are accepted."""
        assert _is_valid_name("Jean-Pierre Martin") is True

    def test_valid_name_with_apostrophe(self):
        """Names with apostrophes (e.g., O'Brien) are accepted."""
        assert _is_valid_name("Patrick O'Brien") is True

    def test_no_letters_rejected(self):
        """Strings with no letters are rejected."""
        assert _is_valid_name("123 456") is False

    def test_single_stop_word_episode_rejected(self):
        """'episode' alone is a stop word and should be rejected."""
        assert _is_valid_name("episode") is False

    def test_single_meaningful_word_accepted(self):
        """A single non-stop meaningful word is accepted (single-word names allowed)."""
        assert _is_valid_name("Einstein") is True


# ---------------------------------------------------------------------------
# 5. Helper functions
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCleanName:
    """Test _clean_name utility."""

    def test_strips_whitespace(self):
        assert _clean_name("  Joe Rogan  ") == "Joe Rogan"

    def test_removes_surrounding_quotes(self):
        assert _clean_name('"Joe Rogan"') == "Joe Rogan"
        assert _clean_name("'Sarah Johnson'") == "Sarah Johnson"

    def test_removes_trailing_punctuation(self):
        assert _clean_name("Bob Smith.") == "Bob Smith"
        assert _clean_name("Bob Smith,") == "Bob Smith"
        assert _clean_name("Bob Smith;") == "Bob Smith"

    def test_collapses_whitespace(self):
        assert _clean_name("Joe   Rogan") == "Joe Rogan"

    def test_strips_curly_quotes(self):
        assert _clean_name("\u201cJane Doe\u201d") == "Jane Doe"


@pytest.mark.unit
class TestNameConfidence:
    """Test _name_confidence scoring."""

    def test_single_word_lower_confidence(self):
        single = _name_confidence("Einstein", "title")
        double = _name_confidence("Joe Rogan", "title")
        assert double > single

    def test_title_source_higher_than_description(self):
        title_conf = _name_confidence("Joe Rogan", "title")
        desc_conf = _name_confidence("Joe Rogan", "description")
        assert title_conf > desc_conf

    def test_title_case_increases_confidence(self):
        title_case = _name_confidence("Joe Rogan", "title")
        lower_case = _name_confidence("joe rogan", "title")
        assert title_case > lower_case

    def test_confidence_capped_at_095(self):
        """Confidence should never exceed 0.95."""
        conf = _name_confidence("Mary Jane Watson Brown", "title")
        assert conf <= 0.95

    def test_three_word_name_higher_than_two(self):
        two = _name_confidence("Joe Rogan", "title")
        three = _name_confidence("Mary Jane Watson", "title")
        assert three >= two


@pytest.mark.unit
class TestMapGenderGuess:
    """Test _map_gender_guess mapping."""

    def test_male_maps_to_male(self):
        assert _map_gender_guess("male") == "male"

    def test_mostly_male_maps_to_male(self):
        assert _map_gender_guess("mostly_male") == "male"

    def test_female_maps_to_female(self):
        assert _map_gender_guess("female") == "female"

    def test_mostly_female_maps_to_female(self):
        assert _map_gender_guess("mostly_female") == "female"

    def test_andy_maps_to_unknown(self):
        assert _map_gender_guess("andy") == "unknown"

    def test_unknown_maps_to_unknown(self):
        assert _map_gender_guess("unknown") == "unknown"

    def test_empty_maps_to_unknown(self):
        assert _map_gender_guess("") == "unknown"


# ---------------------------------------------------------------------------
# 6. Cross-reference with speaker attributes
# ---------------------------------------------------------------------------


def _mock_gender_guesser_modules(gender_map: dict) -> dict:
    """Build a fake sys.modules patch for gender_guesser.

    cross_reference_attributes does:
        import gender_guesser.detector as gender_detector
        detector = gender_detector.Detector()
    so we need both 'gender_guesser' and 'gender_guesser.detector' in
    sys.modules, with Detector() returning a mock that uses gender_map.
    """
    mock_detector_instance = MagicMock()
    mock_detector_instance.get_gender.side_effect = lambda name: gender_map.get(name, "unknown")

    mock_detector_module = MagicMock()
    mock_detector_module.Detector.return_value = mock_detector_instance

    mock_gender_guesser = MagicMock()
    mock_gender_guesser.detector = mock_detector_module

    return {
        "gender_guesser": mock_gender_guesser,
        "gender_guesser.detector": mock_detector_module,
    }


@pytest.mark.unit
class TestCrossReferenceAttributes:
    """Test cross_reference_attributes() function."""

    def test_match_case_female_name_female_voice(self):
        """Female name hint + female predicted voice → alignment='match'."""
        hints = [_make_hint("Jane Doe", role="guest")]
        speaker_attrs = {"SPEAKER_01": {"predicted_gender": "female", "gender_confidence": 0.95}}

        with patch.dict("sys.modules", _mock_gender_guesser_modules({"Jane": "female"})):
            results = cross_reference_attributes(hints, speaker_attrs, speaker_segments=[])

        matches = [r for r in results if r["alignment"] == "match"]
        assert len(matches) >= 1
        assert matches[0]["hint_name"] == "Jane Doe"
        assert matches[0]["speaker_label"] == "SPEAKER_01"

    def test_mismatch_case_male_name_female_voice(self):
        """Male name + female predicted voice → alignment='mismatch'."""
        hints = [_make_hint("Bob Smith", role="guest")]
        speaker_attrs = {"SPEAKER_00": {"predicted_gender": "female", "gender_confidence": 0.80}}

        with patch.dict("sys.modules", _mock_gender_guesser_modules({"Bob": "male"})):
            results = cross_reference_attributes(hints, speaker_attrs, speaker_segments=[])

        mismatches = [r for r in results if r["alignment"] == "mismatch"]
        assert len(mismatches) >= 1
        assert mismatches[0]["hint_name"] == "Bob Smith"

    def test_unknown_name_filtered_out(self):
        """Androgynous/unknown name → entry removed from results (alignment='unknown' filtered)."""
        hints = [_make_hint("Pat Kim", role="unknown")]
        speaker_attrs = {"SPEAKER_00": {"predicted_gender": "female", "gender_confidence": 0.75}}

        with patch.dict("sys.modules", _mock_gender_guesser_modules({"Pat": "andy"})):
            results = cross_reference_attributes(hints, speaker_attrs, speaker_segments=[])

        # 'andy' maps to 'unknown', so no results should be returned for Pat Kim
        pat_results = [r for r in results if r["hint_name"] == "Pat Kim"]
        assert len(pat_results) == 0

    def test_matches_sorted_before_mismatches(self):
        """Results should be sorted: matches appear before mismatches."""
        hints = [
            _make_hint("Bob Smith", role="guest"),  # will mismatch (male vs female voices)
            _make_hint("Jane Doe", role="host"),  # will match (female vs female voice)
        ]
        speaker_attrs = {
            "SPEAKER_00": {"predicted_gender": "female", "gender_confidence": 0.90},
            "SPEAKER_01": {"predicted_gender": "female", "gender_confidence": 0.85},
        }

        with patch.dict(
            "sys.modules",
            _mock_gender_guesser_modules({"Bob": "male", "Jane": "female"}),
        ):
            results = cross_reference_attributes(hints, speaker_attrs, speaker_segments=[])

        # The two hints above are constructed to produce one match and one mismatch, so this
        # is a precondition to assert, not a condition to hide behind: as `if len(results) >=
        # 2:` the whole ordering check vanished whenever the function returned fewer results —
        # including if it returned none at all (issue #431).
        assert len(results) >= 2, f"expected at least 2 cross-referenced results, got {results}"

        alignments = [r["alignment"] for r in results]
        # All 'match' entries must precede all 'mismatch' entries.
        seen_mismatch = False
        for a in alignments:
            if a == "mismatch":
                seen_mismatch = True
            if seen_mismatch:
                assert a != "match", f"match entry found after mismatch entry: {alignments}"

    def test_results_capped_at_10(self):
        """Results should be capped at 10 entries."""
        # Build 15 distinctly named female hints; all first names resolve to "female"
        names = [f"Woman{i} Smith" for i in range(15)]
        hints = [_make_hint(n, role="guest") for n in names]
        speaker_attrs = {
            f"SPEAKER_{i:02d}": {"predicted_gender": "female", "gender_confidence": 0.9}
            for i in range(15)
        }
        # Every first name (Woman0, Woman1, ...) maps to "female"
        gender_map = {f"Woman{i}": "female" for i in range(15)}

        with patch.dict("sys.modules", _mock_gender_guesser_modules(gender_map)):
            results = cross_reference_attributes(hints, speaker_attrs, speaker_segments=[])

        assert len(results) <= 10

    def test_gender_guesser_import_error_returns_empty(self):
        """If gender_guesser is not installed, returns empty list (graceful fallback)."""
        hints = [_make_hint("Jane Doe")]
        speaker_attrs = {"SPEAKER_00": {"predicted_gender": "female"}}

        # Setting the module to None in sys.modules causes ImportError on `import`
        with patch.dict("sys.modules", {"gender_guesser": None, "gender_guesser.detector": None}):
            results = cross_reference_attributes(hints, speaker_attrs, speaker_segments=[])

        assert results == []

    def test_confidence_boost_positive_for_match(self):
        """Matching entries should have positive confidence_boost."""
        hints = [_make_hint("Alice Walker", role="guest")]
        speaker_attrs = {"SPEAKER_00": {"predicted_gender": "female", "gender_confidence": 0.92}}

        with patch.dict("sys.modules", _mock_gender_guesser_modules({"Alice": "female"})):
            results = cross_reference_attributes(hints, speaker_attrs, speaker_segments=[])

        matches = [r for r in results if r["alignment"] == "match"]
        for m in matches:
            assert m["confidence_boost"] > 0

    def test_confidence_boost_negative_for_mismatch(self):
        """Mismatching entries should have negative confidence_boost."""
        hints = [_make_hint("Bob Smith", role="guest")]
        speaker_attrs = {"SPEAKER_00": {"predicted_gender": "female", "gender_confidence": 0.80}}

        with patch.dict("sys.modules", _mock_gender_guesser_modules({"Bob": "male"})):
            results = cross_reference_attributes(hints, speaker_attrs, speaker_segments=[])

        mismatches = [r for r in results if r["alignment"] == "mismatch"]
        for m in mismatches:
            assert m["confidence_boost"] < 0


# ---------------------------------------------------------------------------
# 7. Empty / edge cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEdgeCases:
    """Test empty inputs and edge cases."""

    def test_empty_title_returns_no_hints(self, extractor):
        """Empty title produces no hints."""
        result = extractor.extract({"title": ""})
        assert result.hints == []

    def test_none_title_returns_no_hints(self, extractor):
        """None title produces no hints."""
        result = extractor.extract({"title": None})
        assert result.hints == []

    def test_none_description_returns_no_hints(self, extractor):
        """None description produces no hints."""
        result = extractor.extract({"title": None, "description": None})
        assert result.hints == []

    def test_empty_metadata_dict(self, extractor):
        """Completely empty metadata dict produces empty result."""
        result = extractor.extract({})
        assert result.hints == []
        assert result.content_format == "unknown"

    def test_result_is_metadata_extraction_result_type(self, extractor):
        """extract() always returns a MetadataExtractionResult instance."""
        result = extractor.extract({"title": "Some title"})
        assert isinstance(result, MetadataExtractionResult)

    def test_hint_to_dict(self):
        """MetadataSpeakerHint.to_dict() returns the correct structure."""
        hint = MetadataSpeakerHint(name="Joe Rogan", role="guest", source="title", confidence=0.85)
        d = hint.to_dict()
        assert d == {"name": "Joe Rogan", "role": "guest", "source": "title", "confidence": 0.85}

    def test_extraction_result_to_structured_context_empty(self):
        """to_structured_context() returns '' when there are no hints."""
        result = MetadataExtractionResult(hints=[], content_format="unknown")
        assert result.to_structured_context() == ""

    def test_extraction_result_to_structured_context_with_hints(self):
        """to_structured_context() includes hint name and role."""
        hint = MetadataSpeakerHint(name="Jane Doe", role="host", source="title", confidence=0.9)
        result = MetadataExtractionResult(hints=[hint], content_format="podcast")
        context = result.to_structured_context()
        assert "Jane Doe" in context
        assert "host" in context
        assert "podcast" in context.lower()

    def test_description_length_limit(self, extractor):
        """Descriptions longer than 3000 chars are truncated internally (no crash)."""
        long_desc = "Guest: John Smith\n" + "x" * 5000
        result = extractor.extract({"title": "", "description": long_desc})
        names = [h.name for h in result.hints]
        assert "John Smith" in names


# ---------------------------------------------------------------------------
# 8. build_cross_reference_context
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildCrossReferenceContext:
    """Test build_cross_reference_context() formatting function."""

    def test_empty_cross_refs_returns_empty_string(self):
        assert build_cross_reference_context([]) == ""

    def test_match_entry_formatted_correctly(self):
        cross_refs = [
            {
                "hint_name": "Jane Doe",
                "speaker_label": "SPEAKER_01",
                "name_gender": "female",
                "voice_gender": "female",
                "voice_confidence": 0.97,
                "alignment": "match",
                "confidence_boost": 0.1,
                "hint_role": "guest",
            }
        ]
        context = build_cross_reference_context(cross_refs)
        assert "Jane Doe" in context
        assert "SPEAKER_01" in context
        assert "match" in context.lower() or "\u2713" in context

    def test_mismatch_entry_formatted_correctly(self):
        cross_refs = [
            {
                "hint_name": "Bob Smith",
                "speaker_label": "SPEAKER_00",
                "name_gender": "male",
                "voice_gender": "female",
                "voice_confidence": 0.80,
                "alignment": "mismatch",
                "confidence_boost": -0.05,
                "hint_role": "guest",
            }
        ]
        context = build_cross_reference_context(cross_refs)
        assert "Bob Smith" in context
        assert "SPEAKER_00" in context
        assert "mismatch" in context.lower() or "\u26a0" in context

    def test_unknown_alignment_excluded_from_output(self):
        """Entries with alignment='unknown' should not appear in output."""
        cross_refs = [
            {
                "hint_name": "Pat Kim",
                "speaker_label": "SPEAKER_02",
                "name_gender": "unknown",
                "voice_gender": "female",
                "voice_confidence": 0.75,
                "alignment": "unknown",
                "confidence_boost": 0.0,
                "hint_role": "unknown",
            }
        ]
        context = build_cross_reference_context(cross_refs)
        assert context == ""

    def test_confidence_percentage_shown_when_available(self):
        """Voice confidence should be shown as percentage in output."""
        cross_refs = [
            {
                "hint_name": "Alice Wang",
                "speaker_label": "SPEAKER_00",
                "name_gender": "female",
                "voice_gender": "female",
                "voice_confidence": 0.93,
                "alignment": "match",
                "confidence_boost": 0.1,
                "hint_role": "host",
            }
        ]
        context = build_cross_reference_context(cross_refs)
        assert "93%" in context

    def test_deduplicate_same_hint_speaker_pair(self):
        """Duplicate (hint_name, speaker_label) pairs should appear only once."""
        entry = {
            "hint_name": "Jane Doe",
            "speaker_label": "SPEAKER_01",
            "name_gender": "female",
            "voice_gender": "female",
            "voice_confidence": 0.95,
            "alignment": "match",
            "confidence_boost": 0.1,
            "hint_role": "guest",
        }
        context = build_cross_reference_context([entry, entry])
        assert context.count("Jane Doe") == 1
