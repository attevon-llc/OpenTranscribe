"""Offline post-processing reprocessor.

Loads raw GPU outputs (transcript + diarization) saved by the pipeline
and reruns the cheap post-processing steps (sentence split, speaker
assignment, dedup). No GPU needed — runs in <1s.

Usage:
    # From backend/ directory with venv active:
    PYTHONPATH=. python -m app.transcription.reprocess /path/to/debug_dir

    # Or specify output file:
    PYTHONPATH=. python -m app.transcription.reprocess /path/to/debug_dir -o result.json

    # Skip dedup to see raw speaker assignment:
    PYTHONPATH=. python -m app.transcription.reprocess /path/to/debug_dir --no-dedup

The debug_dir should contain:
    raw_transcript.json   - from pipeline's _save_intermediate()
    raw_diarization.json  - from pipeline's _save_intermediate()
"""

import argparse
import json
import logging
import os
import time
from typing import Any

import numpy as np

from app.transcription.diarize_result import DiarizeResult

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def load_intermediate(debug_dir: str) -> tuple[dict, DiarizeResult]:
    """Load raw transcript and diarization from saved files."""
    transcript_path = os.path.join(debug_dir, "raw_transcript.json")
    diarize_path = os.path.join(debug_dir, "raw_diarization.json")

    with open(transcript_path) as f:
        transcript = json.load(f)

    with open(diarize_path) as f:
        records = json.load(f)
    diarize_df = DiarizeResult(
        start=np.asarray([r["start"] for r in records], dtype=np.float64),
        end=np.asarray([r["end"] for r in records], dtype=np.float64),
        speaker=np.asarray([r["speaker"] for r in records], dtype=object),
    )

    logger.info(
        f"Loaded: {len(transcript.get('segments', []))} segments, "
        f"{sum(len(s.get('words', [])) for s in transcript.get('segments', []))} words, "
        f"{len(diarize_df)} diarization rows, "
        f"{int(np.unique(diarize_df.speaker).size)} speakers"
    )

    return transcript, diarize_df


def reprocess(
    transcript: dict,
    diarize_df: DiarizeResult,
    enable_dedup: bool = True,
) -> dict:
    """Run post-processing on saved intermediate data."""
    from app.transcription.speaker_assigner import assign_speakers
    from app.utils.segment_dedup import clean_segments

    result: dict[str, Any] = {
        "segments": list(transcript["segments"]),
        "language": transcript.get("language"),
    }

    # Step 1: Sentence split + dedup
    if enable_dedup:
        step_start = time.perf_counter()
        original = len(result["segments"])
        result["segments"] = clean_segments(result["segments"])
        logger.info(
            f"Dedup: {original} -> {len(result['segments'])} segments "
            f"({time.perf_counter() - step_start:.3f}s)"
        )

    # Step 2: Speaker assignment
    step_start = time.perf_counter()
    result = assign_speakers(diarize_df, result)

    assigned = sum(1 for s in result["segments"] if s.get("speaker"))
    total = len(result["segments"])
    logger.info(
        f"Speaker assignment: {assigned}/{total} segments assigned "
        f"({time.perf_counter() - step_start:.3f}s)"
    )

    return result


def print_summary(result: dict, num_lines: int = 20) -> None:
    """Print first N segments for human review."""
    segments = result.get("segments", [])
    logger.info(f"\n{'=' * 80}")
    logger.info(f"First {min(num_lines, len(segments))} segments:")
    logger.info(f"{'=' * 80}")

    for seg in segments[:num_lines]:
        speaker = seg.get("speaker", "???")
        start = seg.get("start", 0)
        end = seg.get("end", 0)
        text = seg.get("text", "")[:80]
        word_count = len(seg.get("words", []))
        logger.info(f"  [{speaker:>12}] {start:7.1f}-{end:7.1f}s ({word_count:3d}w) {text}")

    # Speaker stats
    speakers: dict[str, int] = {}
    unassigned = 0
    for seg in segments:
        sp = seg.get("speaker")
        if sp:
            speakers[sp] = speakers.get(sp, 0) + 1
        else:
            unassigned += 1

    logger.info(f"\n{'=' * 80}")
    logger.info(
        f"Total: {len(segments)} segments, {len(speakers)} speakers, {unassigned} unassigned"
    )
    for sp, count in sorted(speakers.items()):
        logger.info(f"  {sp}: {count} segments")


def main() -> None:
    parser = argparse.ArgumentParser(description="Reprocess saved pipeline data")
    parser.add_argument(
        "debug_dir", help="Directory with raw_transcript.json and raw_diarization.json"
    )
    parser.add_argument("-o", "--output", help="Save result to JSON file")
    parser.add_argument("--no-dedup", action="store_true", help="Skip sentence split + dedup")
    parser.add_argument("-n", "--lines", type=int, default=20, help="Number of segments to preview")
    args = parser.parse_args()

    transcript, diarize_df = load_intermediate(args.debug_dir)
    result = reprocess(transcript, diarize_df, enable_dedup=not args.no_dedup)
    print_summary(result, num_lines=args.lines)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(result, f, indent=2, default=str)
        logger.info(f"\nSaved to {args.output}")


if __name__ == "__main__":
    main()
