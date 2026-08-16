"""Tests for ``app/utils/segment_dedup.py``.

This module is WhisperX's segment cleanup pass: it merges the coarse VAD-chunked
segments a batched transcription run also emits alongside their fine-grained
sentence subsegments, splits multi-sentence segments with NLTK, and clamps
adjacent-segment overlaps. It runs on every completed transcription
(``clean_segments`` is called from the storage pipeline).

Six defects were found and verified against the real function (not inferred
from reading the source) before being fixed. Three are fixed in production
code and asserted here as fixes (1-3 below); three remain open defects, still
pinned as characterization tests describing today's (undesired) behavior
(4-6 below):

1. **FIXED — the docstring lied about the default.** ``deduplicate_segments``'s
   docstring said ``overlap_threshold`` defaults to ``0.8``; the signature
   actually defaults to ``0.6`` (and still does — the default itself was never
   changed, only the documentation). The docstring now says ``0.6``.

2. **FIXED — Phase 1's containment mask was order-dependent.** It used to
   mutate ``keep`` while iterating and re-read it in the same pass
   (``contained_mask`` ANDed against the *current*, partially-mutated ``keep``
   array), so a segment already marked for removal earlier in the loop was
   silently excluded from counting toward a *later* segment's coverage, even
   though it still geometrically overlapped that later segment — making the
   result depend on iteration/processing order. The fix computes
   ``contained_mask`` against ``original_keep``, a fixed snapshot taken before
   Phase 1 starts (all-True, since Phase 1 is the first phase to remove
   anything), while still writing removals into the live ``keep`` array.
   Pinned below with a test that runs the same containment scenario via two
   different input orderings and asserts an identical result.

3. **FIXED — Phase 2 (exact-duplicate) and Phase 3 (similar-text) dedup only
   ever compared ADJACENT pairs in ``kept_indices``.** A duplicate two or more
   positions apart in the kept list was invisible to both phases. Both phases
   now compare each candidate against every other still-kept segment (a full
   pairwise scan over the already-reduced kept set — segment counts here are
   in the hundreds per file at most, bounded by
   ``DEFAULT_RECORDING_MAX_DURATION``, so this is cheap and simpler than a
   windowed heuristic). Pinned below with a non-adjacent exact duplicate
   (Phase 2) and a non-adjacent time-overlapping near-duplicate (Phase 3),
   each now correctly removed/merged.

4. **``_map_words_to_sentence`` gives up at the first non-matching word and
   never resumes** (``sent_lower.find(...)`` returning ``-1`` breaks the loop
   entirely, not just skips one word). A single normalization mismatch
   between a transcription word and NLTK's sentence text truncates every word
   after it — including ones that WOULD have matched — clipping ``sent_end`` to
   an earlier word's timestamp and losing word-level timing on the sentence
   after it too (the frozen ``word_offset`` is handed to the next sentence).
   NOT fixed — out of scope for this change.

5. **``_clamp_overlapping_timestamps`` clamps only ``start``, never ``end``**
   (``segments[i]["start"] = prev_end``), so a segment whose ``end`` sits
   before the clamped ``start`` becomes an invalid negative-duration segment
   with no guard rejecting it. NOT fixed — out of scope for this change.

Following the characterization-test convention of ``tests/unit/test_chunking_service.py``
and ``tests/unit/test_transcription_storage.py`` for the still-open defects (4-5):
each defect test's docstring says this is TODAY's behavior, not desired behavior,
and what should replace it once fixed.
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
# 1. FIXED: docstring now matches the actual overlap_threshold default (0.6)
# ---------------------------------------------------------------------------


def _threshold_fixture() -> list[dict[str, Any]]:
    """Two segments whose coverage lands at exactly 0.7 — between the two candidates.

    A [0, 10] outer segment fully covers the first 7 of its 10 seconds with a
    single inner segment [0, 7]. ``_compute_coverage`` therefore reports
    7 / 10 == 0.7 for the outer segment. 0.7 clears a 0.6 threshold and misses
    a 0.8 one, so this fixture distinguishes the two candidate defaults by
    whether the outer segment gets removed.
    """
    outer = _segment(0.0, 10.0, "A coarse segment that spans the whole range.")
    inner = _segment(0.0, 7.0, "A different, fine-grained segment.")
    return [outer, inner]


def test_explicit_0_8_threshold_keeps_the_070_coverage_segment():
    """A higher, non-default threshold (0.8) does not remove a 0.7-covered segment."""
    result = deduplicate_segments(_threshold_fixture(), overlap_threshold=0.8)

    assert len(result) == 2
    assert result[0]["end"] == 10.0
    assert result[1]["end"] == 7.0


def test_explicit_0_6_threshold_removes_the_070_coverage_segment():
    """The actual signature default (0.6), passed explicitly, removes a 0.7-covered segment."""
    result = deduplicate_segments(_threshold_fixture(), overlap_threshold=0.6)

    assert len(result) == 1
    assert result[0]["end"] == 7.0


def test_calling_with_no_threshold_argument_uses_0_6():
    """Calling with no ``overlap_threshold`` argument reproduces the 0.6 control exactly.

    This is the behavior the docstring must describe: the default removes the
    0.7-covered outer segment, matching ``overlap_threshold=0.6`` explicitly,
    not ``overlap_threshold=0.8``.
    """
    result = deduplicate_segments(_threshold_fixture())

    assert len(result) == 1
    assert result[0]["end"] == 7.0
    assert result[0]["text"] == "A different, fine-grained segment."


def test_docstring_documents_the_real_0_6_default():
    """The docstring text itself must state the real default, not the old 0.8 claim."""
    docstring = deduplicate_segments.__doc__
    assert docstring is not None
    assert "default 0.6" in docstring
    assert "default 0.8" not in docstring


# ---------------------------------------------------------------------------
# 2. FIXED: Phase 1's containment mask no longer depends on processing order
# ---------------------------------------------------------------------------


def _order_dependence_fixture() -> tuple[dict, dict, dict, dict]:
    """Four same/near-start segments that used to expose Phase 1's mutation bug.

    P [0, 15], Q [0, 10], R [0, 8] all start at 0.0 (``np.lexsort`` tie-breaks
    that group by duration, longest first, so P is processed before Q before
    R regardless of input list order). S [0.02, 16.0] starts just after and
    is always processed last.

    * P [0, 15] is evaluated first. Q [0, 10] and R [0, 8] are both
      candidates, and their union covers [0, 10] of P's 15s span:
      coverage = 10/15 = 0.667 >= 0.6, so P is removed.
    * Q [0, 10] is evaluated next. Only R [0, 8] is a candidate;
      coverage = 8/10 = 0.8 >= 0.6, so Q is removed too.
    * R [0, 8] has no smaller candidates and survives.
    * S [0.02, 16.0] is evaluated last. P and Q were already marked removed
      earlier in the loop, but the fix computes ``contained_mask`` against a
      fixed snapshot of the ORIGINAL segment set, so P and Q still count
      toward S's coverage even though they're gone from the live ``keep``
      array. The union of P/Q/R clipped to S's range is dominated by P alone:
      [0.02, 15], giving coverage (15 - 0.02) / (16.0 - 0.02) ~= 0.9374 >= 0.6
      — so S is ALSO removed. (Before the fix, S's ``contained_mask`` only
      saw R, giving coverage (8 - 0.02) / 15.98 ~= 0.4994 < 0.6, and S
      survived — a result that depended on P and Q having already been
      processed and marked removed.)

    Only R survives: it is the one segment nothing else fully contains.
    """
    p = _segment(0.0, 15.0, "ppp text alpha one two three four five.")
    q = _segment(0.0, 10.0, "qqq text bravo six seven eight nine ten.")
    r = _segment(0.0, 8.0, "rrr text charlie eleven twelve thirteen.")
    s = _segment(0.02, 16.0, "sss text delta fourteen fifteen sixteen seventeen.")
    return p, q, r, s


def test_phase1_containment_counts_already_removed_candidates():
    """The fix: candidates removed earlier in the loop still count toward later coverage.

    S used to survive because P and Q had already been marked removed by the
    time S was evaluated. With the fix, S is correctly removed too, because
    coverage is computed against the fixed original snapshot, not the live,
    mutating ``keep`` array. Only R — the segment nothing else fully
    contains — survives.
    """
    p, q, r, s = _order_dependence_fixture()

    result = deduplicate_segments([p, q, r, s])

    assert len(result) == 1
    assert (result[0]["start"], result[0]["end"]) == (0.0, 8.0)
    assert result[0]["text"] == "rrr text charlie eleven twelve thirteen."


def test_phase1_containment_result_independent_of_input_list_order():
    """The result must not depend on the order segments are handed in.

    Runs the same containment scenario through ``deduplicate_segments`` with
    the four segments in two different input orders (as originally
    constructed, and reversed) and asserts the outputs are identical. Proves
    the fix's coverage computation is a pure function of the segment set,
    not of processing/iteration order.
    """
    p, q, r, s = _order_dependence_fixture()

    forward_order = deduplicate_segments([p, q, r, s])
    reverse_order = deduplicate_segments([s, r, q, p])
    shuffled_order = deduplicate_segments([r, s, p, q])

    assert forward_order == reverse_order == shuffled_order
    assert len(forward_order) == 1
    assert forward_order[0]["text"] == "rrr text charlie eleven twelve thirteen."


# ---------------------------------------------------------------------------
# 3a. FIXED: Phase 2 (exact-duplicate) now compares beyond adjacent pairs
# ---------------------------------------------------------------------------


def test_phase2_catches_an_exact_duplicate_two_positions_apart():
    """The fix: an exact text duplicate is caught even with a segment between them.

    Segments at positions 0 and 2 have byte-identical text; position 1 is
    unrelated and does not time-overlap either, so Phase 1 leaves all three
    kept. Phase 2 now compares each kept segment against every other kept
    segment (not just its immediate predecessor), so the (2, 0) pair is
    checked and the duplicate at position 2 is removed. Equal-duration ties
    keep the first occurrence (``dur_i >= dur_j`` removes ``i``).
    """
    seg0 = _segment(0.0, 2.0, "Thank you very much everyone.")
    seg1 = _segment(5.0, 7.0, "Completely different filler content here.")
    seg2 = _segment(10.0, 12.0, "Thank you very much everyone.")

    result = deduplicate_segments([seg0, seg1, seg2])

    assert len(result) == 2
    texts = [seg["text"] for seg in result]
    assert texts.count("Thank you very much everyone.") == 1
    assert "Completely different filler content here." in texts


# ---------------------------------------------------------------------------
# 3b. FIXED: Phase 3 (similar-text) now compares beyond adjacent pairs
# ---------------------------------------------------------------------------


def test_phase3_catches_a_time_overlapping_near_duplicate_two_positions_apart():
    """The fix: a time-overlapping, word-similar near-duplicate is caught the same way.

    Segment 0 ``[0, 10]`` and segment 2 ``[5, 15]`` genuinely overlap in time
    and share 4 of 5 words (Jaccard 0.8, well above the 0.5 gate), which is
    exactly the "He looks jacked" / "He looks jacked, right?" shape Phase 3
    exists to catch. Segment 1 sits between them in ``kept_indices`` (its own
    span ``[2, 50]`` overlaps both neighbours in time but shares no words with
    either, so it triggers no merge on its own). Phase 3 now compares each
    kept segment against every other kept segment, so the (2, 0) pair is
    checked: segment 2 has more text, so segment 0 (the shorter version) is
    removed and segment 2 (the more complete version) survives.
    """
    seg0 = _segment(0.0, 10.0, "he looks jacked right")
    seg_middle = _segment(2.0, 50.0, "zzz filler qqq www content xyz vvv uuu ttt sss")
    seg2 = _segment(5.0, 15.0, "he looks jacked right now")

    words0 = set(seg0["text"].lower().split())
    words2 = set(seg2["text"].lower().split())
    overlap = len(words0 & words2) / len(words0 | words2)
    assert overlap == 0.8, "precondition: the pair really is a near-duplicate"

    result = deduplicate_segments([seg0, seg_middle, seg2])

    assert len(result) == 2
    texts = [seg["text"] for seg in result]
    assert "he looks jacked right" not in texts, "the shorter near-duplicate was removed"
    assert "he looks jacked right now" in texts, "the more complete version survived"
    assert seg_middle["text"] in texts


# ---------------------------------------------------------------------------
# 4. OPEN DEFECT: _map_words_to_sentence: one mismatched word truncates everything after it
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
# 5. OPEN DEFECT: _clamp_overlapping_timestamps clamps start but never end
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
