"""Characterization tests for ``app/utils/segment_dedup.py``.

This module is WhisperX's segment cleanup pass: it merges the coarse VAD-chunked
segments a batched transcription run also emits alongside their fine-grained
sentence subsegments, splits multi-sentence segments with NLTK, and clamps
adjacent-segment overlaps. It runs on every completed transcription
(``clean_segments`` is called from the storage pipeline) and had no tests at all.

Five defects are pinned here, each verified against the real function (not
inferred from reading the source) before being written down:

1. **The docstring lies about the default.** ``deduplicate_segments``'s docstring
   (L41-42) says ``overlap_threshold`` defaults to ``0.8``; the signature (L23)
   actually defaults to ``0.6``. A fixture whose coverage lands at exactly 0.7 —
   strictly between the two candidate defaults — decides which one is real: with
   no threshold argument, the segment is removed, which only happens at 0.6.
   Passing ``overlap_threshold=0.8`` explicitly is the control that proves the
   same fixture would NOT be removed under the documented default, so the two
   control tests plus the default-argument test triangulate the answer rather
   than asserting it from one data point.

2. **Phase 1's containment mask is order-dependent because it mutates ``keep``
   while iterating and re-reads it in the same pass** (L94: ``contained_mask``
   ANDs against the *current*, partially-mutated ``keep`` array). A segment
   already marked for removal earlier in the loop is silently excluded from
   counting toward a *later* segment's coverage, even though it still
   geometrically overlaps that later segment. Pinned with a 4-segment fixture,
   plus the hand-computed counterfactual coverage (in the test's own comments)
   showing the removed segments WOULD have pushed a later segment over the
   0.6 threshold too, had they still counted.

3 & 4. **Phase 2 (exact-duplicate) and Phase 3 (similar-text) dedup only ever
   compare ADJACENT pairs in ``kept_indices``** (L125-136, L142-168). A
   duplicate two or more positions apart in the kept list is invisible to both
   phases. Pinned with a non-adjacent exact duplicate (Phase 2) and a
   non-adjacent time-overlapping near-duplicate (Phase 3), each surviving
   deduplication when today's code runs.

5. **``_map_words_to_sentence`` gives up at the first non-matching word and
   never resumes** (L266-269: ``sent_lower.find(...)`` returning ``-1`` breaks
   the loop entirely, not just skips one word). A single normalization mismatch
   between a transcription word and NLTK's sentence text truncates every word
   after it — including ones that WOULD have matched — clipping ``sent_end`` to
   an earlier word's timestamp and losing word-level timing on the sentence
   after it too (the frozen ``word_offset`` is handed to the next sentence).

6. **``_clamp_overlapping_timestamps`` clamps only ``start``, never ``end``**
   (L410-411: ``segments[i]["start"] = prev_end``), so a segment whose
   ``end`` sits before the clamped ``start`` becomes an invalid negative-duration
   segment with no guard rejecting it.

Following the characterization-test convention of ``tests/unit/test_chunking_service.py``
and ``tests/unit/test_transcription_storage.py``: each defect test's docstring says
this is TODAY's behavior, not desired behavior, and what should replace it once fixed.
"""

from __future__ import annotations

from typing import Any

from app.utils.segment_dedup import _clamp_overlapping_timestamps
from app.utils.segment_dedup import _map_words_to_sentence
from app.utils.segment_dedup import deduplicate_segments
from app.utils.segment_dedup import split_sentences_nltk


def _segment(start: float, end: float, text: str, **extra: Any) -> dict[str, Any]:
    seg: dict[str, Any] = {"start": start, "end": end, "text": text}
    seg.update(extra)
    return seg


# ---------------------------------------------------------------------------
# 1. Docstring/signature mismatch on overlap_threshold (0.8 documented, 0.6 real)
# ---------------------------------------------------------------------------


def _threshold_fixture() -> list[dict[str, Any]]:
    """Two segments whose coverage lands at exactly 0.7 — between the two candidates.

    A [0, 10] outer segment fully covers the first 7 of its 10 seconds with a
    single inner segment [0, 7]. ``_compute_coverage`` therefore reports
    7 / 10 == 0.7 for the outer segment. 0.7 clears a 0.6 threshold and misses
    a 0.8 one, so which candidate default is real is decided entirely by
    whether the outer segment gets removed.
    """
    outer = _segment(0.0, 10.0, "A coarse segment that spans the whole range.")
    inner = _segment(0.0, 7.0, "A different, fine-grained segment.")
    return [outer, inner]


def test_explicit_0_8_threshold_keeps_the_070_coverage_segment():
    """Control: the DOCUMENTED default (0.8) does not remove a 0.7-covered segment."""
    result = deduplicate_segments(_threshold_fixture(), overlap_threshold=0.8)

    assert len(result) == 2
    assert result[0]["end"] == 10.0
    assert result[1]["end"] == 7.0


def test_explicit_0_6_threshold_removes_the_070_coverage_segment():
    """Control: the ACTUAL signature default (0.6) does remove a 0.7-covered segment."""
    result = deduplicate_segments(_threshold_fixture(), overlap_threshold=0.6)

    assert len(result) == 1
    assert result[0]["end"] == 7.0


def test_calling_with_no_threshold_argument_behaves_like_0_6_not_the_documented_0_8():
    """The real defect: the docstring's ``(default 0.8)`` claim is false.

    Calling with no ``overlap_threshold`` argument at all reproduces the
    ``overlap_threshold=0.6`` control exactly (the 10-second outer segment is
    removed), not the ``overlap_threshold=0.8`` control (which keeps both).
    If the docstring were correct this test would fail and
    ``test_explicit_0_8_threshold_keeps_the_070_coverage_segment`` would be the
    one matching default behavior instead.
    """
    result = deduplicate_segments(_threshold_fixture())

    assert len(result) == 1
    assert result[0]["end"] == 7.0
    assert result[0]["text"] == "A different, fine-grained segment."


# ---------------------------------------------------------------------------
# 2. Phase 1: mutating `keep` mid-loop makes containment order-dependent
# ---------------------------------------------------------------------------


def test_phase1_containment_is_order_dependent_across_mutation():
    """Regression pin for the CURRENT (fragile) Phase 1 outcome — not a correctness claim.

    Four same/near-start segments, forcing ``np.lexsort`` to tie-break by
    duration on the ``[0.0, 15.0]``/``[0.0, 10.0]``/``[0.0, 8.0]`` group:

    * P [0, 15] is evaluated first (longest at start=0). Q [0, 10] and R [0, 8]
      are both still ``keep=True``, and their union covers [0, 10] of P's 15s
      span: coverage = 10/15 = 0.667 >= 0.6, so P is removed.
    * Q [0, 10] is evaluated next. Only R [0, 8] is still a candidate;
      coverage = 8/10 = 0.8 >= 0.6, so Q is removed too.
    * R [0, 8] has no smaller candidates and survives.
    * S [0.02, 16.0] is evaluated last. Its only ``keep=True`` candidate is R
      (P and Q were already marked False and are excluded from S's
      ``contained_mask`` by the ``& keep`` term at L94, even though P and Q
      both still geometrically overlap S's range). Coverage from R alone is
      (8 - 0.02) / (16.0 - 0.02) = 7.98 / 15.98 ~= 0.4994 < 0.6, so S survives.

    Had P and Q still counted (the counterfactual, if ``contained_mask`` were
    computed against the ORIGINAL membership instead of the live, mutated
    ``keep`` array), the union of P/Q/R clipped to S's range is dominated by P
    alone: [0.02, 15], giving coverage (15 - 0.02) / 15.98 ~= 0.9374 >= 0.6 —
    S would ALSO have been removed. That flip (0.4994 vs 0.9374, either side
    of the 0.6 threshold) is the concrete effect of reading a mutated ``keep``
    mid-loop. Today's code keeps S; a version that recomputed contained_mask
    from the full original set would not.
    """
    p = _segment(0.0, 15.0, "ppp text alpha one two three four five.")
    q = _segment(0.0, 10.0, "qqq text bravo six seven eight nine ten.")
    r = _segment(0.0, 8.0, "rrr text charlie eleven twelve thirteen.")
    s = _segment(0.02, 16.0, "sss text delta fourteen fifteen sixteen seventeen.")

    result = deduplicate_segments([p, q, r, s])

    assert len(result) == 2
    assert (result[0]["start"], result[0]["end"]) == (0.0, 8.0)
    assert result[0]["text"] == "rrr text charlie eleven twelve thirteen."
    assert (result[1]["start"], result[1]["end"]) == (0.02, 16.0)
    assert result[1]["text"] == "sss text delta fourteen fifteen sixteen seventeen."


# ---------------------------------------------------------------------------
# 3. Phase 2: exact-duplicate dedup only checks ADJACENT kept_indices pairs
# ---------------------------------------------------------------------------


def test_phase2_misses_an_exact_duplicate_two_positions_apart():
    """TODAY's gap: an exact text duplicate survives if a different segment sits between.

    Segments at positions 0 and 2 have byte-identical text; position 1 is
    unrelated and does not time-overlap either, so Phase 1 leaves all three
    kept and Phase 2 only ever compares (1, 0) and (2, 1) — never (2, 0). The
    duplicate at position 2 is never caught. Once Phase 2 compares beyond
    adjacent pairs, this test's expectation of 3 survivors must become 2.
    """
    seg0 = _segment(0.0, 2.0, "Thank you very much everyone.")
    seg1 = _segment(5.0, 7.0, "Completely different filler content here.")
    seg2 = _segment(10.0, 12.0, "Thank you very much everyone.")

    result = deduplicate_segments([seg0, seg1, seg2])

    assert len(result) == 3
    assert result[0]["text"] == "Thank you very much everyone."
    assert result[2]["text"] == "Thank you very much everyone."
    assert result[0]["text"] == result[2]["text"], "the un-caught duplicate pair"


# ---------------------------------------------------------------------------
# 4. Phase 3: similar-text dedup only checks ADJACENT kept_indices pairs
# ---------------------------------------------------------------------------


def test_phase3_misses_a_time_overlapping_near_duplicate_two_positions_apart():
    """TODAY's gap: a time-overlapping, word-similar near-duplicate is missed the same way.

    Segment 0 ``[0, 10]`` and segment 2 ``[5, 15]`` genuinely overlap in time
    and share 4 of 5 words (Jaccard 0.8, well above the 0.5 gate), which is
    exactly the "He looks jacked" / "He looks jacked, right?" shape Phase 3
    exists to catch. Segment 1 sits between them in ``kept_indices`` (its own
    span ``[2, 50]`` overlaps both neighbours in time but shares no words with
    either, so it triggers no merge on its own) and blocks the (2, 0)
    comparison Phase 3 never makes. All three survive.
    """
    seg0 = _segment(0.0, 10.0, "he looks jacked right")
    seg_middle = _segment(2.0, 50.0, "zzz filler qqq www content xyz vvv uuu ttt sss")
    seg2 = _segment(5.0, 15.0, "he looks jacked right now")

    result = deduplicate_segments([seg0, seg_middle, seg2])

    assert len(result) == 3
    assert result[0]["text"] == "he looks jacked right"
    assert result[2]["text"] == "he looks jacked right now"
    words0 = set(result[0]["text"].lower().split())
    words2 = set(result[2]["text"].lower().split())
    overlap = len(words0 & words2) / len(words0 | words2)
    assert overlap == 0.8, "precondition: the missed pair really is a near-duplicate"


# ---------------------------------------------------------------------------
# 5. _map_words_to_sentence: one mismatched word truncates everything after it
# ---------------------------------------------------------------------------


def test_map_words_to_sentence_stops_at_the_first_unmatched_word():
    """Direct pin on ``_map_words_to_sentence``: a mid-list mismatch truncates the match.

    ``sentence_text`` contains "bar" and would match it, but the word list has
    a "mismatch" token in between that literally does not appear anywhere in
    the sentence text. ``sent_lower.find("mismatch", pos)`` returns -1, and the
    function ``break``s instead of skipping just that one word — so "foo" and
    "bar", which DO appear later in the sentence, are never consumed.
    """
    sentence_text = "Hello world foo bar."
    words = [
        {"word": "Hello", "start": 0.0, "end": 0.5},
        {"word": "world", "start": 0.5, "end": 1.0},
        {"word": "mismatch", "start": 1.0, "end": 1.5},
        {"word": "foo", "start": 1.5, "end": 2.0},
        {"word": "bar", "start": 2.0, "end": 2.5},
    ]

    matched, next_offset = _map_words_to_sentence(sentence_text, words, 0)

    assert [w["word"] for w in matched] == ["Hello", "world"]
    assert next_offset == 2, "stopped at the mismatched word, not consumed past it"
    assert matched[-1]["end"] == 1.0, "the timestamp a truncated sent_end would inherit"


def test_split_sentences_nltk_truncates_sent_end_and_drops_the_next_sentence_timing():
    """End-to-end pin: the truncation reaches ``split_sentences_nltk``'s output.

    The segment's real word timestamps run through 102.5 ("bar." ends there),
    but the mismatched "mismatch" token (never present in the sentence text)
    truncates the first sentence's word match at "world", so ``sent_end`` is
    pinned to 101.0 instead of 102.5. The frozen ``word_offset`` (2, still
    pointing at "mismatch") is then handed to the second sentence, whose
    ``_map_words_to_sentence`` call breaks on its very first word — so the
    second sentence gets NO matched words and falls back to character-position
    interpolation, discarding real word timestamps it should have used.
    """
    seg = _segment(
        100.0,
        110.0,
        "Hello world foo bar. Another sentence follows nicely today.",
        words=[
            {"word": "Hello", "start": 100.0, "end": 100.5},
            {"word": "world", "start": 100.5, "end": 101.0},
            {"word": "mismatch", "start": 101.0, "end": 101.5},
            {"word": "foo", "start": 101.5, "end": 102.0},
            {"word": "bar", "start": 102.0, "end": 102.5},
            {"word": "Another", "start": 103.0, "end": 103.5},
            {"word": "sentence", "start": 103.5, "end": 104.0},
            {"word": "follows", "start": 104.0, "end": 104.5},
            {"word": "nicely", "start": 104.5, "end": 105.0},
            {"word": "today", "start": 105.0, "end": 105.5},
        ],
    )

    result = split_sentences_nltk([seg])

    assert len(result) == 2
    assert result[0]["text"] == "Hello world foo bar."
    assert result[0]["end"] == 101.0, "truncated at 'world', not the real 102.5 for 'bar.'"
    assert result[1]["text"] == "Another sentence follows nicely today."
    assert result[1]["words"] == [], "word timing lost entirely by the frozen offset"


# ---------------------------------------------------------------------------
# 6. _clamp_overlapping_timestamps clamps start but never end
# ---------------------------------------------------------------------------


def test_clamp_overlapping_timestamps_can_produce_an_invalid_start_after_end():
    """TODAY's gap: clamping ``start`` forward past a short segment's own ``end``.

    Segment 1 spans ``[2.0, 5.0]`` and overlaps segment 0's ``[0.0, 10.0]``, so
    its ``start`` is clamped to segment 0's ``end`` (10.0) per L411. Nothing
    clamps or validates ``end`` against the new ``start``, so segment 1 comes
    back as ``start=10.0, end=5.0`` — a negative-duration segment — with no
    guard rejecting it. Its first word is clamped the same way, producing an
    equally invalid word span. If a guard is added, this must fail and be
    replaced with an assertion that end is also advanced (or the segment
    dropped).
    """
    segments = [
        _segment(0.0, 10.0, "first"),
        _segment(2.0, 5.0, "second", words=[{"word": "x", "start": 2.0, "end": 2.5}]),
    ]

    clamped = _clamp_overlapping_timestamps(segments)

    assert clamped == 1
    assert segments[1]["start"] == 10.0
    assert segments[1]["end"] == 5.0
    assert segments[1]["start"] > segments[1]["end"], "invalid negative-duration segment"
    assert segments[1]["words"][0]["start"] == 10.0
    assert segments[1]["words"][0]["end"] == 2.5
    assert segments[1]["words"][0]["start"] > segments[1]["words"][0]["end"]
