#!/usr/bin/env python3
"""Phase 1b per-stage latency benchmark for the Engine split-stage path.

Measures Stage 1 (preprocess), Stage 2 (GPU inference), and Stage 3 (finalize)
individually so bottlenecks can be identified without the noise of a combined run.

Intended to be run inside the celery-worker container:
    docker exec opentranscribe-celery-worker \
        python /app/scripts/benchmark_engine_single.py --audio /path/to/file.wav

Or from the host (if backend venv is active):
    python scripts/benchmark_engine_single.py --audio benchmark/test_audio/sample.wav
"""

from __future__ import annotations

import argparse
import csv
import logging
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any

logger = logging.getLogger(__name__)

_COLUMNS = [
    'run',
    'audio_file',
    'audio_duration_s',
    'stage1_preprocess_s',
    'stage2_gpu_s',
    'stage3_finalize_s',
    'total_s',
    'realtime_factor',
    'shared_vol_used',
    'cuda_device',
    'segments_count',
    'language',
    'hostname',
    'timestamp',
]


# ──────────────────────────────────────────────────────────────────────────────
# Audio utilities
# ──────────────────────────────────────────────────────────────────────────────


def _probe_duration_ffprobe(audio_path: Path) -> float:
    """Return audio duration in seconds via ffprobe; returns 0.0 on failure."""
    try:
        result = subprocess.run(  # noqa: S603
            [
                'ffprobe',
                '-v',
                'error',
                '-show_entries',
                'format=duration',
                '-of',
                'default=noprint_wrappers=1:nokey=1',
                str(audio_path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def _probe_duration_wave(audio_path: Path) -> float:
    """Return WAV duration from stdlib wave module; returns 0.0 on failure."""
    import wave

    try:
        with wave.open(str(audio_path), 'rb') as wf:
            return wf.getnframes() / wf.getframerate()
    except Exception:
        return 0.0


def probe_duration(audio_path: Path) -> float:
    duration = _probe_duration_wave(audio_path)
    if duration > 0:
        return duration
    return _probe_duration_ffprobe(audio_path)


# ──────────────────────────────────────────────────────────────────────────────
# Single-run executor
# ──────────────────────────────────────────────────────────────────────────────


def run_single(
    audio_path: Path,
    engine: Any,
    run_index: int,
    cuda_device: int,
    use_shared_vol: bool,
) -> dict:
    """Execute one benchmark run through all three engine stages.

    Returns a flat dict matching _COLUMNS (ready for CSV).
    """
    from app.transcription.engine.job import JobSpec

    task_id = f'bench-{uuid.uuid4().hex[:8]}'
    job = JobSpec(audio_path=str(audio_path), task_id=task_id)

    logger.info('Run %d — stage 1 (preprocess) …', run_index)
    t_pre0 = time.perf_counter()
    pre_result = engine.run_preprocess(job)
    stage1_s = time.perf_counter() - t_pre0

    audio_duration_s = pre_result.audio_duration_s or probe_duration(audio_path)

    if not use_shared_vol:
        # Override the shared-volume path so Stage 2 uses an in-memory path
        # (set local_wav_path to the original file — stages tolerate any readable path).
        pre_result.local_wav_path = str(audio_path)

    logger.info('Run %d — stage 2 (GPU inference) …', run_index)
    t_gpu0 = time.perf_counter()
    raw_result = engine.run_gpu_stage(pre_result)
    stage2_s = time.perf_counter() - t_gpu0

    logger.info('Run %d — stage 3 (finalize) …', run_index)
    t_fin0 = time.perf_counter()
    job_result = engine.run_cpu_finalize(raw_result)
    stage3_s = time.perf_counter() - t_fin0

    total_s = stage1_s + stage2_s + stage3_s
    rtf = audio_duration_s / total_s if total_s > 0 else 0.0

    return {
        'run': run_index,
        'audio_file': audio_path.name,
        'audio_duration_s': round(audio_duration_s, 2),
        'stage1_preprocess_s': round(stage1_s, 3),
        'stage2_gpu_s': round(stage2_s, 3),
        'stage3_finalize_s': round(stage3_s, 3),
        'total_s': round(total_s, 3),
        'realtime_factor': round(rtf, 2),
        'shared_vol_used': use_shared_vol,
        'cuda_device': cuda_device,
        'segments_count': len(job_result.segments),
        'language': job_result.language,
        'hostname': socket.gethostname(),
        'timestamp': datetime.now(tz=UTC).isoformat(),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Reporting
# ──────────────────────────────────────────────────────────────────────────────


def _fmt_val(val: Any) -> str:
    if val is None:
        return '—'
    if isinstance(val, bool):
        return 'Y' if val else 'N'
    return str(val)


def print_run_summary(row: dict) -> None:
    print(
        f'  Run {row["run"]:>2}  |'
        f'  preprocess {row["stage1_preprocess_s"]:>7.3f}s  |'
        f'  gpu {row["stage2_gpu_s"]:>7.3f}s  |'
        f'  finalize {row["stage3_finalize_s"]:>7.3f}s  |'
        f'  total {row["total_s"]:>7.3f}s  |'
        f'  RTF {row["realtime_factor"]:>5.2f}x  |'
        f'  lang={row["language"]}  segs={row["segments_count"]}'
    )


def print_aggregate(rows: list[dict]) -> None:
    if not rows:
        return
    stages: dict[str, str] = {
        'stage1_preprocess_s': 'preprocess',
        'stage2_gpu_s': 'gpu',
        'stage3_finalize_s': 'finalize',
        'total_s': 'total',
        'realtime_factor': 'RTF',
    }
    print()
    print(f'  {"Stage":<14}  {"min":>8}  {"mean":>8}  {"max":>8}')
    print(f'  {"-" * 14}  {"-" * 8}  {"-" * 8}  {"-" * 8}')
    for col, label in stages.items():
        vals = [r[col] for r in rows]
        unit = 'x' if col == 'realtime_factor' else 's'
        print(
            f'  {label:<14}  {min(vals):>7.3f}{unit}  {mean(vals):>7.3f}{unit}'
            f'  {max(vals):>7.3f}{unit}'
        )
    print()


def print_table(rows: list[dict]) -> None:
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
    ts = datetime.now(tz=UTC).strftime('%Y%m%dT%H%M%S')
    parser = argparse.ArgumentParser(
        description='Phase 1b per-stage latency benchmark for the Engine split-stage path.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        '--audio',
        required=True,
        metavar='PATH',
        help='Audio/video file to benchmark.',
    )
    parser.add_argument(
        '--runs',
        type=int,
        default=3,
        metavar='N',
        help='Number of repeated runs.',
    )
    parser.add_argument(
        '--output',
        default=str(Path(tempfile.gettempdir()) / f'engine_single_{ts}.csv'),
        metavar='CSV',
        help='Output CSV path.',
    )
    parser.add_argument(
        '--cuda-device',
        type=int,
        default=0,
        metavar='N',
        help='CUDA device index.',
    )
    parser.add_argument(
        '--no-shared-vol',
        action='store_true',
        default=False,
        help='Skip shared-volume WAV write; use original file path for Stage 2 instead.',
    )
    parser.add_argument(
        '--log-level',
        default='INFO',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        help='Python logging level.',
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )

    audio_path = Path(args.audio)
    if not audio_path.exists():
        logger.error('Audio file not found: %s', audio_path)
        return 1

    import os

    os.environ.setdefault('CUDA_VISIBLE_DEVICES', str(args.cuda_device))

    from app.transcription.engine import Engine, EngineConfig

    engine_config = EngineConfig.from_environment()
    engine = Engine(engine_config)

    use_shared_vol = not args.no_shared_vol
    audio_duration_s = probe_duration(audio_path)
    logger.info(
        'Benchmarking %s (%.1f s) — %d run(s), cuda_device=%d, shared_vol=%s',
        audio_path.name,
        audio_duration_s,
        args.runs,
        args.cuda_device,
        use_shared_vol,
    )
    print()

    rows: list[dict] = []
    for i in range(1, args.runs + 1):
        try:
            row = run_single(
                audio_path,
                engine,
                run_index=i,
                cuda_device=args.cuda_device,
                use_shared_vol=use_shared_vol,
            )
        except RuntimeError as exc:
            if 'out of memory' in str(exc).lower() or 'cuda' in str(exc).lower():
                logger.error('OOM on run %d: %s — aborting remaining runs', i, exc)
                break
            raise
        rows.append(row)
        print_run_summary(row)

    if not rows:
        logger.error('No successful runs.')
        return 1

    print()
    print_table(rows)
    print_aggregate(rows)

    write_csv(rows, Path(args.output))
    print(f'Completed {len(rows)}/{args.runs} run(s). CSV: {args.output}')

    return 0


if __name__ == '__main__':
    sys.exit(main())
