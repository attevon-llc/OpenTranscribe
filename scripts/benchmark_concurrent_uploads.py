#!/usr/bin/env python3
"""Concurrent-upload contention benchmark.

Launches N simultaneous uploads of the same fixture file, polls each to
completion, then reads the ``file_pipeline_timing`` table to produce p50/p95
per stage. Complements ``scripts/benchmark_parallel.py`` (which hits the
reprocess endpoint only) by exercising the full HTTP ingress path under
load — the part the plan calls out as the "dark blind spot".

Requires:
    - ENABLE_BENCHMARK_TIMING=true in .env
    - Backend running at BACKEND_URL
    - file_pipeline_timing table exists (Alembic migration v360+)

Usage:
    BENCHMARK_EMAIL=admin@example.com BENCHMARK_PASSWORD=password \\
        python scripts/benchmark_concurrent_uploads.py \\
            --fixture-file /path/to/sample.mp4 --n 8
"""

from __future__ import annotations

import argparse
import csv
import os
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# Reuse helpers from benchmark_e2e so we stay in lockstep with its upload path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from benchmark_e2e import (  # noqa: E402
    DEFAULT_BACKEND_URL,
    DEFAULT_REDIS_URL,
    POLL_TIMEOUT,
    _fmt_duration,
    get_auth_token,
    poll_task_completion,
    upload_file_via_api,
)

DB_CONTAINER = 'opentranscribe-postgres'
DB_USER = 'postgres'
DB_NAME = 'opentranscribe'


# ---------------------------------------------------------------------------
# DB helpers — read the file_pipeline_timing table via docker exec
# ---------------------------------------------------------------------------
def _db_query(sql: str) -> list[list[str]]:
    try:
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
            timeout=30,
        )
        if result.returncode != 0:
            return []
        return [line.split('\t') for line in result.stdout.strip().split('\n') if line.strip()]
    except Exception:
        return []


def fetch_timing_rows(task_ids: list[str]) -> list[dict]:
    """Return rows from ``file_pipeline_timing`` for the given task_ids."""
    if not task_ids:
        return []
    # Quote each task_id safely for inlining into the SQL (they're UUIDs, so
    # the risk surface is minimal, but we belt-and-suspenders anyway).
    safe = ','.join(f"'{t.replace(chr(39), chr(39) * 2)}'" for t in task_ids)
    columns = [
        'task_id',
        'http_request_received_ms',
        'http_response_end_ms',
        'minio_put_start_ms',
        'minio_put_end_ms',
        'thumbnail_start_ms',
        'thumbnail_end_ms',
        'db_commit_start_ms',
        'db_commit_end_ms',
        'dispatch_timestamp_ms',
        'preprocess_end_ms',
        'gpu_received_ms',
        'gpu_end_ms',
        'postprocess_received_ms',
        'postprocess_end_ms',
        'completion_notified_ms',
        'search_index_end_ms',
        'waveform_end_ms',
        'user_perceived_duration_ms',
        'fully_indexed_duration_ms',
        'file_size_bytes',
        'audio_duration_s',
        'concurrent_files_at_dispatch',
        'cpu_worker_cold',
        'gpu_worker_cold',
    ]
    sql = f'SELECT {",".join(columns)} FROM file_pipeline_timing WHERE task_id IN ({safe})'
    rows: list[dict] = []
    for row in _db_query(sql):
        if len(row) < len(columns):
            continue
        entry = dict(zip(columns, row, strict=False))
        rows.append(entry)
    return rows


def _to_float(x: str) -> float | None:
    try:
        return float(x) if x else None
    except ValueError:
        return None


def _pstat(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    k = int(round((p / 100.0) * (len(values) - 1)))
    return values[max(0, min(k, len(values) - 1))]


# ---------------------------------------------------------------------------
# Upload worker
# ---------------------------------------------------------------------------
def _one_upload(
    backend_url: str,
    token: str,
    fixture_path: str,
    verify: bool,
    poll_timeout: int,
) -> dict:
    """Upload, wait for completion, return summary including file_uuid."""
    started = time.time()
    try:
        _, upload_resp, _ = upload_file_via_api(backend_url, token, fixture_path, verify=verify)
    except Exception as e:
        return {
            'status': 'upload_failed',
            'error': str(e),
            'started': started,
        }
    file_uuid = str(upload_resp.get('uuid') or upload_resp.get('id') or '')
    if not file_uuid:
        return {'status': 'no_uuid', 'started': started}

    ok = poll_task_completion(
        backend_url,
        file_uuid,
        token,
        timeout=poll_timeout,
        verify=verify,
    )
    elapsed = time.time() - started
    return {
        'status': 'ok' if ok else 'timeout_or_error',
        'file_uuid': file_uuid,
        'started': started,
        'elapsed_s': elapsed,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Concurrent-upload contention benchmark',
    )
    parser.add_argument('--fixture-file', required=True, help='Path to the media fixture to upload')
    parser.add_argument('--n', type=int, default=8, help='Number of concurrent uploads (default 8)')
    parser.add_argument('--backend-url', default=DEFAULT_BACKEND_URL)
    parser.add_argument('--redis-url', default=DEFAULT_REDIS_URL)
    parser.add_argument(
        '--timeout',
        type=int,
        default=POLL_TIMEOUT,
        help='Max seconds to wait per upload (default 3600)',
    )
    parser.add_argument('--output', default='benchmark_concurrent_uploads.csv')
    parser.add_argument('--no-verify', action='store_true')
    args = parser.parse_args()

    email = os.environ.get('BENCHMARK_EMAIL', 'admin@example.com')
    password = os.environ.get('BENCHMARK_PASSWORD', 'password')
    verify = not args.no_verify

    print(f'Authenticating to {args.backend_url}...')
    token = get_auth_token(args.backend_url, email, password, verify=verify)

    print(f'Launching {args.n} concurrent uploads of {args.fixture_file}...')
    batch_start = time.time()
    with ThreadPoolExecutor(max_workers=args.n) as pool:
        futures = [
            pool.submit(
                _one_upload,
                args.backend_url,
                token,
                args.fixture_file,
                verify,
                args.timeout,
            )
            for _ in range(args.n)
        ]
        summaries = []
        for f in as_completed(futures):
            s = f.result()
            summaries.append(s)
            tag = s.get('file_uuid', s.get('status', ''))
            elapsed = s.get('elapsed_s', 0)
            print(f'  worker done: {tag} in {_fmt_duration(elapsed)}')
    batch_elapsed = time.time() - batch_start

    successes = [s for s in summaries if s.get('status') == 'ok']
    print(f'\nBatch complete: {len(successes)}/{args.n} ok in {_fmt_duration(batch_elapsed)}')

    # Give the postprocess flush a beat to write the DB rows
    time.sleep(5)

    # Look up task_ids via the same /api/files path so we can query the
    # timing table without requiring a docker exec to fetch active_task_id.
    uuids = [s['file_uuid'] for s in successes if s.get('file_uuid')]
    # Use DB shortcut: task_id == active_task_id on the media_file row.
    task_map: dict[str, str] = {}
    for uuid in uuids:
        rows = _db_query(f"SELECT active_task_id FROM media_file WHERE uuid = '{uuid}'")
        if rows and rows[0][0].strip():
            task_map[uuid] = rows[0][0].strip()

    print(f'Resolved {len(task_map)}/{len(uuids)} task_ids')
    timing_rows = fetch_timing_rows(list(task_map.values()))
    print(f'Fetched {len(timing_rows)} rows from file_pipeline_timing')

    # Write raw CSV
    if timing_rows:
        with open(args.output, 'w', newline='') as fp:
            writer = csv.DictWriter(fp, fieldnames=sorted(timing_rows[0].keys()))
            writer.writeheader()
            for row in timing_rows:
                writer.writerow(row)
        print(f'CSV written to {args.output}')

    # Aggregate per-stage durations
    stage_defs = [
        ('http_total', 'http_request_received_ms', 'http_response_end_ms'),
        ('minio_put', 'minio_put_start_ms', 'minio_put_end_ms'),
        ('thumbnail', 'thumbnail_start_ms', 'thumbnail_end_ms'),
        ('db_commit', 'db_commit_start_ms', 'db_commit_end_ms'),
        ('preprocess', 'dispatch_timestamp_ms', 'preprocess_end_ms'),
        ('queue_cpu_to_gpu', 'preprocess_end_ms', 'gpu_received_ms'),
        ('gpu', 'gpu_received_ms', 'gpu_end_ms'),
        ('queue_gpu_to_post', 'gpu_end_ms', 'postprocess_received_ms'),
        ('postprocess', 'postprocess_received_ms', 'postprocess_end_ms'),
        ('user_perceived', 'http_request_received_ms', 'completion_notified_ms'),
        ('search_index_tail', 'completion_notified_ms', 'search_index_end_ms'),
    ]

    def _extract(row: dict, start_key: str, end_key: str) -> float | None:
        s = _to_float(row.get(start_key, ''))
        e = _to_float(row.get(end_key, ''))
        if s is None or e is None or e < s:
            return None
        return (e - s) / 1000.0

    print('\n' + '=' * 72)
    print(f'PER-STAGE STATISTICS (n={len(timing_rows)})')
    print('=' * 72)
    print(f'{"stage":<20} {"p50":>9} {"p95":>9} {"min":>9} {"max":>9} {"mean":>9}')
    print('-' * 72)
    for name, s_key, e_key in stage_defs:
        values = [v for v in (_extract(row, s_key, e_key) for row in timing_rows) if v is not None]
        if not values:
            print(f'{name:<20} {"n/a":>9} {"n/a":>9} {"n/a":>9} {"n/a":>9} {"n/a":>9}')
            continue
        mean = statistics.mean(values)
        print(
            f'{name:<20} '
            f'{_pstat(values, 50):>8.2f}s '
            f'{_pstat(values, 95):>8.2f}s '
            f'{min(values):>8.2f}s '
            f'{max(values):>8.2f}s '
            f'{mean:>8.2f}s'
        )
    print('=' * 72)

    cold_count = sum(1 for row in timing_rows if row.get('gpu_worker_cold') == 'true')
    if cold_count:
        print(
            f'\nNote: {cold_count} of {len(timing_rows)} tasks were the first on '
            f'their worker process (cold start).'
        )


if __name__ == '__main__':
    main()
