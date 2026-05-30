"""Vectorized transcript comparison utility for pipeline benchmarking.

Compares transcript results across pipeline runs using numpy for efficient
timestamp alignment and text similarity computation. Used to verify that
pipeline changes (model upgrades, parameter tuning) do not degrade output
quality beyond acceptable thresholds.
"""

import json
import logging
from datetime import datetime
from datetime import timezone

import numpy as np

from app.models.media import Speaker
from app.models.media import TranscriptSegment

logger = logging.getLogger(__name__)


def export_baseline(db, file_id: int, output_path: str) -> dict:
    """Export transcript segments and speakers as a JSON baseline snapshot.

    Args:
        db: SQLAlchemy database session.
        file_id: Internal integer ID of the media file.
        output_path: Filesystem path to write the JSON snapshot.

    Returns:
        Dictionary containing the exported baseline data including
        segment_count, speaker_count, and full segment/speaker details.
    """
    segments = (
        db.query(TranscriptSegment)
        .filter(TranscriptSegment.media_file_id == file_id)
        .order_by(TranscriptSegment.start_time)
        .all()
    )
    speakers = (
        db.query(Speaker.id, Speaker.name, Speaker.display_name)
        .filter(Speaker.media_file_id == file_id)
        .all()
    )
    speaker_map = {s.id: s.name for s in speakers}

    baseline = {
        "file_id": file_id,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "segment_count": len(segments),
        "speaker_count": len(speakers),
        "segments": [
            {
                "start": s.start_time,
                "end": s.end_time,
                "text": s.text,
                "speaker_name": speaker_map.get(s.speaker_id, "UNKNOWN"),
                "is_overlap": s.is_overlap,
                "words": s.words or [],
            }
            for s in segments
        ],
        "speakers": [
            {
                "id": sp.id,
                "name": sp.name,
                "display_name": sp.display_name,
            }
            for sp in speakers
        ],
    }

    with open(output_path, "w") as f:
        json.dump(baseline, f, indent=2)

    logger.info(
        "Exported baseline for file_id=%d: %d segments, %d speakers -> %s",
        file_id,
        len(segments),
        len(speakers),
        output_path,
    )
    return baseline


def export_word_reference(
    db,
    file_id: int,
    out_words_json: str,
    out_rttm: str | None = None,
) -> dict:
    """Export a word-level diarization REFERENCE from a hand-corrected transcript (issue #193).

    Each word inherits its (human-corrected) segment speaker, producing the parallel
    word inventory used by WSER plus an optional RTTM for DER. Run in-container with a DB
    session — the word JSONB is not exposed via the API. Words without start/end are
    dropped (they cannot be timed against the diarization).

    Args:
        db: SQLAlchemy session.
        file_id: Internal media file id.
        out_words_json: Path for ``reference.words.json`` (``[{start,end,word,speaker}]``).
        out_rttm: Optional path for ``reference.rttm`` (same-speaker words collapsed).

    Returns:
        Dict with ``word_count``, ``speaker_count``, and the written paths.
    """
    import json

    from app.utils.diarization_metrics import words_to_rttm

    segments = (
        db.query(TranscriptSegment)
        .filter(TranscriptSegment.media_file_id == file_id)
        .order_by(TranscriptSegment.start_time)
        .all()
    )
    speakers = (
        db.query(Speaker.id, Speaker.name, Speaker.display_name)
        .filter(Speaker.media_file_id == file_id)
        .all()
    )
    speaker_map = {s.id: (s.display_name or s.name) for s in speakers}

    words: list[dict] = []
    for seg in segments:
        spk = speaker_map.get(seg.speaker_id, "UNKNOWN")
        for w in seg.words or []:
            if w.get("start") is None or w.get("end") is None:
                continue
            words.append(
                {
                    "start": float(w["start"]),
                    "end": float(w["end"]),
                    "word": w.get("word", ""),
                    "speaker": spk,
                }
            )

    with open(out_words_json, "w") as f:
        json.dump(words, f, indent=2)

    uri = f"file_{file_id}"
    if out_rttm:
        with open(out_rttm, "w") as f:
            f.write(words_to_rttm(words, uri=uri))

    n_speakers = len({w["speaker"] for w in words})
    logger.info(
        "Exported word reference for file_id=%d: %d words, %d speakers -> %s",
        file_id,
        len(words),
        n_speakers,
        out_words_json,
    )
    return {
        "word_count": len(words),
        "speaker_count": n_speakers,
        "words_json": out_words_json,
        "rttm": out_rttm,
    }


def compare_transcripts(baseline: dict, current: dict) -> dict:
    """Compare two transcript snapshots using vectorized numpy operations.

    Performs timestamp alignment via distance matrix broadcasting, then
    computes text similarity, speaker consistency, and timing accuracy
    metrics on the aligned segment pairs.

    Args:
        baseline: Baseline transcript snapshot dictionary (from export_baseline).
        current: Current transcript snapshot dictionary to compare against.

    Returns:
        Dictionary with comparison metrics including:
        - segment_count_diff: Absolute difference in segment counts.
        - timestamp_start_mae_seconds: Mean absolute error of start times.
        - timestamp_end_mae_seconds: Mean absolute error of end times.
        - text_exact_match_pct: Percentage of exact text matches.
        - text_word_overlap_avg: Average word overlap ratio (Jaccard).
        - speaker_consistency_pct: Percentage of speaker label agreement.
        - speaker_mapping: Baseline-to-current speaker label mapping.
        - pass_text, pass_timestamps, pass_speakers, pass_overall: Booleans.
    """
    b_segs = baseline.get("segments", [])
    c_segs = current.get("segments", [])

    # Handle empty edge cases
    if not b_segs and not c_segs:
        return _empty_comparison(baseline, current, match=True)
    if not b_segs or not c_segs:
        return _empty_comparison(baseline, current, match=False)

    # Vectorized timestamp arrays
    b_starts = np.array([s["start"] for s in b_segs], dtype=np.float64)
    b_ends = np.array([s["end"] for s in b_segs], dtype=np.float64)
    c_starts = np.array([s["start"] for s in c_segs], dtype=np.float64)
    c_ends = np.array([s["end"] for s in c_segs], dtype=np.float64)

    # Distance matrix: shape (len(b_segs), len(c_segs))
    dist_matrix = np.abs(b_starts[:, None] - c_starts[None, :])
    nearest_indices = np.argmin(dist_matrix, axis=1)

    # Timestamp MAE on matched pairs
    matched_c_starts = c_starts[nearest_indices]
    matched_c_ends = c_ends[nearest_indices]
    start_mae = float(np.mean(np.abs(b_starts - matched_c_starts)))
    end_mae = float(np.mean(np.abs(b_ends - matched_c_ends)))

    # Text comparison on matched pairs
    exact_matches = 0
    word_overlaps = []
    for i, ci in enumerate(nearest_indices):
        b_text = b_segs[i].get("text", "").strip()
        c_text = c_segs[ci].get("text", "").strip()

        if b_text == c_text:
            exact_matches += 1

        b_words = set(b_text.lower().split())
        c_words = set(c_text.lower().split())
        if b_words or c_words:
            union = b_words | c_words
            intersection = b_words & c_words
            word_overlaps.append(len(intersection) / len(union) if union else 0.0)
        else:
            word_overlaps.append(1.0)

    text_exact_match_pct = (exact_matches / len(b_segs)) * 100.0
    avg_word_overlap = float(np.mean(word_overlaps)) * 100.0

    # Speaker consistency with remapping
    speaker_mapping = _build_speaker_mapping(b_segs, c_segs, nearest_indices)
    speaker_matches = 0
    for i, ci in enumerate(nearest_indices):
        b_speaker = b_segs[i].get("speaker_name", "UNKNOWN")
        c_speaker = c_segs[ci].get("speaker_name", "UNKNOWN")
        mapped_speaker = speaker_mapping.get(b_speaker, b_speaker)
        if mapped_speaker == c_speaker:
            speaker_matches += 1
    speaker_consistency_pct = (speaker_matches / len(b_segs)) * 100.0

    # Pass/fail thresholds
    pass_text = text_exact_match_pct > 80 or avg_word_overlap > 85.0
    pass_timestamps = start_mae < 5.0 and end_mae < 5.0
    pass_speakers = speaker_consistency_pct > 75.0
    pass_overall = pass_text and pass_timestamps and pass_speakers

    return {
        "baseline_file_id": baseline.get("file_id"),
        "current_file_id": current.get("file_id"),
        "baseline_segment_count": len(b_segs),
        "current_segment_count": len(c_segs),
        "segment_count_diff": abs(len(b_segs) - len(c_segs)),
        "timestamp_start_mae_seconds": round(start_mae, 4),
        "timestamp_end_mae_seconds": round(end_mae, 4),
        "text_exact_match_pct": round(text_exact_match_pct, 2),
        "text_word_overlap_avg": round(avg_word_overlap, 2),
        "speaker_consistency_pct": round(speaker_consistency_pct, 2),
        "speaker_mapping": speaker_mapping,
        "pass_text": pass_text,
        "pass_timestamps": pass_timestamps,
        "pass_speakers": pass_speakers,
        "pass_overall": pass_overall,
    }


def _build_speaker_mapping(
    b_segs: list[dict],
    c_segs: list[dict],
    nearest_indices: np.ndarray,
) -> dict:
    """Map baseline speaker labels to current labels by maximum text overlap.

    Uses a numpy overlap matrix with greedy assignment to find the best
    one-to-one mapping between baseline and current speaker labels.

    Args:
        b_segs: Baseline segment list with speaker_name and text fields.
        c_segs: Current segment list with speaker_name and text fields.
        nearest_indices: Array mapping baseline segment indices to their
            nearest current segment indices (from timestamp alignment).

    Returns:
        Dictionary mapping baseline speaker names to current speaker names.
    """
    # Collect unique speakers
    b_speakers = sorted({s.get("speaker_name", "UNKNOWN") for s in b_segs})
    c_speakers = sorted({s.get("speaker_name", "UNKNOWN") for s in c_segs})

    if not b_speakers or not c_speakers:
        return {}

    b_idx = {name: i for i, name in enumerate(b_speakers)}
    c_idx = {name: i for i, name in enumerate(c_speakers)}

    # Build overlap matrix: shape (len(b_speakers), len(c_speakers))
    overlap_matrix = np.zeros((len(b_speakers), len(c_speakers)), dtype=np.float64)

    for i, ci in enumerate(nearest_indices):
        b_name = b_segs[i].get("speaker_name", "UNKNOWN")
        c_name = c_segs[ci].get("speaker_name", "UNKNOWN")

        b_words = set(b_segs[i].get("text", "").lower().split())
        c_words = set(c_segs[ci].get("text", "").lower().split())

        if b_words or c_words:
            union = b_words | c_words
            intersection = b_words & c_words
            overlap = len(intersection) / len(union) if union else 0.0
        else:
            overlap = 0.0

        bi = b_idx[b_name]
        cj = c_idx[c_name]
        overlap_matrix[bi, cj] += overlap

    # Greedy assignment: pick highest overlap pairs iteratively
    mapping = {}
    used_current = set()
    remaining = list(range(len(b_speakers)))

    while remaining:
        best_score = -1.0
        best_bi = -1
        best_cj = -1

        for bi in remaining:
            for cj in range(len(c_speakers)):
                if cj in used_current:
                    continue
                if overlap_matrix[bi, cj] > best_score:
                    best_score = overlap_matrix[bi, cj]
                    best_bi = bi
                    best_cj = cj

        if best_bi < 0 or best_cj < 0 or best_score <= 0.0:
            break

        mapping[b_speakers[best_bi]] = c_speakers[best_cj]
        used_current.add(best_cj)
        remaining.remove(best_bi)

    return mapping


def _empty_comparison(baseline: dict, current: dict, match: bool) -> dict:
    """Return a comparison result for edge cases with empty segments.

    Args:
        baseline: Baseline transcript snapshot.
        current: Current transcript snapshot.
        match: Whether the empty states should be considered a match.

    Returns:
        Comparison dictionary with zeroed metrics and pass/fail based on match.
    """
    b_count = len(baseline.get("segments", []))
    c_count = len(current.get("segments", []))
    return {
        "baseline_file_id": baseline.get("file_id"),
        "current_file_id": current.get("file_id"),
        "baseline_segment_count": b_count,
        "current_segment_count": c_count,
        "segment_count_diff": abs(b_count - c_count),
        "timestamp_start_mae_seconds": 0.0,
        "timestamp_end_mae_seconds": 0.0,
        "text_exact_match_pct": 100.0 if match else 0.0,
        "text_word_overlap_avg": 100.0 if match else 0.0,
        "speaker_consistency_pct": 100.0 if match else 0.0,
        "speaker_mapping": {},
        "pass_text": match,
        "pass_timestamps": match,
        "pass_speakers": match,
        "pass_overall": match,
    }
