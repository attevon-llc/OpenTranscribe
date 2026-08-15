#!/usr/bin/env python3
"""Database analysis and reprocessing time projection.

Queries PostgreSQL for total audio hours, file distribution, and historical
processing times, then projects how long it would take to reprocess all files
at various concurrency levels.

Can use measured benchmark data (from benchmark_e2e.py CSV) or a manually
provided realtime factor.

Requires:
    - PostgreSQL accessible via docker exec (opentranscribe-postgres)

Usage:
    # Auto-detect realtime factor from historical task data
    python scripts/benchmark_projection.py

    # Use measured realtime factor from benchmark
    python scripts/benchmark_projection.py --realtime-factor 32.2

    # Read benchmark CSV for measured data
    python scripts/benchmark_projection.py --benchmark-csv benchmark_results.csv

    # Specify hardware for projection labels
    python scripts/benchmark_projection.py --gpu-name "RTX A6000 (49GB)"
"""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from datetime import datetime

DB_CONTAINER = 'opentranscribe-postgres'
DB_USER = 'postgres'
DB_NAME = 'opentranscribe'


def db_query(sql: str) -> list[list[str]]:
    """Run a SQL query via docker exec and return rows."""
    result = subprocess.run(
        [
            'docker',
            'exec',
            DB_CONTAINER,
            'psql',
            '-U',
            DB_USER,
            '-d',
            DB_NAME,
            '-t',
            '-A',
            '-F',
            '\t',
            '-c',
            sql,
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        print(f'DB ERROR: {result.stderr.strip()}', file=sys.stderr)
        return []
    rows = []
    for line in result.stdout.strip().split('\n'):
        line = line.strip()
        if line:
            rows.append(line.split('\t'))
    return rows


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f'{seconds:.1f}s'
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f'{m}m{s:02d}s'
    h, m = divmod(m, 60)
    return f'{h}h{m:02d}m{s:02d}s'


def _fmt_size(bytes_val: int) -> str:
    if bytes_val < 1024**3:
        return f'{bytes_val / (1024**2):.1f} MB'
    return f'{bytes_val / (1024**3):.2f} GB'


def get_file_summary() -> dict:
    """Get summary statistics for all completed files."""
    rows = db_query(
        'SELECT '
        '  COUNT(*), '
        '  COALESCE(SUM(duration), 0), '
        '  COALESCE(AVG(duration), 0), '
        '  COALESCE(MIN(duration), 0), '
        '  COALESCE(MAX(duration), 0), '
        '  COALESCE(SUM(file_size), 0) '
        "FROM media_file WHERE status = 'completed' AND duration > 0"
    )
    if not rows:
        return {}
    return {
        'total_files': int(rows[0][0]),
        'total_duration_s': float(rows[0][1]),
        'avg_duration_s': float(rows[0][2]),
        'min_duration_s': float(rows[0][3]),
        'max_duration_s': float(rows[0][4]),
        'total_size_bytes': int(float(rows[0][5])),
    }


def get_all_status_counts() -> dict:
    """Get file counts by status."""
    rows = db_query('SELECT status, COUNT(*) FROM media_file GROUP BY status ORDER BY status')
    return {row[0].strip(): int(row[1]) for row in rows}


def get_duration_distribution() -> list[dict]:
    """Get file count and hours by duration bucket."""
    rows = db_query(
        'SELECT '
        '  CASE '
        "    WHEN duration < 300 THEN '< 5min' "
        "    WHEN duration < 1800 THEN '5-30min' "
        "    WHEN duration < 3600 THEN '30-60min' "
        "    WHEN duration < 7200 THEN '1-2hr' "
        "    WHEN duration < 10800 THEN '2-3hr' "
        "    ELSE '3hr+' "
        '  END AS bucket, '
        '  COUNT(*), '
        '  COALESCE(SUM(duration) / 3600.0, 0), '
        '  COALESCE(AVG(duration), 0), '
        '  COALESCE(SUM(file_size), 0) '
        'FROM media_file '
        "WHERE status = 'completed' AND duration > 0 "
        'GROUP BY 1 ORDER BY MIN(duration)'
    )
    dist = []
    for row in rows:
        dist.append(
            {
                'bucket': row[0].strip(),
                'file_count': int(row[1]),
                'total_hours': float(row[2]),
                'avg_duration_s': float(row[3]),
                'total_size_bytes': int(float(row[4])),
            }
        )
    return dist


def get_historical_processing_stats() -> dict:
    """Get processing time stats from completed transcription tasks."""
    rows = db_query(
        'SELECT '
        '  COUNT(*), '
        '  COALESCE(AVG(EXTRACT(EPOCH FROM (t.completed_at - t.created_at))), 0), '
        '  COALESCE(MIN(EXTRACT(EPOCH FROM (t.completed_at - t.created_at))), 0), '
        '  COALESCE(MAX(EXTRACT(EPOCH FROM (t.completed_at - t.created_at))), 0) '
        'FROM task t '
        "WHERE t.task_type = 'transcription' "
        "  AND t.status = 'completed' "
        '  AND t.completed_at IS NOT NULL '
        '  AND t.created_at IS NOT NULL'
    )
    if not rows or int(rows[0][0]) == 0:
        return {}
    return {
        'task_count': int(rows[0][0]),
        'avg_processing_s': float(rows[0][1]),
        'min_processing_s': float(rows[0][2]),
        'max_processing_s': float(rows[0][3]),
    }


def get_realtime_factor_from_history() -> float:
    """Calculate realtime factor from historical task+file data."""
    rows = db_query(
        'SELECT '
        '  COALESCE(SUM(m.duration), 0) AS total_audio_s, '
        '  COALESCE(SUM(EXTRACT(EPOCH FROM (t.completed_at - t.created_at))), 0) AS total_proc_s '
        'FROM task t '
        'JOIN media_file m ON m.active_task_id = CAST(t.id AS TEXT) '
        "WHERE t.task_type = 'transcription' "
        "  AND t.status = 'completed' "
        '  AND t.completed_at IS NOT NULL '
        '  AND m.duration > 0'
    )
    if not rows:
        return 0
    total_audio = float(rows[0][0])
    total_proc = float(rows[0][1])
    if total_proc > 0:
        return total_audio / total_proc
    return 0


def read_benchmark_csv(csv_path: str) -> dict:
    """Read benchmark_e2e.py CSV and extract realtime-relevant data."""
    try:
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        if not rows:
            return {}
        gpu_durations = []
        total_durations = []
        for row in rows:
            if 'gpu_duration' in row and row['gpu_duration']:
                gpu_durations.append(float(row['gpu_duration']))
            if 'total_dispatch_to_postprocess' in row and row['total_dispatch_to_postprocess']:
                total_durations.append(float(row['total_dispatch_to_postprocess']))
        return {
            'iterations': len(rows),
            'avg_gpu_s': sum(gpu_durations) / len(gpu_durations) if gpu_durations else 0,
            'avg_total_s': sum(total_durations) / len(total_durations) if total_durations else 0,
        }
    except Exception as e:
        print(f'WARNING: Could not read benchmark CSV: {e}', file=sys.stderr)
        return {}


def main():
    parser = argparse.ArgumentParser(
        description='Database analysis and reprocessing time projection',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--realtime-factor',
        type=float,
        default=0,
        help='Measured realtime factor (audio_duration / gpu_duration). Auto-detected if not set.',
    )
    parser.add_argument(
        '--benchmark-csv',
        default='',
        help='Path to benchmark_e2e.py CSV for measured timing data',
    )
    parser.add_argument(
        '--gpu-name',
        default='',
        help="GPU name for projection labels (e.g., 'RTX A6000 (49GB)')",
    )
    parser.add_argument(
        '--overhead-per-file',
        type=float,
        default=20,
        help='Estimated non-GPU overhead per file in seconds (default: 20)',
    )
    args = parser.parse_args()

    # ── Database Summary ─────────────────────────────────────────────────
    summary = get_file_summary()
    if not summary:
        print('ERROR: No completed files found in database.', file=sys.stderr)
        sys.exit(1)

    status_counts = get_all_status_counts()
    distribution = get_duration_distribution()
    historical = get_historical_processing_stats()

    total_files = summary['total_files']
    total_hours = summary['total_duration_s'] / 3600
    total_days = total_hours / 24

    print('=' * 80)
    print('OPENTRANSCRIBE REPROCESSING PROJECTION')
    print(f'Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    print('=' * 80)

    print('\nDATABASE SUMMARY')
    print('-' * 80)
    total_all = sum(status_counts.values())
    print(f'  Total files in system:     {total_all}')
    for status, count in sorted(status_counts.items()):
        print(f'    {status:<20}       {count}')
    print(f'\n  Completed files:           {total_files}')
    print(f'  Total audio duration:      {total_hours:.1f} hours ({total_days:.1f} days)')
    print(f'  Average file duration:     {_fmt_duration(summary["avg_duration_s"])}')
    print(f'  Shortest file:             {_fmt_duration(summary["min_duration_s"])}')
    print(f'  Longest file:              {_fmt_duration(summary["max_duration_s"])}')
    print(f'  Total storage size:        {_fmt_size(summary["total_size_bytes"])}')

    if distribution:
        print('\n  Duration Distribution:')
        print(f'    {"Bucket":<12} {"Files":>8} {"Hours":>10} {"Avg Dur":>12} {"Size":>12}')
        print(f'    {"─" * 12} {"─" * 8} {"─" * 10} {"─" * 12} {"─" * 12}')
        for d in distribution:
            print(
                f'    {d["bucket"]:<12} {d["file_count"]:>8} '
                f'{d["total_hours"]:>9.1f} '
                f'{_fmt_duration(d["avg_duration_s"]):>12} '
                f'{_fmt_size(d["total_size_bytes"]):>12}'
            )

    # ── Realtime Factor ──────────────────────────────────────────────────
    rt_factor = args.realtime_factor
    rt_source = ''

    if rt_factor > 0:
        rt_source = 'command-line argument'
    elif args.benchmark_csv:
        bench = read_benchmark_csv(args.benchmark_csv)
        if bench.get('avg_gpu_s', 0) > 0:
            # We need the file duration — get it from the CSV filename or ask
            print(
                f'\n  Benchmark CSV: {bench["iterations"]} iterations, '
                f'avg GPU = {_fmt_duration(bench["avg_gpu_s"])}'
            )
            print('  NOTE: Provide --realtime-factor to use benchmark data for projection')
    else:
        rt_factor = get_realtime_factor_from_history()
        if rt_factor > 0:
            rt_source = 'historical task data (active_task_id join)'
        else:
            # Fallback: use avg processing time / avg duration
            if historical and historical.get('avg_processing_s', 0) > 0:
                rt_factor = summary['avg_duration_s'] / historical['avg_processing_s']
                rt_source = 'estimated (avg_duration / avg_processing_time)'

    if historical:
        print('\n  Historical Processing Stats (completed transcription tasks):')
        print(f'    Tasks completed:     {historical["task_count"]}')
        print(f'    Avg processing time: {_fmt_duration(historical["avg_processing_s"])}')
        print(f'    Min processing time: {_fmt_duration(historical["min_processing_s"])}')
        print(f'    Max processing time: {_fmt_duration(historical["max_processing_s"])}')

    if rt_factor <= 0:
        print('\n  WARNING: Could not determine realtime factor. Provide --realtime-factor.')
        print('  Run benchmark_e2e.py first to measure actual GPU throughput.')
        sys.exit(1)

    gpu_label = args.gpu_name if args.gpu_name else 'GPU'
    overhead = args.overhead_per_file

    print(f'\n  Realtime Factor:           {rt_factor:.1f}x ({rt_source})')
    print(f'  1 hour of audio takes:     {_fmt_duration(3600 / rt_factor)}')
    print(f'  Non-GPU overhead/file:     {overhead:.0f}s')

    # ── Projections ──────────────────────────────────────────────────────
    # Time per file = (avg_audio_duration / rt_factor) + overhead
    avg_file_gpu_s = summary['avg_duration_s'] / rt_factor
    avg_file_total_s = avg_file_gpu_s + overhead

    print('\nREPROCESSING TIME PROJECTIONS')
    print('-' * 80)
    print(
        f'  Avg file: {_fmt_duration(summary["avg_duration_s"])} audio -> '
        f'{_fmt_duration(avg_file_gpu_s)} GPU + {overhead:.0f}s overhead = '
        f'{_fmt_duration(avg_file_total_s)} per file'
    )
    print()
    print(f'  {"Configuration":<40} {"Workers":>8} {"Est. Time":>12} {"Audio/hr":>10}')
    print(f'  {"─" * 40} {"─" * 8} {"─" * 12} {"─" * 10}')

    configs = [
        (f'{gpu_label} sequential', 1),
        (f'{gpu_label} concurrent=2', 2),
        (f'{gpu_label} concurrent=3', 3),
        (f'{gpu_label} concurrent=4', 4),
        ('Dual-GPU (1+4 workers)', 5),
        ('Triple-GPU (1+4+4 workers)', 9),
    ]

    for label, workers in configs:
        # Sub-linear scaling: ~15% overhead per additional concurrent task
        eff = 1.0 if workers == 1 else workers * (1 / (1 + 0.15 * (workers - 1)))

        total_time_s = (total_files * avg_file_total_s) / eff
        total_time_h = total_time_s / 3600
        audio_per_hour = total_hours / total_time_h if total_time_h > 0 else 0

        print(
            f'  {label:<40} {workers:>8} {_fmt_duration(total_time_s):>12} {audio_per_hour:>9.1f}x'
        )

    # ── Recommendations ──────────────────────────────────────────────────
    print('\nRECOMMENDATIONS')
    print('-' * 80)
    print('  1. Run benchmark_e2e.py on a 3hr file to get exact realtime factor:')
    print('     python scripts/benchmark_e2e.py --file-uuid <UUID> --detailed')
    print('  2. Run benchmark_parallel.py to test concurrency limits:')
    print('     python scripts/benchmark_parallel.py --batches 1,2,3,4 --min-duration 10800')
    print('  3. Use measured data for accurate projection:')
    print('     python scripts/benchmark_projection.py --realtime-factor <measured>')
    print('=' * 80)


if __name__ == '__main__':
    main()
