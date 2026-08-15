#!/usr/bin/env python3
"""Real-user upload benchmark across a fixture matrix.

Drives actual HTTP uploads through the same code path a browser would
(POST /api/files multipart/form-data with SHA-256 hash header) for every
fixture in a directory, polls each to completion, then reads
file_pipeline_timing to print the full stage breakdown per file.

Unlike benchmark_e2e.py (reprocess only) and
benchmark_concurrent_uploads.py (N copies of one file), this one walks
a variety of sizes / durations / formats in sequence so we can see how
the per-stage wall-clock scales. Exactly what "click submit" feels like
at different file sizes.

Requires:
    - ENABLE_BENCHMARK_TIMING=true in .env
    - Backend reachable at --backend-url (default http://localhost:5174)
    - psql accessible via docker exec (for timing row queries)
    - Fixtures under --fixtures-dir (defaults to /tmp/benchmark_fixtures)

Usage:
    BENCHMARK_EMAIL=admin@example.com BENCHMARK_PASSWORD=password \\
        python scripts/benchmark_upload_matrix.py \\
            --fixtures-dir /tmp/benchmark_fixtures [--output results.csv]
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path

# Reuse the proven helpers so we behave exactly like benchmark_e2e.py
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))
# E402: the two imports below resolve only after the sys.path.insert above.
from benchmark_concurrent_uploads import (  # noqa: E402
    _db_query,
    fetch_timing_rows,
)
from benchmark_e2e import (  # noqa: E402
    DEFAULT_BACKEND_URL,
    get_auth_token,
    poll_task_completion,
    upload_file_via_api,  # noqa: E402
)


def resolve_task_id_from_db(file_uuid: str) -> str:
    """Find the most recent Task row for ``file_uuid``.

    Needed because the API's ``active_task_id`` is cleared once the file
    reaches COMPLETED, so the upload benchmark has to reach around it via
    Postgres to get the timing key. Picks the newest task (ORDER BY
    created_at DESC) so reprocess-friendly.
    """
    sql = (
        'SELECT t.id FROM task t JOIN media_file m ON m.id = t.media_file_id '
        f"WHERE m.uuid = '{file_uuid}' ORDER BY t.created_at DESC LIMIT 1"
    )
    rows = _db_query(sql)
    if rows and rows[0]:
        return str(rows[0][0])
    return ''


# Explicit column order so the table is predictable and easy to eyeball.
# Durations are printed in seconds (from epoch-ms BIGINT columns).
_STAGES: list[tuple[str, str, str]] = [
    # (label, start_col, end_col)
    ('http_validate', 'http_request_received_ms', 'http_validation_end_ms'),
    ('http_read', 'http_validation_end_ms', 'http_read_complete_ms'),
    ('imohash', 'imohash_start_ms', 'imohash_end_ms'),
    ('minio_put', 'minio_put_start_ms', 'minio_put_end_ms'),
    ('db_commit', 'db_commit_start_ms', 'db_commit_end_ms'),
    ('http_done', 'http_request_received_ms', 'http_response_end_ms'),
    ('queue_wait', 'http_response_end_ms', 'preprocess_task_prerun_ms'),
    ('preprocess', 'preprocess_task_prerun_ms', 'preprocess_end_ms'),
    ('  ffmpeg', 'ffmpeg_start_ms', 'ffmpeg_end_ms'),
    ('  metadata', 'metadata_start_ms', 'metadata_end_ms'),
    ('  temp_upload', 'temp_upload_start_ms', 'temp_upload_end_ms'),
    ('gpu_pickup', 'preprocess_end_ms', 'gpu_task_prerun_ms'),
    ('  audio_load', 'gpu_audio_load_start_ms', 'gpu_audio_load_end_ms'),
    ('gpu_total', 'gpu_received_ms', 'gpu_end_ms'),
    ('post_pickup', 'gpu_end_ms', 'postprocess_task_prerun_ms'),
    ('postprocess', 'postprocess_task_prerun_ms', 'postprocess_end_ms'),
    ('search_index', 'search_index_chunks_start_ms', 'search_index_chunks_end_ms'),
    ('waveform', 'waveform_start_ms', 'waveform_end_ms'),
    # Derived
    ('USER DONE', 'http_request_received_ms', 'completion_notified_ms'),
    ('FULLY INDEXED', 'http_request_received_ms', 'search_index_chunks_end_ms'),
]


def _fmt_ms(a: str | None, b: str | None) -> str:
    try:
        if not a or not b:
            return '-'
        delta = (int(b) - int(a)) / 1000.0
        if delta < 0:
            return '-'
        return f'{delta:7.3f}'
    except (TypeError, ValueError):
        return '-'


def _fetch_with_retry(task_id: str, tries: int = 6, pause: float = 1.0) -> dict | None:
    """Pull the timing row; retry briefly because the flush happens at
    postprocess_end — just after the file status flips to COMPLETED."""
    last: list[dict] = []
    for _ in range(tries):
        last = fetch_timing_rows([task_id])
        if last and last[0].get('http_request_received_ms'):
            return last[0]
        time.sleep(pause)
    return last[0] if last else None


def _print_header(fixtures: list[tuple[str, int, float]]) -> None:
    print()
    print('Fixture matrix:')
    print(f'  {"Name":<24} {"Size":>9}  {"Duration":>10}')
    for name, size, dur in fixtures:
        size_mb = size / (1024 * 1024)
        print(f'  {name:<24} {size_mb:>7.1f}MB  {dur:>9.1f}s')
    print()


def _print_row_header(fixture_names: list[str]) -> None:
    hdr = ' Stage              '
    for name in fixture_names:
        hdr += f'{name[:14]:>16}'
    print(hdr)
    print('-' * len(hdr))


def _print_stage_row(
    label: str,
    rows_by_fixture: dict[str, dict],
    fixture_names: list[str],
    start_col: str,
    end_col: str,
) -> None:
    line = f' {label:<18}'
    for name in fixture_names:
        row = rows_by_fixture.get(name)
        if not row:
            line += f'{"n/a":>16}'
        else:
            line += f'{_fmt_ms(row.get(start_col), row.get(end_col)):>16}'
    print(line)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixtures-dir', default='/tmp/benchmark_fixtures')
    parser.add_argument('--backend-url', default=DEFAULT_BACKEND_URL)
    parser.add_argument('--output', default=None, help='Optional CSV output path')
    parser.add_argument(
        '--timeout', type=int, default=1800, help='Per-upload poll timeout, seconds (default 1800)'
    )
    parser.add_argument(
        '--no-verify', action='store_true', help='Skip TLS cert verification for the backend'
    )
    args = parser.parse_args()

    verify = not args.no_verify
    email = os.environ.get('BENCHMARK_EMAIL', 'admin@example.com')
    password = os.environ.get('BENCHMARK_PASSWORD', 'password')

    fixtures_dir = Path(args.fixtures_dir)
    if not fixtures_dir.is_dir():
        print(f'Fixtures directory not found: {fixtures_dir}', file=sys.stderr)
        sys.exit(1)

    # Build fixture list sorted by size ASC so small files run first.
    fixture_files = sorted(
        (p for p in fixtures_dir.iterdir() if p.is_file()),
        key=lambda p: p.stat().st_size,
    )
    if not fixture_files:
        print(f'No files in {fixtures_dir}', file=sys.stderr)
        sys.exit(1)

    # Pull duration metadata via ffprobe where available (pure cosmetic).
    fixture_summaries: list[tuple[str, int, float]] = []
    for fp in fixture_files:
        size = fp.stat().st_size
        duration = 0.0
        try:
            import subprocess

            out = subprocess.check_output(
                [
                    'ffprobe',
                    '-v',
                    'error',
                    '-show_entries',
                    'format=duration',
                    '-of',
                    'default=noprint_wrappers=1:nokey=1',
                    str(fp),
                ],
                text=True,
                timeout=10,
            )
            duration = float(out.strip())
        except Exception:
            pass
        fixture_summaries.append((fp.name, size, duration))

    _print_header(fixture_summaries)

    print(f'Authenticating to {args.backend_url}...')
    token = get_auth_token(args.backend_url, email, password, verify=verify)

    # Upload one-by-one so stage timings are clean (no cross-file contention).
    # A separate concurrent benchmark lives in benchmark_concurrent_uploads.py.
    rows_by_fixture: dict[str, dict] = {}
    upload_metadata: dict[str, dict] = {}

    for fp in fixture_files:
        name = fp.name
        print(f'\n▶ Uploading {name} ({fp.stat().st_size / 1024 / 1024:.1f} MB)...')
        t0 = time.time()
        try:
            hash_s, resp, put_s = upload_file_via_api(
                args.backend_url, token, str(fp), verify=verify
            )
        except Exception as e:
            print(f'  ✗ Upload failed: {e}')
            continue

        file_uuid = str(resp.get('uuid') or resp.get('id') or '')
        if not file_uuid:
            print(f'  ✗ No UUID in response: {resp}')
            continue

        print(f'  ✓ HTTP {put_s:.2f}s (hash {hash_s:.2f}s), UUID {file_uuid[:8]}…')

        # Wait for pipeline to finish.
        ok = poll_task_completion(
            args.backend_url,
            file_uuid,
            token,
            timeout=args.timeout,
            verify=verify,
        )
        wall = time.time() - t0
        if not ok:
            print(f'  ✗ Did not complete inside {args.timeout}s')
            continue

        # Resolve the application task_id via Postgres. We can't use the
        # API's ``active_task_id`` because it's cleared once the file
        # reaches COMPLETED; the Task row is the durable record.
        task_id = resolve_task_id_from_db(file_uuid)
        if not task_id:
            print('  ✗ Could not resolve task_id for this file_uuid')
            continue
        print(f'  ✓ Completed in {wall:.2f}s (task_id {task_id[:8]}…)')

        row = _fetch_with_retry(task_id)
        if not row:
            print('  ✗ No timing row in file_pipeline_timing')
            continue
        rows_by_fixture[name] = row
        upload_metadata[name] = {
            'uuid': file_uuid,
            'task_id': task_id,
            'wall_s': wall,
            'client_hash_s': hash_s,
            'client_put_s': put_s,
        }

    if not rows_by_fixture:
        print('\nNo timing rows collected. Aborting table render.', file=sys.stderr)
        sys.exit(1)

    # Table render
    print('\n' + '=' * 80)
    print('Per-stage wall-clock (seconds, higher = slower):')
    print('=' * 80)
    names_in_order = [n for n, _, _ in fixture_summaries if n in rows_by_fixture]
    _print_row_header(names_in_order)
    for label, start_col, end_col in _STAGES:
        _print_stage_row(label, rows_by_fixture, names_in_order, start_col, end_col)

    # Client-side totals (what the user's browser felt)
    print()
    print('Client-observed totals:')
    for name in names_in_order:
        meta = upload_metadata[name]
        print(
            f'  {name:<24} client_hash={meta["client_hash_s"]:>5.2f}s  '
            f'client_put={meta["client_put_s"]:>6.2f}s  '
            f'wall_total={meta["wall_s"]:>6.2f}s'
        )

    # CSV output (flat, one row per fixture)
    if args.output:
        with open(args.output, 'w', newline='') as fp_csv:
            writer = csv.writer(fp_csv)
            headers = [
                'fixture',
                'size_bytes',
                'duration_s',
                'task_id',
                'wall_total_s',
                'client_hash_s',
                'client_put_s',
            ]
            for label, *_ in _STAGES:
                headers.append(label.strip().replace(' ', '_') + '_s')
            writer.writerow(headers)
            for name, size, dur in fixture_summaries:
                if name not in rows_by_fixture:
                    continue
                row = rows_by_fixture[name]
                meta = upload_metadata[name]
                line = [
                    name,
                    size,
                    dur,
                    meta['task_id'],
                    f'{meta["wall_s"]:.3f}',
                    f'{meta["client_hash_s"]:.3f}',
                    f'{meta["client_put_s"]:.3f}',
                ]
                for _, s_col, e_col in _STAGES:
                    line.append(_fmt_ms(row.get(s_col), row.get(e_col)).strip())
                writer.writerow(line)
        print(f'\nCSV written to {args.output}')


if __name__ == '__main__':
    main()
