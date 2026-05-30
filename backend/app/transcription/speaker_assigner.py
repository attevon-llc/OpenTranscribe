"""Vectorized speaker assignment using numpy matrix operations.

Replaces WhisperX's per-word interval tree loop with a fully vectorized
approach using numpy broadcasting and matrix multiply. Achieves ~13x speedup
for long-form content (80s → 6s for a 4.7hr file with 54K words).

Algorithm:
  1. Extract all word timestamps into numpy arrays
  2. Compute (words × diarization) overlap matrix via broadcasting
  3. Accumulate per-speaker overlap via matrix multiply with speaker indicator
  4. Assign each word/segment to the speaker with maximum overlap
"""

import logging
import time

import numpy as np

from app.transcription.diarize_result import DiarizeResult

logger = logging.getLogger(__name__)

# Process words in chunks to bound memory: (CHUNK × n_diarize) float32 matrix
_CHUNK_SIZE = 5000


def assign_speakers(
    diarize_df: DiarizeResult,
    transcript_result: dict,
) -> dict:
    """Assign speaker labels to transcript segments and words.

    Vectorized numpy implementation: extracts all word/segment timestamps,
    computes overlap with diarization intervals via matrix operations, and
    assigns the dominant speaker per word and segment.

    Args:
        diarize_df: DiarizeResult with start/end/speaker numpy arrays.
        transcript_result: Dict with "segments" key containing transcript segments.

    Returns:
        Updated transcript_result with speaker assignments on segments and words.
    """
    segments = transcript_result.get("segments", [])
    if not segments or diarize_df is None or len(diarize_df) == 0:
        return transcript_result

    step_start = time.perf_counter()

    # Extract diarization intervals as numpy arrays
    d_starts = diarize_df.start.astype(np.float64)
    d_ends = diarize_df.end.astype(np.float64)
    d_speaker_labels = diarize_df.speaker

    # Build speaker index mapping for matrix multiply
    unique_speakers = np.unique(d_speaker_labels)
    speaker_to_idx = {s: i for i, s in enumerate(unique_speakers)}
    n_speakers = len(unique_speakers)
    d_speaker_indices = np.array([speaker_to_idx[s] for s in d_speaker_labels])

    # Speaker indicator matrix: (n_diarize, n_speakers) — one-hot encoding
    n_diarize = len(d_starts)
    speaker_matrix = np.zeros((n_diarize, n_speakers), dtype=np.float32)
    speaker_matrix[np.arange(n_diarize), d_speaker_indices] = 1.0

    # --- Assign speakers to segments ---
    seg_starts = np.array([s.get("start", 0.0) for s in segments], dtype=np.float64)
    seg_ends = np.array([s.get("end", 0.0) for s in segments], dtype=np.float64)
    seg_speakers = _batch_assign(
        seg_starts,
        seg_ends,
        d_starts,
        d_ends,
        speaker_matrix,
        unique_speakers,
    )
    for i, seg in enumerate(segments):
        if seg_speakers[i] is not None:
            seg["speaker"] = seg_speakers[i]
        else:
            # Fill nearest: find closest diarization midpoint
            seg_mid = (seg_starts[i] + seg_ends[i]) / 2
            mids = (d_starts + d_ends) / 2
            nearest_idx = np.argmin(np.abs(mids - seg_mid))
            seg["speaker"] = str(d_speaker_labels[nearest_idx])

    # --- Assign speakers to words (vectorized batch) ---
    # Collect all words with timestamps into flat arrays
    word_starts_list = []
    word_ends_list = []
    word_indices = []  # (segment_idx, word_idx) for writing back

    for si, seg in enumerate(segments):
        words = seg.get("words", [])
        for wi, word in enumerate(words):
            if "start" in word:
                word_starts_list.append(word["start"])
                word_ends_list.append(word.get("end", word["start"]))
                word_indices.append((si, wi))

    if word_starts_list:
        w_starts = np.array(word_starts_list, dtype=np.float64)
        w_ends = np.array(word_ends_list, dtype=np.float64)

        word_speakers, word_margins = _batch_assign(
            w_starts,
            w_ends,
            d_starts,
            d_ends,
            speaker_matrix,
            unique_speakers,
            return_margins=True,
        )

        # Write results back to word dicts. The transient "_overlap_margin" flags
        # genuinely-ambiguous boundary words for the smoother (issue #193 Phase 2);
        # it is stripped before persistence (storage only keeps word/start/end/score).
        for k, (si, wi) in enumerate(word_indices):
            word = segments[si]["words"][wi]
            word["_overlap_margin"] = word_margins[k]
            if word_speakers[k] is not None:
                word["speaker"] = word_speakers[k]
            else:
                # Fill nearest
                word_mid = (w_starts[k] + w_ends[k]) / 2
                mids = (d_starts + d_ends) / 2
                nearest_idx = np.argmin(np.abs(mids - word_mid))
                word["speaker"] = str(d_speaker_labels[nearest_idx])

    elapsed = time.perf_counter() - step_start
    n_words = len(word_starts_list)
    logger.info(
        f"Vectorized speaker assignment: {len(segments)} segments, {n_words} words, "
        f"{n_diarize} diarization intervals in {elapsed:.3f}s"
    )

    return transcript_result


def _batch_assign(
    query_starts: np.ndarray,
    query_ends: np.ndarray,
    d_starts: np.ndarray,
    d_ends: np.ndarray,
    speaker_matrix: np.ndarray,
    unique_speakers: np.ndarray,
    return_margins: bool = False,
) -> list | tuple[list, list]:
    """Batch-assign speakers to query intervals using matrix multiply.

    Computes the overlap between each query interval and all diarization
    intervals, accumulates per-speaker overlap via matmul, and returns
    the dominant speaker for each query.

    Args:
        query_starts: (N,) array of query start times
        query_ends: (N,) array of query end times
        d_starts: (M,) array of diarization start times
        d_ends: (M,) array of diarization end times
        speaker_matrix: (M, K) one-hot speaker indicator matrix
        unique_speakers: (K,) array of unique speaker labels
        return_margins: When True, also return per-query top1-top2 overlap margins
            (seconds). A small margin flags a boundary word as genuinely ambiguous
            (issue #193 Phase 2). No overlap → margin 0.0.

    Returns:
        List of N speaker labels (str or None if no overlap), or
        ``(speakers, margins)`` when ``return_margins`` is True.
    """
    n_queries = len(query_starts)
    results: list = [None] * n_queries
    margins: list = [0.0] * n_queries if return_margins else []
    n_speakers = speaker_matrix.shape[1]

    for chunk_start in range(0, n_queries, _CHUNK_SIZE):
        chunk_end = min(chunk_start + _CHUNK_SIZE, n_queries)

        # (chunk, 1) for broadcasting against (M,)
        qs = query_starts[chunk_start:chunk_end, None]
        qe = query_ends[chunk_start:chunk_end, None]

        # Overlap: max(0, min(query_end, diar_end) - max(query_start, diar_start))
        # Shape: (chunk, M)
        overlap = np.maximum(
            0, np.minimum(qe, d_ends[None, :]) - np.maximum(qs, d_starts[None, :])
        ).astype(np.float32)

        # Accumulate per-speaker overlap: (chunk, M) @ (M, K) -> (chunk, K)
        speaker_overlaps = overlap @ speaker_matrix

        # Pick dominant speaker per query
        has_overlap = speaker_overlaps.max(axis=1) > 0
        best_idx = np.argmax(speaker_overlaps, axis=1)

        if return_margins:
            # top1 - top2 overlap: small => the seam word is genuinely ambiguous.
            if n_speakers >= 2:
                part = np.partition(speaker_overlaps, -2, axis=1)
                chunk_margins = part[:, -1] - part[:, -2]
            else:
                chunk_margins = speaker_overlaps[:, 0]

        for i in range(chunk_end - chunk_start):
            if has_overlap[i]:
                results[chunk_start + i] = str(unique_speakers[best_idx[i]])
            if return_margins:
                margins[chunk_start + i] = float(chunk_margins[i])

    if return_margins:
        return results, margins
    return results
