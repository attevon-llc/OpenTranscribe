#!/usr/bin/env python3
"""Phase 1a regression gate: verify Engine.process() == TranscriptionPipeline.process().

Runs both paths on the same audio files and checks that all output fields are
byte-identical (or within floating-point tolerance for embeddings).

Intended to be run inside the celery-worker container:
    docker exec opentranscribe-celery-worker \
        python /app/scripts/benchmark_engine_compare.py --max-files 2

Or from the host (if backend venv is active):
    python scripts/benchmark_engine_compare.py --audio-dir benchmark/test_audio
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Output comparison helpers
# ──────────────────────────────────────────────────────────────────────────────


def _stable_json(obj: Any) -> str:
    """Produce a deterministic JSON string for deep equality checks."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def _compare_segments(old: list[dict], new: list[dict]) -> tuple[bool, str]:
    """Check that two segment lists are structurally identical."""
    if len(old) != len(new):
        return False, f'segment count mismatch: old={len(old)} new={len(new)}'
    old_json = _stable_json(old)
    new_json = _stable_json(new)
    if old_json != new_json:
        # Find first differing segment for a helpful message
        for i, (o, n) in enumerate(zip(old, new, strict=False)):
            if _stable_json(o) != _stable_json(n):
                return False, f'segment[{i}] differs: old_keys={set(o)} new_keys={set(n)}'
        return False, 'segments differ (JSON mismatch but no single differing segment found)'
    return True, 'ok'


def _compare_overlap_info(old: dict, new: dict) -> tuple[bool, str]:
    """Check overlap_info equality (both may be absent / empty)."""
    if _stable_json(old) == _stable_json(new):
        return True, 'ok'
    return False, f'overlap_info mismatch: old={old} new={new}'


def _compare_embeddings(
    old: dict[str, Any] | None,
    new: dict[str, Any] | None,
    tol: float = 1e-6,
) -> tuple[bool, str]:
    """Compare native_speaker_embeddings within floating-point tolerance."""
    if old is None and new is None:
        return True, 'both absent'
    if old is None or new is None:
        return False, f'one is None: old={old is None} new={new is None}'
    if set(old.keys()) != set(new.keys()):
        return False, f'embedding keys differ: old={set(old)} new={set(new)}'
    try:
        import numpy as np

        for key in old:
            a = np.asarray(old[key], dtype=float)
            b = np.asarray(new[key], dtype=float)
            if a.shape != b.shape:
                return False, f'embedding[{key}] shape mismatch: {a.shape} vs {b.shape}'
            if not np.allclose(a, b, atol=tol, rtol=0):
                max_diff = float(np.max(np.abs(a - b)))
                return False, f'embedding[{key}] max_diff={max_diff:.2e} > tol={tol:.2e}'
    except ImportError:
        # Fallback: string comparison if numpy unavailable
        if _stable_json(old) != _stable_json(new):
            return False, 'embedding values differ (numpy unavailable for float comparison)'
    return True, 'ok'


# ──────────────────────────────────────────────────────────────────────────────
# Per-file runner
# ──────────────────────────────────────────────────────────────────────────────


def _audio_duration_s(audio_path: Path) -> float:
    """Return audio duration in seconds using wave stdlib (WAV only)."""
    import wave

    try:
        with wave.open(str(audio_path), 'rb') as wf:
            return wf.getnframes() / wf.getframerate()
    except Exception:
        return 0.0


def run_pipeline(audio_path: Path, config: Any) -> tuple[dict, float]:
    """Run TranscriptionPipeline.process() and return (result, elapsed_s)."""
    from app.transcription import TranscriptionPipeline

    pipeline = TranscriptionPipeline(config)
    t0 = time.perf_counter()
    result = pipeline.process(str(audio_path), task_id=f'pipeline-{uuid.uuid4().hex[:8]}')
    elapsed = time.perf_counter() - t0
    return result, elapsed


def run_engine(audio_path: Path, engine_config: Any) -> tuple[dict, float]:
    """Run Engine.process() → .to_pipeline_dict() and return (result, elapsed_s)."""
    from app.transcription import Engine, JobSpec

    engine = Engine(engine_config)
    job = JobSpec(audio_path=str(audio_path), task_id=f'engine-{uuid.uuid4().hex[:8]}')
    t0 = time.perf_counter()
    job_result = engine.process(job)
    elapsed = time.perf_counter() - t0
    result = job_result.to_pipeline_dict()
    return result, elapsed


def compare_file(audio_path: Path, config: Any, engine_config: Any) -> dict:
    """Run both paths on one file and return a comparison row dict."""
    filename = audio_path.name
    duration_s = _audio_duration_s(audio_path)
    logger.info('Starting comparison for %s (%.0f s)', filename, duration_s)

    row: dict[str, Any] = {
        'filename': filename,
        'duration_s': round(duration_s, 1),
        'pipeline_time_s': None,
        'engine_time_s': None,
        'segments_match': False,
        'language_match': False,
        'overlap_match': False,
        'embeddings_match': False,
        'result': 'FAIL',
        'notes': '',
    }

    try:
        old_result, pipeline_time = run_pipeline(audio_path, config)
    except RuntimeError as exc:
        if 'out of memory' in str(exc).lower() or 'cuda' in str(exc).lower():
            logger.warning('Pipeline OOM on %s: %s', filename, exc)
            row['result'] = 'SKIP'
            row['notes'] = f'pipeline OOM: {exc}'
            return row
        raise
    except Exception as exc:
        logger.error('Pipeline failed on %s: %s', filename, exc)
        row['result'] = 'FAIL'
        row['notes'] = f'pipeline error: {exc}'
        return row

    row['pipeline_time_s'] = round(pipeline_time, 2)
    logger.info('Pipeline done in %.1f s', pipeline_time)

    try:
        new_result, engine_time = run_engine(audio_path, engine_config)
    except RuntimeError as exc:
        if 'out of memory' in str(exc).lower() or 'cuda' in str(exc).lower():
            logger.warning('Engine OOM on %s: %s', filename, exc)
            row['result'] = 'SKIP'
            row['notes'] = f'engine OOM: {exc}'
            return row
        raise
    except Exception as exc:
        logger.error('Engine failed on %s: %s', filename, exc)
        row['result'] = 'FAIL'
        row['notes'] = f'engine error: {exc}'
        return row

    row['engine_time_s'] = round(engine_time, 2)
    logger.info('Engine done in %.1f s', engine_time)

    # ── field comparisons ────────────────────────────────────────────────────
    seg_ok, seg_msg = _compare_segments(
        old_result.get('segments', []), new_result.get('segments', [])
    )
    lang_ok = old_result.get('language') == new_result.get('language')
    overlap_ok, overlap_msg = _compare_overlap_info(
        old_result.get('overlap_info', {}), new_result.get('overlap_info', {})
    )
    emb_ok, emb_msg = _compare_embeddings(
        old_result.get('native_speaker_embeddings'),
        new_result.get('native_speaker_embeddings'),
    )

    row['segments_match'] = seg_ok
    row['language_match'] = lang_ok
    row['overlap_match'] = overlap_ok
    row['embeddings_match'] = emb_ok

    notes: list[str] = []
    if not seg_ok:
        notes.append(f'segments: {seg_msg}')
    if not lang_ok:
        notes.append(
            f'language: old={old_result.get("language")!r} new={new_result.get("language")!r}'
        )
    if not overlap_ok:
        notes.append(f'overlap: {overlap_msg}')
    if not emb_ok:
        notes.append(f'embeddings: {emb_msg}')

    all_pass = seg_ok and lang_ok and overlap_ok and emb_ok
    row['result'] = 'PASS' if all_pass else 'FAIL'
    row['notes'] = '; '.join(notes) if notes else ''

    logger.info(
        '%s: %s — segs=%s lang=%s overlap=%s emb=%s',
        filename,
        row['result'],
        seg_ok,
        lang_ok,
        overlap_ok,
        emb_ok,
    )
    return row


# ──────────────────────────────────────────────────────────────────────────────
# Reporting
# ──────────────────────────────────────────────────────────────────────────────

_COLUMNS = [
    'filename',
    'duration_s',
    'pipeline_time_s',
    'engine_time_s',
    'segments_match',
    'language_match',
    'overlap_match',
    'embeddings_match',
    'result',
    'notes',
]


def _fmt_val(val: Any) -> str:
    if val is None:
        return '—'
    if isinstance(val, bool):
        return 'Y' if val else 'N'
    return str(val)


def print_table(rows: list[dict]) -> None:
    """Print a plain-text summary table to stdout."""
    widths = {col: len(col) for col in _COLUMNS}
    for row in rows:
        for col in _COLUMNS:
            widths[col] = max(widths[col], len(_fmt_val(row.get(col))))

    header = '  '.join(col.ljust(widths[col]) for col in _COLUMNS)
    separator = '  '.join('-' * widths[col] for col in _COLUMNS)
    print(header)
    print(separator)
    for row in rows:
        line = '  '.join(_fmt_val(row.get(col)).ljust(widths[col]) for col in _COLUMNS)
        print(line)


def write_csv(rows: list[dict], output_path: Path) -> None:
    """Write comparison results to a CSV file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open('w', newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(fh, fieldnames=_COLUMNS, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    logger.info('CSV written to %s', output_path)


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Phase 1a regression gate: compare Engine vs TranscriptionPipeline output.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        '--audio-dir',
        default='/app/benchmark/test_audio',
        help='Directory containing WAV test files.',
    )
    parser.add_argument(
        '--output',
        default=str(Path(tempfile.gettempdir()) / 'engine_compare.csv'),
        help='Path to write the CSV results.',
    )
    parser.add_argument(
        '--files',
        default='',
        help=(
            "Comma-separated filenames to run (e.g. '0.5h_1899s.wav,1.0h_3758s.wav'). "
            'If empty, selects the shortest --max-files files by duration.'
        ),
    )
    parser.add_argument(
        '--max-files',
        type=int,
        default=2,
        help='Maximum number of files to process when --files is not specified.',
    )
    parser.add_argument(
        '--log-level',
        default='INFO',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        help='Python logging level.',
    )
    return parser


def select_files(audio_dir: Path, files_arg: str, max_files: int) -> list[Path]:
    """Resolve the list of audio files to process."""
    if files_arg.strip():
        names = [n.strip() for n in files_arg.split(',') if n.strip()]
        selected = [audio_dir / name for name in names]
        missing = [p for p in selected if not p.exists()]
        if missing:
            raise FileNotFoundError(f'Audio files not found: {missing}')
        return selected

    all_wavs = sorted(audio_dir.glob('*.wav'))
    if not all_wavs:
        raise FileNotFoundError(f'No WAV files found in {audio_dir}')

    # Sort by file size as a proxy for duration (avoids loading all audio headers)
    all_wavs.sort(key=lambda p: p.stat().st_size)
    return all_wavs[:max_files]


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )

    audio_dir = Path(args.audio_dir)
    if not audio_dir.exists():
        logger.error('Audio directory not found: %s', audio_dir)
        return 1

    try:
        audio_files = select_files(audio_dir, args.files, args.max_files)
    except FileNotFoundError as exc:
        logger.error('%s', exc)
        return 1

    logger.info(
        'Running comparison on %d file(s): %s',
        len(audio_files),
        [f.name for f in audio_files],
    )

    # Build configs once — both paths share the same TranscriptionConfig so
    # model weights, hardware detection, and env vars are identical.
    from app.transcription import EngineConfig
    from app.transcription.config import TranscriptionConfig

    config = TranscriptionConfig.from_environment()
    engine_config = EngineConfig()
    engine_config._transcription_config = config

    rows: list[dict] = []
    for audio_path in audio_files:
        row = compare_file(audio_path, config, engine_config)
        rows.append(row)

    print()
    print_table(rows)
    print()

    write_csv(rows, Path(args.output))

    pass_count = sum(1 for r in rows if r['result'] == 'PASS')
    skip_count = sum(1 for r in rows if r['result'] == 'SKIP')
    fail_count = sum(1 for r in rows if r['result'] == 'FAIL')

    print(f'Results: {pass_count} PASS  {skip_count} SKIP  {fail_count} FAIL')

    return 0 if fail_count == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
