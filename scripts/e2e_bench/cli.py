"""Argument parsing and the iteration loop of the end-to-end benchmark."""

import argparse
import csv
import logging
import os
import sys
import time

import redis
import requests
from bench_common import load_redis_url

from e2e_bench.backend_api import (
    get_auth_token,
    get_file_metadata,
    get_task_id_for_file_via_api,
    poll_task_completion,
    trigger_reprocess,
    upload_file_via_api,
)
from e2e_bench.config import (
    DEFAULT_BACKEND_URL,
    INTER_ITERATION_WAIT,
    POLL_TIMEOUT,
)
from e2e_bench.db import find_benchmark_task_id, get_task_id_for_file
from e2e_bench.formatting import fmt_duration, fmt_size
from e2e_bench.reports import print_detailed_report, print_summary, print_vram_summary
from e2e_bench.stages import calculate_stages, collect_benchmark_data, collect_vram_data

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description='E2E transcription benchmark',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Reprocess an existing file (default, no upload latency measured)
  python scripts/benchmark_e2e.py --file-uuid abc123

  # Full upload-to-done timing of a fresh file (legacy HTTP flow)
  python scripts/benchmark_e2e.py --mode upload --fixture-file /path/to/sample.mp4

  # Detailed report with VRAM breakdown and realtime factor
  python scripts/benchmark_e2e.py --file-uuid abc123 --detailed

  # 60-minute timeout for very long files
  python scripts/benchmark_e2e.py --file-uuid abc123 --timeout 3600 --detailed
        """,
    )
    parser.add_argument(
        '--mode',
        choices=['reprocess', 'upload'],
        default='reprocess',
        help='reprocess: re-run pipeline on existing file (default). '
        'upload: POST a fixture to /api/files end-to-end.',
    )
    parser.add_argument(
        '--file-uuid', help='UUID of file to benchmark (required for --mode reprocess)'
    )
    parser.add_argument(
        '--fixture-file', help='Path to a file to upload (required for --mode upload)'
    )
    parser.add_argument(
        '--flow',
        choices=['legacy', 'presigned'],
        default='legacy',
        help='Upload flow to use when --mode upload. legacy: POST to '
        '/api/files. presigned: new direct-to-MinIO flow (future).',
    )
    parser.add_argument(
        '--iterations',
        type=int,
        default=3,
        help='Number of iterations (default: 3). In upload mode each '
        'iteration creates a new MediaFile row.',
    )
    parser.add_argument('--output', default='benchmark_results.csv', help='Output CSV path')
    parser.add_argument('--backend-url', default=DEFAULT_BACKEND_URL, help='Backend URL')
    parser.add_argument(
        '--redis-url',
        default=None,
        help='Redis URL (default: BENCHMARK_REDIS_URL, else the resolved stack)',
    )
    parser.add_argument(
        '--timeout',
        type=int,
        default=POLL_TIMEOUT,
        help=f'Max seconds to wait for completion (default: {POLL_TIMEOUT})',
    )
    parser.add_argument(
        '--detailed',
        action='store_true',
        help='Print detailed report with VRAM breakdown and realtime factor',
    )
    parser.add_argument(
        '--no-verify',
        action='store_true',
        help='Disable TLS certificate verification (for self-signed certs)',
    )
    args = parser.parse_args()

    if args.mode == 'reprocess' and not args.file_uuid:
        parser.error('--file-uuid is required when --mode reprocess')
    if args.mode == 'upload' and not args.fixture_file:
        parser.error('--fixture-file is required when --mode upload')
    if args.mode == 'upload' and args.flow == 'presigned':
        parser.error('presigned flow is not yet implemented (Phase 2 PR)')

    email = os.environ.get('BENCHMARK_EMAIL', 'admin@example.com')
    password = os.environ.get('BENCHMARK_PASSWORD', 'password')
    verify = not args.no_verify

    print(f'Authenticating to {args.backend_url}...')
    token = get_auth_token(args.backend_url, email, password, verify=verify)

    current_file_uuid = args.file_uuid
    file_meta: dict = {}

    if args.mode == 'reprocess':
        # Get file metadata before starting
        print(f'Fetching file metadata for {current_file_uuid}...')
        file_meta = get_file_metadata(args.backend_url, current_file_uuid, token, verify=verify)
        if file_meta:
            dur = file_meta.get('duration', 0)
            print(f'  File: {file_meta.get("filename", "unknown")}')
            print(f'  Duration: {fmt_duration(dur)} ({dur:.0f}s)')
            print(f'  Size: {fmt_size(file_meta.get("file_size", 0))}')
        else:
            print('  WARNING: Could not fetch file metadata', file=sys.stderr)

    # Resolved only when the flag is absent: an explicit URL must work with no stack running.
    r = redis.from_url(args.redis_url or load_redis_url(), decode_responses=False)

    all_results: list[dict] = []
    all_vram: list[dict] = []

    for i in range(args.iterations):
        if i > 0:
            print(f'  Waiting {INTER_ITERATION_WAIT}s between iterations...')
            time.sleep(INTER_ITERATION_WAIT)

        print(f'\n--- Iteration {i + 1}/{args.iterations} ---')

        if args.mode == 'upload':
            print(f'  Uploading fixture: {args.fixture_file}')
            dispatch_time = time.time()
            try:
                client_hash_elapsed, upload_resp, put_elapsed = upload_file_via_api(
                    args.backend_url, token, args.fixture_file, verify=verify
                )
            except (requests.RequestException, OSError) as e:
                logger.exception('upload failed')
                print(f'  Upload failed: {e}', file=sys.stderr)
                continue
            current_file_uuid = str(upload_resp.get('uuid') or upload_resp.get('id') or '')
            if not current_file_uuid:
                print('  Upload returned no UUID', file=sys.stderr)
                continue
            print(
                f'  Uploaded file {current_file_uuid} '
                f'(hash={client_hash_elapsed:.1f}s, put={put_elapsed:.1f}s)'
            )
            # Brief delay to let the dispatch write hit Redis
            time.sleep(1.5)
            resolved_id = find_benchmark_task_id(
                r, dispatch_time, match_field='http_request_received'
            )
            if not resolved_id:
                resolved_id = find_benchmark_task_id(r, dispatch_time)
            if not resolved_id:
                resolved_id = get_task_id_for_file_via_api(
                    args.backend_url, current_file_uuid, token, verify=verify
                )
            task_id = resolved_id
        else:
            print(f'  Triggering reprocess for {current_file_uuid}...')
            dispatch_time = time.time()
            trigger_reprocess(args.backend_url, current_file_uuid, token, verify=verify)
            task_id = get_task_id_for_file(current_file_uuid)

        if task_id:
            print(f'  Task ID: {task_id}')

        print('  Waiting for completion...')
        wall_start = dispatch_time  # measure from dispatch/upload-start, not poll-start
        success = poll_task_completion(
            args.backend_url,
            current_file_uuid,
            token,
            timeout=args.timeout,
            verify=verify,
        )
        wall_elapsed = time.time() - wall_start

        if not success:
            print(f'  Skipping iteration {i + 1} (failed/timeout)')
            continue

        print(f'  Completed in {fmt_duration(wall_elapsed)}')

        # Brief delay for Redis data propagation
        time.sleep(3)

        # Resolve benchmark task_id if not already known
        if not task_id:
            resolved_id = find_benchmark_task_id(r, dispatch_time)
            if resolved_id:
                task_id = resolved_id
            else:
                print('  WARNING: Could not resolve task_id from Redis or DB')
                continue

        bench_data = collect_benchmark_data(r, task_id)
        vram_data = collect_vram_data(r, task_id)
        stages = calculate_stages(bench_data)

        result = {
            'iteration': i + 1,
            'task_id': task_id,
            'file_uuid': current_file_uuid,
            'wall_elapsed': wall_elapsed,
            'raw_timestamps': bench_data,
            'stages': stages,
        }
        all_results.append(result)
        all_vram.append(vram_data)

        # Quick per-iteration feedback
        gpu_dur = stages.get('gpu_duration', 0)
        pre_dur = stages.get('preprocess_duration', 0)
        http_dur = stages.get('http_total_duration')
        user_perc = stages.get('user_perceived_duration')
        if http_dur is not None:
            perc_fmt = f'{user_perc:.1f}s' if user_perc else 'n/a'
            print(
                f'  HTTP: {http_dur:.1f}s | Preprocess: {pre_dur:.1f}s | '
                f'GPU: {gpu_dur:.1f}s | User-perceived: {perc_fmt}'
            )
        else:
            print(
                f'  Preprocess: {pre_dur:.1f}s | GPU: {gpu_dur:.1f}s | '
                f'Gaps: {stages.get("preprocess_to_gpu_gap", 0):.1f}s + '
                f'{stages.get("gpu_to_postprocess_gap", 0):.1f}s'
            )
        if vram_data:
            print(f'  VRAM profile: {len(vram_data.get("steps", []))} steps captured')
        else:
            print('  VRAM profile: None (enable ENABLE_VRAM_PROFILING=true)')

    if not all_results:
        print('ERROR: no iteration completed', file=sys.stderr)
        sys.exit(1)

    # Refresh file metadata after processing (to get segment/speaker counts)
    last_uuid = all_results[-1]['file_uuid']
    file_meta = get_file_metadata(args.backend_url, last_uuid, token, verify=verify)

    stage_keys = set()
    for r_item in all_results:
        stage_keys.update(r_item.get('stages', {}).keys())
    stage_keys = sorted(stage_keys)

    with open(args.output, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['iteration', 'task_id', 'file_uuid', 'wall_elapsed'] + stage_keys)
        for r_item in all_results:
            row = [
                r_item['iteration'],
                r_item['task_id'],
                r_item.get('file_uuid', ''),
                f'{r_item["wall_elapsed"]:.3f}',
            ]
            row += [r_item['stages'].get(k, '') for k in stage_keys]
            writer.writerow(row)

    print(f'\nCSV written to {args.output}')

    if args.detailed:
        print_detailed_report(all_results, all_vram, file_meta, last_uuid)
    else:
        print_summary(all_results)
        print_vram_summary(all_vram)
