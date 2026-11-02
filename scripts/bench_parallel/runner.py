"""Run one parallel batch: dispatch, poll to completion, collect Redis stage timings."""

from __future__ import annotations

import json
import time
from pathlib import Path

import redis

from bench_parallel.backend_api import (
    get_active_task_id,
    get_file_status,
    get_valid_token,
    trigger_reprocess,
    upload_file,
)
from bench_parallel.config import POLL_INTERVAL, POLL_TIMEOUT
from bench_parallel.console_reports import print_batch_summary
from bench_parallel.formatting import fmt_duration, now_ts
from bench_parallel.models import BatchResult, FileResult
from bench_parallel.monitors import DockerStatsMonitor, VramMonitor


def collect_benchmark_stages(r: redis.Redis, task_id: str) -> dict:
    """Collect pipeline stage timing from Redis benchmark hash."""
    key = f'benchmark:{task_id}'
    raw = r.hgetall(key)
    if not raw:
        return {}
    # The hash mixes float timestamps with non-numeric flags (e.g. 'true');
    # keep only the float-convertible entries.
    data = {}
    for k, v in raw.items():
        try:
            data[k.decode()] = float(v.decode())
        except (ValueError, AttributeError):
            continue
    stages = {}
    dispatch = data.get('dispatch_timestamp')
    pre_end = data.get('preprocess_end')
    gpu_recv = data.get('gpu_received')
    gpu_end = data.get('gpu_end')
    post_recv = data.get('postprocess_received')

    if dispatch and pre_end:
        stages['1_preprocess'] = round(pre_end - dispatch, 3)
    if pre_end and gpu_recv:
        stages['2_cpu_to_gpu_queue'] = round(gpu_recv - pre_end, 3)
    if gpu_recv and gpu_end:
        stages['3_gpu_transcribe'] = round(gpu_end - gpu_recv, 3)
    if gpu_end and post_recv:
        stages['4_gpu_to_post_queue'] = round(post_recv - gpu_end, 3)
    if dispatch and gpu_end:
        stages['total_to_gpu_end'] = round(gpu_end - dispatch, 3)
    if dispatch and post_recv:
        stages['total_to_postprocess'] = round(post_recv - dispatch, 3)
    # Store raw timestamps for cross-file analysis
    stages['_dispatch'] = dispatch
    stages['_gpu_recv'] = gpu_recv
    stages['_gpu_end'] = gpu_end
    return stages


def collect_vram_profile(r: redis.Redis, task_id: str) -> dict | None:
    """Collect VRAM profiler data from Redis."""
    key = f'gpu:profile:{task_id}'
    raw = r.get(key)
    if raw:
        return json.loads(raw)
    return None


# ---------------------------------------------------------------------------
# Core benchmark logic
# ---------------------------------------------------------------------------
def run_batch(
    token: str,
    r: redis.Redis,
    files: list[dict],
    batch_size: int,
    gpu_id: int,
    upload_mode: bool = False,
    audio_dir: str = '',
    stats_containers: list[str] | None = None,
) -> BatchResult:
    """Run a single batch of N files and collect all metrics."""
    batch_files = files[:batch_size]
    result = BatchResult(batch_size=batch_size)

    print(f'\n{"=" * 80}')
    print(f'  BATCH SIZE: {batch_size} files')
    print(f'  Files: {", ".join(f["filename"][:40] for f in batch_files)}')
    print(f'{"=" * 80}')

    # Initialize file results
    for f in batch_files:
        result.file_results.append(
            FileResult(
                uuid=f['uuid'],
                filename=f['filename'],
                duration_s=f['duration'],
            )
        )

    # Start VRAM + CPU/RAM monitoring
    vram_monitor = VramMonitor(gpu_id=gpu_id)
    vram_monitor.start()
    stats_monitor = DockerStatsMonitor(stats_containers or [])
    stats_monitor.start()

    # Trigger all files: upload fresh (real user flow) or reprocess existing.
    result.wall_start = time.time()
    token = get_valid_token()
    action = 'upload' if upload_mode else 'reprocess'
    print(f'\n  [{now_ts()}] Triggering {batch_size} {action} requests...')
    for fr in result.file_results:
        fr.wall_start = time.time()
        if upload_mode:
            new_uuid = upload_file(token, str(Path(audio_dir) / fr.filename))
            if new_uuid:
                fr.uuid = new_uuid  # fresh uuid drives polling + task-id lookup
                fr.status = 'dispatched'
            else:
                fr.status = 'dispatch_failed'
        else:
            success = trigger_reprocess(token, fr.uuid)
            fr.status = 'dispatched' if success else 'dispatch_failed'
        # Small stagger to avoid overwhelming the API
        time.sleep(0.3)

    # Collect task IDs from DB (wait a moment for DB to be updated)
    time.sleep(3)
    for fr in result.file_results:
        if fr.status == 'dispatched':
            fr.task_id = get_active_task_id(fr.uuid)
            if fr.task_id:
                print(f'    {fr.filename[:50]}: task_id={fr.task_id[:12]}...')
            else:
                print(f'    {fr.filename[:50]}: WARNING - no task_id found')

    # Poll until all files complete
    print(f'\n  [{now_ts()}] Polling for completion (timeout: {POLL_TIMEOUT / 60:.0f}min)...')
    pending = {fr.uuid for fr in result.file_results if fr.status == 'dispatched'}
    completed = set()
    failed = set()
    last_status_print = time.time()

    deadline = time.time() + POLL_TIMEOUT
    while pending and time.time() < deadline:
        token = get_valid_token()
        for uuid in list(pending):
            status = get_file_status(token, uuid)
            if status == 'completed':
                pending.discard(uuid)
                completed.add(uuid)
                # Record wall_end for this file
                for fr in result.file_results:
                    if fr.uuid == uuid:
                        fr.wall_end = time.time()
                        fr.wall_elapsed = fr.wall_end - fr.wall_start
                        fr.status = 'completed'
                        print(
                            f'    [{now_ts()}] DONE: {fr.filename[:45]} '
                            f'({fmt_duration(fr.wall_elapsed)}) '
                            f'[{len(completed)}/{batch_size}]'
                        )
            elif status == 'error':
                pending.discard(uuid)
                failed.add(uuid)
                for fr in result.file_results:
                    if fr.uuid == uuid:
                        fr.wall_end = time.time()
                        fr.wall_elapsed = fr.wall_end - fr.wall_start
                        fr.status = 'error'
                        print(
                            f'    [{now_ts()}] ERROR: {fr.filename[:45]} '
                            f'({fmt_duration(fr.wall_elapsed)})'
                        )
            # Small delay between per-file status checks
            time.sleep(0.1)

        if pending:
            # Print status every 60 seconds
            if time.time() - last_status_print > 60:
                elapsed = time.time() - result.wall_start
                print(
                    f'    [{now_ts()}] {len(completed)} done, {len(pending)} pending '
                    f'({fmt_duration(elapsed)} elapsed)'
                )
                last_status_print = time.time()
            time.sleep(POLL_INTERVAL)

    result.wall_end = time.time()
    result.wall_elapsed = result.wall_end - result.wall_start

    # Stop VRAM + CPU/RAM monitoring
    vram_monitor.stop()
    result.vram_samples, result.vram_peak_mb, result.vram_avg_mb = vram_monitor.get_results()
    stats_monitor.stop()
    (
        result.cpu_samples,
        result.cpu_pct_peak,
        result.cpu_pct_avg,
        result.ram_mb_peak,
        result.ram_mb_avg,
    ) = stats_monitor.get_results()

    # Mark timed-out files
    for fr in result.file_results:
        if fr.status == 'dispatched':
            fr.status = 'timeout'
            fr.wall_end = time.time()
            fr.wall_elapsed = fr.wall_end - fr.wall_start

    # Collect Redis benchmark data for each file
    print(f'\n  [{now_ts()}] Collecting benchmark data from Redis...')
    time.sleep(2)  # Allow final writes to propagate
    for fr in result.file_results:
        if fr.task_id:
            fr.stages = collect_benchmark_stages(r, fr.task_id)
            fr.vram_profile = collect_vram_profile(r, fr.task_id)
            if not fr.stages:
                print(
                    f'    WARNING: No benchmark data for {fr.filename[:40]} '
                    f'(task_id={fr.task_id[:12]})'
                )

    # Print batch summary
    print_batch_summary(result)

    return result
