"""Real behavioral tests for ``app/services/search/snippet_redaction.py`` (issue #474).

``mask_snippets`` masks search-result previews at read time. It is deliberately exercised
here through the **profanity** and **custom** categories only (never ``pii``): both are
cheap regex/wordlist detectors (``app/services/redaction/detectors/wordlist.py``), so these
tests run real detection and real masking with no Presidio/GLiNER model weights, no
``@pytest.mark.models``, and no mocking of production logic. "damn"/"shit" are real entries
in the curated list at ``app/services/redaction/data/profanity_en.txt``.

Coverage: the enable/category gating short-circuits, the ``<mark>`` split-and-restore
round trip (including a span crossing a tag boundary), HTML-entity decode/re-escape,
unicode custom words, and the fail-closed ``SnippetMaskingUnavailableError`` path (the
detector failure is simulated the same way ``test_chat_output_redaction.py``'s
``_StubDetector`` simulates a down Presidio — this module has no seam of its own to
inject a real detector outage into, so the class method is monkeypatched for exactly one
call to prove ``blocking_detector_failures`` is actually wired up end to end).

⚠️ One test in ``TestMarkTagBoundary`` (``test_a_custom_word_split_across_a_mark_tag_
boundary_is_still_masked``) pins the CORRECT behavior and is currently RED — see that
test's docstring and this suite's home issue for the root cause. Do not "fix" it by
loosening the assertion.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.services.redaction import service as redaction_service
from app.services.redaction.config import EffectiveRedactionConfig
from app.services.search.snippet_redaction import MASKABLE_CATEGORIES
from app.services.search.snippet_redaction import SnippetMaskingUnavailableError
from app.services.search.snippet_redaction import mask_snippets

_BASE_CFG = EffectiveRedactionConfig(enabled=True, enabled_categories={"profanity"})


def _cfg(**over) -> EffectiveRedactionConfig:
    return dataclasses.replace(_BASE_CFG, **over)


class TestMaskableCategories:
    def test_toxicity_is_deliberately_absent(self):
        # See the module docstring: toxicity emits a per-segment score, never a span,
        # so there is nothing on this path that could produce a maskable toxicity span.
        assert frozenset({"pii", "profanity", "custom"}) == MASKABLE_CATEGORIES
        assert "toxicity" not in MASKABLE_CATEGORIES


class TestGatingShortCircuits:
    def test_disabled_config_returns_snippets_unchanged(self):
        cfg = _cfg(enabled=False)
        snippets = ["This is a damn shame"]
        assert mask_snippets(snippets, cfg) == snippets

    def test_a_disabled_config_returns_the_same_values_not_merely_equal_strings(self):
        # `list(snippets)` is documented as the short-circuit — pin that it is a new
        # list (not the same list object) but with byte-identical string contents.
        cfg = _cfg(enabled=False)
        snippets = ["unchanged text"]
        out = mask_snippets(snippets, cfg)
        assert out is not snippets
        assert out == snippets

    def test_enabled_but_no_maskable_category_selected_returns_snippets_unchanged(self):
        # toxicity is enabled but not in MASKABLE_CATEGORIES, so cats is empty.
        cfg = _cfg(enabled_categories={"toxicity"})
        snippets = ["This is a damn shame"]
        assert mask_snippets(snippets, cfg) == snippets

    def test_empty_categories_set_returns_snippets_unchanged(self):
        cfg = _cfg(enabled_categories=set())
        snippets = ["This is a damn shame"]
        assert mask_snippets(snippets, cfg) == snippets

    def test_empty_snippet_list_returns_empty_list(self):
        assert mask_snippets([], _cfg()) == []

    def test_an_empty_string_snippet_is_left_empty(self):
        assert mask_snippets([""], _cfg()) == [""]

    def test_a_whitespace_only_snippet_with_nothing_to_mask_is_unchanged(self):
        assert mask_snippets(["   "], _cfg()) == ["   "]


class TestProfanityMasking:
    def test_a_curated_profanity_word_is_replaced_with_its_label(self):
        out = mask_snippets(["This is a damn shame"], _cfg())
        assert out == ["This is a [PROFANITY] shame"]

    def test_a_snippet_with_no_profanity_comes_back_byte_identical(self):
        text = "Nothing objectionable in this sentence at all."
        assert mask_snippets([text], _cfg()) == [text]

    def test_multiple_profane_words_in_one_snippet_are_all_masked(self):
        out = mask_snippets(["damn, this shit is bad"], _cfg())
        assert out == ["[PROFANITY], this [PROFANITY] is bad"]

    def test_matching_is_case_insensitive(self):
        out = mask_snippets(["DAMN it all"], _cfg())
        assert out == ["[PROFANITY] it all"]

    def test_word_boundary_is_respected_no_scunthorpe_problem(self):
        # "damning" contains "damn" as a substring but is not the curated word itself,
        # and the detector is word-boundary (\b) matched.
        text = "a damning indictment"
        assert mask_snippets([text], _cfg()) == [text]

    def test_a_page_of_multiple_snippets_is_masked_positionally_1_to_1(self):
        snippets = ["clean text", "a damn shame", "also clean"]
        out = mask_snippets(snippets, _cfg())
        assert out == ["clean text", "a [PROFANITY] shame", "also clean"]


class TestCustomWordMasking:
    def test_a_configured_custom_word_is_replaced_with_its_label(self):
        cfg = _cfg(enabled_categories={"custom"}, custom_words=["alice"])
        out = mask_snippets(["Alice arrived early"], cfg)
        assert out == ["[CUSTOM] arrived early"]

    def test_no_custom_words_configured_masks_nothing(self):
        cfg = _cfg(enabled_categories={"custom"}, custom_words=[])
        text = "Alice arrived early"
        assert mask_snippets([text], cfg) == [text]

    def test_unicode_custom_word_is_matched_and_masked(self):
        cfg = _cfg(enabled_categories={"custom"}, custom_words=["café"])
        out = mask_snippets(["I love café time"], cfg)
        assert out == ["I love [CUSTOM] time"]

    def test_custom_word_fully_inside_one_mark_run_is_masked(self):
        cfg = _cfg(enabled_categories={"custom"}, custom_words=["alice"])
        out = mask_snippets(["prefix <mark>alice</mark> suffix"], cfg)
        assert out == ["prefix <mark>[CUSTOM]</mark> suffix"]


class TestMarkTagRoundTrip:
    """The ``<mark>`` split-out/restore contract (module docstring point 2)."""

    def test_a_snippet_with_no_mark_tags_masks_normally(self):
        out = mask_snippets(["a damn shame"], _cfg())
        assert out == ["a [PROFANITY] shame"]

    def test_mark_tags_survive_untouched_when_the_covered_word_is_masked(self):
        out = mask_snippets(["a <mark>damn</mark> shame"], _cfg())
        assert out == ["a <mark>[PROFANITY]</mark> shame"]

    def test_mark_tag_attributes_are_preserved(self):
        out = mask_snippets(['a <mark class="hl">damn</mark> shame'], _cfg())
        assert out == ['a <mark class="hl">[PROFANITY]</mark> shame']

    def test_two_separate_highlighted_matches_are_both_masked(self):
        out = mask_snippets(["<mark>damn</mark> and <mark>shit</mark> too"], _cfg())
        assert out == ["<mark>[PROFANITY]</mark> and <mark>[PROFANITY]</mark> too"]

    def test_a_snippet_that_is_only_a_bare_mark_pair_round_trips_unchanged(self):
        assert mask_snippets(["<mark></mark>"], _cfg()) == ["<mark></mark>"]

    def test_unmatched_snippet_with_mark_tags_is_byte_identical(self):
        text = "a <mark>clean</mark> word here"
        assert mask_snippets([text], _cfg()) == [text]


class TestMarkTagBoundary:
    def test_a_profanity_word_split_across_a_mark_tag_boundary_is_still_masked(self):
        # OpenSearch highlighting does not wrap whole words (module docstring point 2:
        # real output includes `<mark>budget ? Not the </mark>original`). Detection runs
        # over the concatenated `doc.plain`, so a curated word split mid-token is still
        # found; the resulting span is clipped and masked on EACH side of the tag
        # separately (documented: "Two adjacent labels look odd; a mask that swallowed
        # the tag between them would emit unbalanced markup").
        out = mask_snippets(["a <mark>da</mark>mn shame"], _cfg())
        assert out == ["a <mark>[PROFANITY]</mark>[PROFANITY] shame"]

    def test_a_custom_word_split_across_a_mark_tag_boundary_is_still_masked(self):
        """Pins CORRECT behavior — currently RED, real bug in snippet_redaction.py.

        Root cause: unlike profanity/pii, custom-word spans are never added to
        ``_Snippet.spans`` via ``_detect()`` (snippet_redaction.py's ``_detect``, ~L163-219,
        only calls ``RedactionService.detect_segment_spans`` with ``run_profanity``/
        ``run_pii`` — there is no third call for ``custom``). Instead, custom-word masking
        happens entirely inside ``_Snippet.render()``'s per-RUN call to
        ``RedactionService.mask_segment`` (snippet_redaction.py:114), which internally
        re-scans with ``wordlist.find_custom_spans(text, cfg.custom_words, ...)`` using
        ONLY that run's text (service.py:483-487). A custom word whose characters are
        split across a ``<mark>`` boundary — the exact scenario this module exists to
        handle (see the module docstring's point 2, and the profanity-side test right
        above this one, which passes) — never appears intact in either individual run, so
        it is never found and leaks into the search-result preview verbatim.

        Proposed fix: in ``_detect()``, when ``"custom" in cats`` and ``cfg.custom_words``,
        also run ``wordlist.find_custom_spans(doc.plain, cfg.custom_words, None,
        cfg.allowlist)`` and extend ``doc.spans`` with the results (mirroring the
        ``run_profanity`` branch), so custom spans get clipped per-run in ``render()``
        exactly like profanity/pii spans already are. The existing per-run rescan inside
        ``mask_segment`` can stay — it is a no-op duplicate for words that stayed inside
        one run, since `_merge_spans` de-overlaps.
        """
        cfg = _cfg(enabled_categories={"custom"}, custom_words=["alice"])
        out = mask_snippets(["prefix <mark>ali</mark>ce suffix"], cfg)
        assert out == ["prefix <mark>[CUSTOM]</mark>[CUSTOM] suffix"]


class TestHtmlEntityRoundTrip:
    def test_an_entity_is_decoded_for_detection_and_the_masked_run_is_reescaped(self):
        cfg = _cfg(enabled_categories={"custom"}, custom_words=["Alice"])
        # `Alice&#x27;s` decodes to "Alice's" for detection; the masked run is
        # re-escaped with html.escape, so the apostrophe comes back as an entity.
        out = mask_snippets(["Alice&#x27;s report"], cfg)
        assert out == ["[CUSTOM]&#x27;s report"]

    def test_an_untouched_run_keeps_its_original_entity_bytes_not_a_reescaped_copy(self):
        # A run with nothing masked is emitted via `self.raw_runs[index]` (the ORIGINAL
        # bytes), not through the html.escape round trip — so an entity encoding choice
        # the indexer made (e.g. `&amp;` instead of the bare character) is preserved
        # exactly rather than normalized.
        cfg = _cfg(enabled_categories={"custom"}, custom_words=["Alice"])
        text = "nothing to see here &amp; done"
        assert mask_snippets([text], cfg) == [text]


class TestUnavailableDetectorFailsClosed:
    def test_a_blocking_detector_failure_raises_and_withholds_the_snippet(self, monkeypatch):
        """Simulates a detector outage the way ``test_chat_output_redaction.py``'s
        ``_StubDetector`` simulates a down Presidio — there is no lighter-weight way to
        exercise this branch without an actually-broken model dependency, and this repo's
        own convention is to stand in for the detector rather than skip the path.
        """

        def _failing_detect(
            text,
            words,
            det_cfg,
            *,
            run_profanity=True,
            run_pii=True,
            run_toxicity=True,
            failures=None,
            unavailable=None,
        ):
            if failures is not None:
                failures.append("profanity")
            return [], None

        monkeypatch.setattr(
            redaction_service.RedactionService,
            "detect_segment_spans",
            staticmethod(_failing_detect),
        )

        with pytest.raises(SnippetMaskingUnavailableError) as excinfo:
            mask_snippets(["This is a damn shame"], _cfg())
        assert "profanity" in str(excinfo.value)

    def test_a_failure_for_a_category_the_user_did_not_enable_does_not_block(self, monkeypatch):
        # blocking_detector_failures is narrow on purpose: a PII outage must not cost a
        # profanity-only user their snippets.
        def _failing_detect(
            text,
            words,
            det_cfg,
            *,
            run_profanity=True,
            run_pii=True,
            run_toxicity=True,
            failures=None,
            unavailable=None,
        ):
            if failures is not None:
                failures.append("pii")
            # Still return the real profanity spans so we can prove masking continued.
            from app.services.redaction.detectors import wordlist

            spans = [s.model_dump() for s in wordlist.find_profanity_spans(text, words)]
            return spans, None

        monkeypatch.setattr(
            redaction_service.RedactionService,
            "detect_segment_spans",
            staticmethod(_failing_detect),
        )

        out = mask_snippets(["This is a damn shame"], _cfg())
        assert out == ["This is a [PROFANITY] shame"]
