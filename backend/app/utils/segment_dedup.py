"""
Vectorized segment deduplication for transcription output.

Whisper's batched pipeline can return both coarse VAD-chunked segments AND
their fine-grained sentence subsegments, creating overlapping/duplicate
entries. This module merges them using numpy interval operations.

The dominant pattern is "full containment": a long segment (e.g., 0.0-40.0s)
followed by shorter subsegments of the same content (0.0-13.0s, 14.0-25.0s, etc).
The fix: detect containment groups, keep the fine-grained segments, discard coarse.
"""

import logging
import time

import numpy as np

logger = logging.getLogger(__name__)

#: Maximum silent gap (seconds) between two identically-worded segments for them to be
#: considered the *same* utterance emitted twice, rather than the speaker genuinely saying
#: the same short phrase again later ("Yeah.", "Right.", "Okay.").
#:
#: Phase 2 compares every kept segment against every other one, so without a time bound it
#: collapses repeated utterances anywhere in the recording — real speech, silently deleted
#: (issue #487: 31 words lost from the 10-minute boundary fixture, including three "Yeah."s
#: up to 149 s apart). Phase 3's "must actually overlap" rule is too strict to reuse here:
#: genuine Whisper re-emissions in that fixture are adjacent, not overlapping, so requiring
#: overlap disables Phase 2 entirely.
#:
#: Measured against ``tests/fixtures/boundary/karpathy_10m``: the correct 110 segments /
#: 2282 words is recovered for any value in [0.5, 2.0]; 0.0 removes nothing and >=5.0 starts
#: over-removing again. 2.0 is the widest value in that plateau.
_EXACT_DUPLICATE_MAX_GAP_S = 2.0


def deduplicate_segments(
    segments: list[dict],
    overlap_threshold: float = 0.6,
) -> list[dict]:
    """
    Remove duplicate/overlapping segments from WhisperX output.

    Uses vectorized numpy operations for O(n log n) performance.

    Strategy:
    1. Sort segments by start time, then by duration (shortest first)
    2. Build a containment graph: segment A "contains" segment B if B's time
       range is mostly within A's range (>80% overlap by duration)
    3. For each containment group, keep the fine-grained (shorter) segments
       and discard the coarse (longer) parent segment
    4. Merge any remaining partial overlaps by concatenating text

    Args:
        segments: List of segment dicts with 'start', 'end', 'text' keys.
                  May also have 'speaker', 'speaker_id', 'confidence', etc.
        overlap_threshold: Fraction of a segment's duration that must overlap
                          with another to be considered contained (default 0.6)

    Returns:
        Deduplicated list of segments, sorted by start time.
    """
    if not segments or len(segments) <= 1:
        return segments

    start_time = time.perf_counter()
    n = len(segments)

    # Convert to numpy arrays for vectorized operations
    starts = np.array([s["start"] for s in segments], dtype=np.float64)
    ends = np.array([s["end"] for s in segments], dtype=np.float64)
    durations = ends - starts

    # Sort by start time, then by duration descending (longest first within same start)
    # This groups coarse segments before their fine-grained children
    sort_idx = np.lexsort((-durations, starts))
    starts = starts[sort_idx]
    ends = ends[sort_idx]
    durations = durations[sort_idx]
    sorted_segments = [segments[i] for i in sort_idx]

    # Mark segments to keep (start with all True)
    keep = np.ones(n, dtype=bool)

    _remove_contained_segments(starts, ends, durations, overlap_threshold, keep)
    _remove_exact_duplicates(sorted_segments, starts, ends, durations, keep)
    _remove_similar_overlapping_segments(sorted_segments, starts, ends, keep)

    result = [sorted_segments[i] for i in range(n) if keep[i]]

    # Re-sort by start time
    result.sort(key=lambda s: s["start"])

    elapsed = time.perf_counter() - start_time
    removed = n - len(result)
    logger.info(
        f"TIMING: segment_dedup completed in {elapsed:.3f}s - "
        f"removed {removed}/{n} segments ({removed / n * 100:.1f}%), "
        f"kept {len(result)} segments"
    )

    return result


def _remove_contained_segments(
    starts: np.ndarray,
    ends: np.ndarray,
    durations: np.ndarray,
    overlap_threshold: float,
    keep: np.ndarray,
) -> None:
    """Phase 1: remove coarse segments that are fully covered by finer ones.

    A "coarse" segment contains multiple "fine" segments within its time
    range. For each segment, checks whether shorter segments collectively
    cover its time range; if so, marks the coarse one for removal.

    Coverage is computed against a fixed snapshot of the original segment
    membership (all-True, since this is the first phase to remove anything),
    not the live ``keep`` array. Without this, a candidate already marked for
    removal earlier in the loop would be silently excluded from counting
    toward a *later* segment's coverage, even though it still geometrically
    overlaps that later segment — making the result depend on iteration
    order.

    Args:
        starts: Segment start times, sorted by start then duration descending.
        ends: Segment end times, in the same order as ``starts``.
        durations: Segment durations (``ends - starts``), same order.
        overlap_threshold: Minimum coverage fraction to remove a segment.
        keep: Boolean keep/remove mask, modified in place.
    """
    n = len(starts)
    original_keep = keep.copy()

    for i in range(n):
        if not keep[i]:
            continue

        seg_start = starts[i]
        seg_end = ends[i]
        seg_dur = durations[i]

        # Skip very short segments (< 0.5s) - these are fine-grained
        if seg_dur < 0.5:
            continue

        # Find segments that are contained within this segment's time range
        contained_mask = (
            (starts >= seg_start - 0.05)
            & (ends <= seg_end + 0.05)
            & (durations < seg_dur * 0.95)  # Must be meaningfully shorter
            & original_keep  # Original membership, not the live-mutating array
            & (np.arange(n) != i)  # Not self
        )

        contained_indices = np.where(contained_mask)[0]

        if len(contained_indices) == 0:
            continue

        # Check if the contained segments collectively cover this segment
        # by computing the union of their time ranges
        c_starts = starts[contained_indices]
        c_ends = ends[contained_indices]

        # Sort contained segments by start time
        c_sort = np.argsort(c_starts)
        c_starts = c_starts[c_sort]
        c_ends = c_ends[c_sort]

        # Compute union coverage using merge-intervals approach
        coverage = _compute_coverage(c_starts, c_ends, seg_start, seg_end)

        if coverage >= overlap_threshold:
            # The fine-grained segments cover most of this coarse segment
            keep[i] = False


def _remove_exact_duplicates(
    sorted_segments: list[dict],
    starts: np.ndarray,
    ends: np.ndarray,
    durations: np.ndarray,
    keep: np.ndarray,
) -> None:
    """Phase 2: remove exact text duplicates among kept segments.

    Checks each kept segment against every OTHER still-kept segment for
    identical text (from Whisper's non-deterministic output) — not just the
    segment immediately before it, or a duplicate separated by an unrelated
    segment would never be caught. Segment counts here are in the hundreds
    per file at most (bounded by ``DEFAULT_RECORDING_MAX_DURATION``), so a
    full pairwise scan over the already-reduced kept set is cheap and
    simpler than a windowed heuristic.

    Pairs separated by more than ``_EXACT_DUPLICATE_MAX_GAP_S`` of silence are
    left alone. Comparing against only the immediate predecessor used to bound
    this implicitly; the full pairwise scan needs the bound stated explicitly,
    or repeated short utterances get collapsed across the whole recording and
    real speech disappears (issue #487).

    Args:
        sorted_segments: Segment dicts, sorted by start then duration descending.
        starts: Segment start times, same order as ``sorted_segments``.
        ends: Segment end times, same order as ``sorted_segments``.
        durations: Segment durations, same order as ``sorted_segments``.
        keep: Boolean keep/remove mask, modified in place.
    """
    kept_indices = np.where(keep)[0]
    for idx in range(1, len(kept_indices)):
        i = kept_indices[idx]
        if not keep[i]:
            continue
        text_i = sorted_segments[i]["text"].strip()

        for prev_idx in range(idx):
            j = kept_indices[prev_idx]
            if not keep[j]:
                continue
            if text_i != sorted_segments[j]["text"].strip():
                continue

            # Negative when the two segments overlap in time; positive is the silent
            # gap between them. Same utterance twice, or a genuine repetition later?
            gap = max(starts[i], starts[j]) - min(ends[i], ends[j])
            if gap > _EXACT_DUPLICATE_MAX_GAP_S:
                continue

            # Keep the one with better timing (more precise start/end)
            dur_i = durations[i]
            dur_j = durations[j]
            # Keep shorter duration (more precise) or first occurrence
            if dur_i >= dur_j:
                keep[i] = False
                break  # i is gone; no need to compare it further
            else:
                keep[j] = False


def _remove_similar_overlapping_segments(
    sorted_segments: list[dict],
    starts: np.ndarray,
    ends: np.ndarray,
    keep: np.ndarray,
) -> None:
    """Phase 3: remove time-overlapping segments with similar text.

    Handles cases like "He looks jacked" [20.7-22.1] vs "He looks jacked,
    right?" [21.5-22.5] where text substantially overlaps. As in Phase 2,
    compares each kept segment against every OTHER still-kept segment (not
    just its immediate predecessor), gated by a real time-overlap check.

    Args:
        sorted_segments: Segment dicts, sorted by start then duration descending.
        starts: Segment start times, same order as ``sorted_segments``.
        ends: Segment end times, same order as ``sorted_segments``.
        keep: Boolean keep/remove mask, modified in place.
    """
    kept_indices = np.where(keep)[0]
    for idx in range(1, len(kept_indices)):
        i = kept_indices[idx]
        if not keep[i]:
            continue

        words_i: set[str] | None = None

        for prev_idx in range(idx):
            j = kept_indices[prev_idx]
            if not keep[j]:
                continue

            # Check for time overlap
            time_overlap = min(ends[j], ends[i]) - max(starts[j], starts[i])
            if time_overlap <= 0:
                continue

            # Check text similarity (word overlap ratio)
            words_j = set(sorted_segments[j]["text"].lower().split())
            if words_i is None:
                words_i = set(sorted_segments[i]["text"].lower().split())
            if not words_j or not words_i:
                continue

            word_overlap = len(words_j & words_i) / max(len(words_j | words_i), 1)

            if word_overlap >= 0.5:
                # Substantial text overlap + time overlap = duplicate
                # Keep the segment with more text (more complete version)
                if len(sorted_segments[i]["text"]) >= len(sorted_segments[j]["text"]):
                    keep[j] = False
                else:
                    keep[i] = False
                    break  # i is gone; no need to compare it further


def _compute_coverage(
    sorted_starts: np.ndarray,
    sorted_ends: np.ndarray,
    target_start: float,
    target_end: float,
) -> float:
    """
    Compute what fraction of [target_start, target_end] is covered by
    the union of intervals defined by sorted_starts and sorted_ends.

    Uses a merge-intervals approach for O(n) performance.

    Args:
        sorted_starts: Start times, sorted ascending
        sorted_ends: End times, corresponding to sorted_starts
        target_start: Start of the range to check coverage for
        target_end: End of the range to check coverage for

    Returns:
        Fraction of target range covered (0.0 to 1.0)
    """
    target_dur = target_end - target_start
    if target_dur <= 0:
        return 1.0

    # Clip intervals to target range
    clipped_starts = np.maximum(sorted_starts, target_start)
    clipped_ends = np.minimum(sorted_ends, target_end)

    # Remove intervals that don't intersect target
    valid = clipped_starts < clipped_ends
    if not np.any(valid):
        return 0.0

    cs = clipped_starts[valid]
    ce = clipped_ends[valid]

    # Merge overlapping intervals
    covered = 0.0
    merge_start = cs[0]
    merge_end = ce[0]

    for i in range(1, len(cs)):
        if cs[i] <= merge_end:
            # Extend current merged interval
            merge_end = max(merge_end, ce[i])
        else:
            # Gap found - add current merged interval
            covered += merge_end - merge_start
            merge_start = cs[i]
            merge_end = ce[i]

    # Add last merged interval
    covered += merge_end - merge_start

    return covered / target_dur  # type: ignore[no-any-return]


def _map_words_to_sentence(
    sentence_text: str,
    words: list[dict],
    word_offset: int,
) -> tuple[list[dict], int]:
    """Map words to a sentence by matching word text to sentence text.

    Walks through words starting at word_offset, consuming words whose
    stripped text appears in the sentence. Returns the matched words
    and the new offset.
    """
    matched = []
    sent_lower = sentence_text.lower()
    pos = 0
    idx = word_offset

    while idx < len(words):
        word_text = words[idx].get("word", "").strip().lower()
        if not word_text:
            idx += 1
            continue

        found = sent_lower.find(word_text, pos)
        if found == -1:
            # Word doesn't belong to this sentence
            break

        matched.append(words[idx])
        pos = found + len(word_text)
        idx += 1

    return matched, idx


def split_sentences_nltk(segments: list[dict]) -> list[dict]:
    """Split multi-sentence segments into individual sentences using NLTK punkt.

    When word-level timestamps are available (from faster-whisper native
    word_timestamps from faster-whisper), uses them for precise sentence
    boundaries. Falls back to character-position interpolation when words
    are not available.

    Args:
        segments: List of segment dicts with 'start', 'end', 'text' keys.
            Optionally 'words' with word-level timestamps.

    Returns:
        List of segments with multi-sentence segments split into individual ones.
    """
    import nltk
    from nltk.data import load as nltk_load

    start_time = time.perf_counter()

    try:
        sentence_splitter = nltk_load("tokenizers/punkt/english.pickle")
    except LookupError:
        nltk.download("punkt_tab", quiet=True)
        sentence_splitter = nltk_load("tokenizers/punkt/english.pickle")

    result = []
    split_count = 0

    for seg in segments:
        text = seg.get("text", "").strip()
        if not text:
            continue

        seg_start = seg["start"]
        seg_end = seg["end"]
        seg_dur = seg_end - seg_start
        words = seg.get("words", [])

        # Get sentence spans from NLTK
        sentence_spans = list(sentence_splitter.span_tokenize(text))

        if len(sentence_spans) <= 1:
            result.append(seg)
            continue

        split_count += 1
        has_word_timestamps = bool(words and "start" in words[0] and "end" in words[0])

        if has_word_timestamps:
            # Use word timestamps for precise sentence boundaries
            word_offset = 0
            for span_start, span_end in sentence_spans:
                sentence_text = text[span_start:span_end].strip()
                if not sentence_text:
                    continue

                sent_words, word_offset = _map_words_to_sentence(sentence_text, words, word_offset)

                if sent_words:
                    sent_start = sent_words[0]["start"]
                    sent_end = sent_words[-1].get("end", sent_words[-1]["start"])
                else:
                    # Fallback: interpolate if no words matched
                    total_chars = len(text)
                    sent_start = round(seg_start + (span_start / total_chars) * seg_dur, 3)
                    sent_end = round(seg_start + (span_end / total_chars) * seg_dur, 3)

                new_seg = {
                    "text": sentence_text,
                    "start": float(sent_start),
                    "end": float(sent_end),
                    "words": sent_words,
                }
                # Preserve other metadata (but NOT parent's words or speaker)
                for key in seg:
                    if key not in ("text", "start", "end", "words", "speaker"):
                        new_seg[key] = seg[key]
                result.append(new_seg)
        else:
            # No word timestamps — interpolate by character position
            total_chars = len(text)
            for span_start, span_end in sentence_spans:
                sentence_text = text[span_start:span_end].strip()
                if not sentence_text:
                    continue

                frac_start = span_start / total_chars
                frac_end = span_end / total_chars
                sent_start = round(seg_start + frac_start * seg_dur, 3)
                sent_end = round(seg_start + frac_end * seg_dur, 3)

                new_seg = dict(seg)
                new_seg["text"] = sentence_text
                new_seg["start"] = sent_start
                new_seg["end"] = sent_end
                # Remove words since we can't accurately slice them
                new_seg.pop("words", None)
                result.append(new_seg)

    elapsed = time.perf_counter() - start_time
    logger.info(
        f"TIMING: split_sentences_nltk completed in {elapsed:.3f}s - "
        f"split {split_count} multi-sentence segments, "
        f"{len(segments)} -> {len(result)} segments"
    )

    return result


def _clamp_overlapping_timestamps(segments: list[dict]) -> int:
    """Clamp adjacent segment timestamps to remove small overlaps.

    After sentence splitting, adjacent segments can overlap by 50-220ms
    because word boundaries from Whisper aren't perfectly sequential.
    This sets each segment's start to max(start, prev_end) so segments
    tile without gaps or overlaps.

    Also clamps the first word's start time in the segment if it falls
    before the clamped segment start.

    Args:
        segments: Sorted list of segment dicts (modified in place).

    Returns:
        Number of overlaps clamped.
    """
    clamped = 0
    for i in range(1, len(segments)):
        prev_end = segments[i - 1]["end"]
        curr_start = segments[i]["start"]

        if curr_start < prev_end:
            segments[i]["start"] = prev_end
            clamped += 1

            # Also clamp the first word if it starts before the new segment start
            words = segments[i].get("words")
            if words and words[0].get("start", prev_end) < prev_end:
                words[0]["start"] = prev_end

    return clamped


def clean_segments(
    segments: list[dict],
    enable_sentence_splitting: bool = True,
    enable_dedup: bool = True,
) -> list[dict]:
    """
    Full segment cleaning pipeline: sentence split, dedup, clamp overlaps.

    This replaces the segment merging that WhisperX's align() function
    previously performed, using native word timestamps from faster-whisper.

    Args:
        segments: Raw segments from WhisperX transcription
        enable_sentence_splitting: Split multi-sentence segments using NLTK
        enable_dedup: Remove overlapping/duplicate segments

    Returns:
        Cleaned segments ready for speaker assignment and storage
    """
    start_time = time.perf_counter()
    original_count = len(segments)

    result = segments

    if enable_sentence_splitting:
        result = split_sentences_nltk(result)

    if enable_dedup:
        result = deduplicate_segments(result)

    # Final pass: clamp any remaining timestamp overlaps between adjacent segments
    clamped = _clamp_overlapping_timestamps(result)
    if clamped:
        logger.info(f"Clamped {clamped} overlapping segment timestamps")

    elapsed = time.perf_counter() - start_time
    logger.info(
        f"TIMING: clean_segments completed in {elapsed:.3f}s - "
        f"{original_count} -> {len(result)} segments"
    )

    return result
