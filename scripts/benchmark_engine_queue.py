#!/usr/bin/env python3
"""Throughput benchmark: submit N files to Celery queues and measure end-to-end performance.

Two operating modes:
  1. Queue mode (--file-uuids): submits real Celery pipeline chains for files already
     uploaded to a running stack, then polls the Redis result backend until all complete.
  2. Direct mode (default, --audio-dir): runs Engine.process() in-process for each file
     (same as benchmark_engine_single.py) but batches them with --concurrency threads.

Usage examples:
    # Queue mode — benchmark 5 already-uploaded files
    python benchmark_engine_queue.py --file-uuids uuid1,uuid2,uuid3,uuid4,uuid5

    # Direct mode — benchmark audio files from disk with 3 concurrent threads
    python benchmark_engine_queue.py --audio-dir /app/benchmark/test_audio --concurrency 3

    # Inside the celery-worker container (queue mode)
    docker exec opentranscribe-celery-worker \
        python /app/scripts/benchmark_engine_queue.py --file-uuids uuid1,uuid2
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Data structures
# ──────────────────────────────────────────────────────────────────────────────

_FILE_COLUMNS = [
    'file_name',
    'audio_duration_s',
    'submit_time',
    'complete_time',
    'wall_time_s',
    'realtime_factor',
    'success',
    'error_msg',
    'task_id',
]

_SUMMARY_COLUMNS = [
    'aggregate_files',
    'aggregate_wall_s',
    'aggregate_audio_s',
    'avg_realtime_factor',
    'cpu_queue_depth_mean',
    'gpu_queue_depth_mean',
]


@dataclass
class FileRow:
    file_name: str
    audio_duration_s: float
    submit_time: float
    complete_time: float = 0.0
    wall_time_s: float = 0.0
    realtime_factor: float = 0.0
    success: bool = False
    error_msg: str = ''
    task_id: str = ''

    def as_dict(self) -> dict[str, Any]:
        return {
            'file_name': self.file_name,
            'audio_duration_s': round(self.audio_duration_s, 1),
            'submit_time': round(self.submit_time, 3),
            'complete_time': round(self.complete_time, 3),
            'wall_time_s': round(self.wall_time_s, 2),
            'realtime_factor': round(self.realtime_factor, 2),
            'success': self.success,
            'error_msg': self.error_msg,
            'task_id': self.task_id,
        }


@dataclass
class QueueSample:
    timestamp: float
    cpu_depth: int
    gpu_depth: int


@dataclass
class BenchmarkState:
    rows: list[FileRow] = field(default_factory=list)
    samples: list[QueueSample] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    completed_count: int = 0
    total_count: int = 0


# ──────────────────────────────────────────────────────────────────────────────
# Audio duration helpers
# ──────────────────────────────────────────────────────────────────────────────


def _audio_duration_s(audio_path: Path) -> float:
    """Return audio duration in seconds (WAV only via stdlib; 0.0 on failure)."""
    import wave

    try:
        with wave.open(str(audio_path), 'rb') as wf:
            return wf.getnframes() / wf.getframerate()
    except Exception:
        return 0.0


# ──────────────────────────────────────────────────────────────────────────────
# Redis queue depth monitoring
# ──────────────────────────────────────────────────────────────────────────────


def _build_redis_client() -> Any:
    """Return a Redis client connected to REDIS_URL from the environment."""
    import redis

    redis_url = os.getenv('REDIS_URL', 'redis://localhost:5177/0')
    return redis.from_url(redis_url)


def _llen_queue(client: Any, queue_name: str) -> int:
    """Return the approximate depth of a Celery queue via LLEN.

    Kombu priority queues use the bare queue name for priority 0 and
    ``<queue>\x06\x16<priority>`` suffixes for other priorities.  LLEN on
    the base key captures priority-0 messages — adequate for a relative
    depth signal.
    """
    try:
        result = client.llen(queue_name)
        return int(result or 0)
    except Exception:
        return 0


def monitor_queues(
    state: BenchmarkState,
    stop_event: threading.Event,
    poll_interval: float,
) -> None:
    """Background thread: sample CPU/GPU queue depths every poll_interval seconds."""
    try:
        client = _build_redis_client()
    except Exception as exc:
        logger.warning('Queue monitor: Redis unavailable — %s', exc)
        return

    while not stop_event.is_set():
        cpu_depth = _llen_queue(client, 'cpu')
        gpu_depth = _llen_queue(client, 'gpu')
        sample = QueueSample(
            timestamp=time.time(),
            cpu_depth=cpu_depth,
            gpu_depth=gpu_depth,
        )
        with state.lock:
            state.samples.append(sample)
        stop_event.wait(poll_interval)


# ──────────────────────────────────────────────────────────────────────────────
# Queue mode — dispatch real Celery chains
# ──────────────────────────────────────────────────────────────────────────────


def _resolve_file_duration_from_db(file_uuid: str) -> float:
    """Look up audio duration for a MediaFile UUID from the database."""
    try:
        from app.db.session_utils import session_scope
        from app.models.media import MediaFile

        with session_scope() as db:
            mf = db.query(MediaFile).filter(MediaFile.uuid == file_uuid).first()
            if mf and mf.duration:
                return float(mf.duration)
    except Exception as exc:
        logger.debug('Could not resolve duration for %s: %s', file_uuid, exc)
    return 0.0


def _dispatch_one_file(file_uuid: str) -> tuple[str, str]:
    """Dispatch the transcription pipeline for one UUID; return (task_id, file_name)."""
    from app.db.session_utils import session_scope
    from app.models.media import MediaFile
    from app.tasks.transcription.dispatch import dispatch_transcription_pipeline

    app_task_id = str(uuid.uuid4())
    file_name = file_uuid

    with session_scope() as db:
        mf = db.query(MediaFile).filter(MediaFile.uuid == file_uuid).first()
        if mf and mf.original_filename:
            file_name = mf.original_filename

    dispatch_transcription_pipeline(file_uuid=file_uuid, task_id=app_task_id)
    return app_task_id, file_name


def _poll_task_completion(app_task_id: str, timeout_s: float = 14400.0) -> tuple[bool, str]:
    """Poll the DB task record until the pipeline completes or times out.

    Returns (success, error_msg).
    """
    from app.db.session_utils import session_scope
    from app.models.task import Task

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with session_scope() as db:
                task = db.query(Task).filter(Task.task_id == app_task_id).first()
                if task:
                    if task.status == 'completed':
                        return True, ''
                    if task.status == 'failed':
                        return False, task.error_message or 'task failed'
        except Exception as exc:
            logger.debug('Poll error for %s: %s', app_task_id, exc)
        time.sleep(2.0)

    return False, f'timed out after {timeout_s:.0f}s'


def run_queue_mode(
    file_uuids: list[str],
    concurrency: int,
    state: BenchmarkState,
) -> None:
    """Submit file UUIDs as real Celery chains and wait for completion."""
    state.total_count = len(file_uuids)

    def _process_one(file_uuid: str) -> FileRow:
        duration_s = _resolve_file_duration_from_db(file_uuid)
        submit_time = time.time()
        app_task_id = ''
        file_name = file_uuid

        try:
            app_task_id, file_name = _dispatch_one_file(file_uuid)
            success, error_msg = _poll_task_completion(app_task_id)
        except Exception as exc:
            success = False
            error_msg = str(exc)

        complete_time = time.time()
        wall_time_s = complete_time - submit_time
        realtime_factor = duration_s / wall_time_s if wall_time_s > 0 and duration_s > 0 else 0.0

        row = FileRow(
            file_name=file_name,
            audio_duration_s=duration_s,
            submit_time=submit_time,
            complete_time=complete_time,
            wall_time_s=wall_time_s,
            realtime_factor=realtime_factor,
            success=success,
            error_msg=error_msg,
            task_id=app_task_id,
        )

        with state.lock:
            state.completed_count += 1
            n = state.completed_count
            t = state.total_count

        status = 'OK' if success else 'FAIL'
        logger.info(
            '[%d/%d] %s completed in %.1fs (realtime: %.1fx) [%s]',
            n,
            t,
            file_name,
            wall_time_s,
            realtime_factor,
            status,
        )
        print(
            f'[{n}/{t}] {file_name} completed in {wall_time_s:.1f}s '
            f'(realtime: {realtime_factor:.1f}x) [{status}]',
            flush=True,
        )
        return row

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(_process_one, fuid): fuid for fuid in file_uuids}
        for future in as_completed(futures):
            try:
                row = future.result()
            except Exception as exc:
                fuid = futures[future]
                row = FileRow(
                    file_name=fuid,
                    audio_duration_s=0.0,
                    submit_time=time.time(),
                    complete_time=time.time(),
                    success=False,
                    error_msg=str(exc),
                )
            with state.lock:
                state.rows.append(row)


# ──────────────────────────────────────────────────────────────────────────────
# Direct (in-process) mode — Engine.process() via threads
# ──────────────────────────────────────────────────────────────────────────────


def run_direct_mode(
    audio_files: list[Path],
    concurrency: int,
    state: BenchmarkState,
) -> None:
    """Run Engine.process() in-process for each file using a thread pool."""
    from app.transcription import Engine, JobSpec
    from app.transcription.engine.config import EngineConfig

    engine_config = EngineConfig()
    engine = Engine(engine_config)

    state.total_count = len(audio_files)

    def _process_one(audio_path: Path) -> FileRow:
        duration_s = _audio_duration_s(audio_path)
        submit_time = time.time()
        job_task_id = f'bench-{uuid.uuid4().hex[:8]}'
        success = False
        error_msg = ''

        try:
            job = JobSpec(audio_path=str(audio_path), task_id=job_task_id)
            engine.process(job)
            success = True
        except Exception as exc:
            error_msg = str(exc)
            logger.error('Engine failed on %s: %s', audio_path.name, exc)

        complete_time = time.time()
        wall_time_s = complete_time - submit_time
        realtime_factor = duration_s / wall_time_s if wall_time_s > 0 and duration_s > 0 else 0.0

        row = FileRow(
            file_name=audio_path.name,
            audio_duration_s=duration_s,
            submit_time=submit_time,
            complete_time=complete_time,
            wall_time_s=wall_time_s,
            realtime_factor=realtime_factor,
            success=success,
            error_msg=error_msg,
            task_id=job_task_id,
        )

        with state.lock:
            state.completed_count += 1
            n = state.completed_count
            t = state.total_count

        status = 'OK' if success else 'FAIL'
        print(
            f'[{n}/{t}] {audio_path.name} completed in {wall_time_s:.1f}s '
            f'(realtime: {realtime_factor:.1f}x) [{status}]',
            flush=True,
        )
        return row

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(_process_one, p): p for p in audio_files}
        for future in as_completed(futures):
            try:
                row = future.result()
            except Exception as exc:
                p = futures[future]
                row = FileRow(
                    file_name=p.name,
                    audio_duration_s=0.0,
                    submit_time=time.time(),
                    complete_time=time.time(),
                    success=False,
                    error_msg=str(exc),
                )
            with state.lock:
                state.rows.append(row)


# ──────────────────────────────────────────────────────────────────────────────
# Reporting
# ──────────────────────────────────────────────────────────────────────────────


def _fmt_val(val: Any) -> str:
    if val is None:
        return '—'
    if isinstance(val, bool):
        return 'Y' if val else 'N'
    return str(val)


def print_file_table(rows: list[FileRow]) -> None:
    dicts = [r.as_dict() for r in rows]
    widths = {col: len(col) for col in _FILE_COLUMNS}
    for d in dicts:
        for col in _FILE_COLUMNS:
            widths[col] = max(widths[col], len(_fmt_val(d.get(col))))

    header = '  '.join(col.ljust(widths[col]) for col in _FILE_COLUMNS)
    sep = '  '.join('-' * widths[col] for col in _FILE_COLUMNS)
    print(header)
    print(sep)
    for d in dicts:
        line = '  '.join(_fmt_val(d.get(col)).ljust(widths[col]) for col in _FILE_COLUMNS)
        print(line)


def compute_summary(
    rows: list[FileRow],
    samples: list[QueueSample],
) -> dict[str, Any]:
    successful = [r for r in rows if r.success]
    agg_audio_s = sum(r.audio_duration_s for r in successful)
    agg_wall_s = sum(r.wall_time_s for r in successful)
    avg_rtf = sum(r.realtime_factor for r in successful) / len(successful) if successful else 0.0
    cpu_mean = sum(s.cpu_depth for s in samples) / len(samples) if samples else 0.0
    gpu_mean = sum(s.gpu_depth for s in samples) / len(samples) if samples else 0.0
    return {
        'aggregate_files': len(rows),
        'aggregate_wall_s': round(agg_wall_s, 2),
        'aggregate_audio_s': round(agg_audio_s, 1),
        'avg_realtime_factor': round(avg_rtf, 2),
        'cpu_queue_depth_mean': round(cpu_mean, 2),
        'gpu_queue_depth_mean': round(gpu_mean, 2),
    }


def print_summary(summary: dict[str, Any]) -> None:
    print()
    print('── Summary ──────────────────────────────────────────')
    for key in _SUMMARY_COLUMNS:
        print(f'  {key:<30} {summary.get(key, "—")}')
    print()

    agg_wall = summary.get('aggregate_wall_s', 0.0)
    agg_audio = summary.get('aggregate_audio_s', 0.0)
    n = summary.get('aggregate_files', 0)
    if agg_wall and agg_audio:
        tput = n / (agg_wall / 3600.0)
        print(f'  Throughput: {tput:.1f} files/hour')
        agg_rtf = agg_audio / agg_wall
        print(f'  Aggregate realtime factor: {agg_rtf:.2f}x')


def write_csv(rows: list[FileRow], summary: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open('w', newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(fh, fieldnames=_FILE_COLUMNS, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            writer.writerow(row.as_dict())

        fh.write('\n')
        writer2 = csv.DictWriter(fh, fieldnames=_SUMMARY_COLUMNS, extrasaction='ignore')
        writer2.writeheader()
        writer2.writerow(summary)

    logger.info('CSV written to %s', output_path)


# ──────────────────────────────────────────────────────────────────────────────
# File selection (direct mode)
# ──────────────────────────────────────────────────────────────────────────────


def select_audio_files(audio_dir: Path, max_files: int) -> list[Path]:
    """Return up to max_files WAV files from audio_dir, shortest first."""
    wavs = sorted(audio_dir.glob('*.wav'), key=lambda p: p.stat().st_size)
    if not wavs:
        raise FileNotFoundError(f'No WAV files found in {audio_dir}')
    return wavs[:max_files]


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            'Queue throughput benchmark: submit files to Celery or run Engine in-process, '
            'measure wall-clock throughput and queue depths.'
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        '--audio-dir',
        default='/app/benchmark/test_audio',
        help='Directory of WAV files (direct mode only, ignored when --file-uuids is set).',
    )
    parser.add_argument(
        '--file-uuids',
        default='',
        help=(
            'Comma-separated MediaFile UUIDs already uploaded to a running stack. '
            'When provided, tasks are submitted as real Celery chains (queue mode).'
        ),
    )
    parser.add_argument(
        '--max-files',
        type=int,
        default=5,
        help='Max files to process in direct mode (ignored in queue mode).',
    )
    parser.add_argument(
        '--output',
        default='',
        help=(
            'CSV output path. Defaults to '
            f'{Path(tempfile.gettempdir()) / "engine_queue_<timestamp>.csv"}.'
        ),
    )
    parser.add_argument(
        '--concurrency',
        type=int,
        default=3,
        help='Number of files to submit / process simultaneously.',
    )
    parser.add_argument(
        '--poll-interval',
        type=float,
        default=2.0,
        help='Seconds between queue depth polls.',
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

    output_path = Path(
        args.output
        if args.output
        else Path(tempfile.gettempdir()) / f'engine_queue_{int(time.time())}.csv'
    )

    state = BenchmarkState()
    stop_event = threading.Event()

    monitor_thread = threading.Thread(
        target=monitor_queues,
        args=(state, stop_event, args.poll_interval),
        daemon=True,
        name='queue-monitor',
    )
    monitor_thread.start()

    queue_mode = bool(args.file_uuids.strip())

    if queue_mode:
        uuids = [u.strip() for u in args.file_uuids.split(',') if u.strip()]
        if not uuids:
            logger.error('--file-uuids provided but no valid UUIDs parsed')
            stop_event.set()
            return 1
        logger.info('Queue mode: dispatching %d file(s) to Celery', len(uuids))
        print(f'Queue mode: dispatching {len(uuids)} file(s) via Celery pipeline…', flush=True)
        run_queue_mode(uuids, args.concurrency, state)
    else:
        audio_dir = Path(args.audio_dir)
        if not audio_dir.exists():
            logger.error('Audio directory not found: %s', audio_dir)
            stop_event.set()
            return 1
        try:
            audio_files = select_audio_files(audio_dir, args.max_files)
        except FileNotFoundError as exc:
            logger.error('%s', exc)
            stop_event.set()
            return 1

        logger.info(
            'Direct mode: processing %d file(s): %s',
            len(audio_files),
            [f.name for f in audio_files],
        )
        print(
            f'Direct mode: processing {len(audio_files)} file(s) '
            f'with concurrency={args.concurrency}…',
            flush=True,
        )
        run_direct_mode(audio_files, args.concurrency, state)

    stop_event.set()
    monitor_thread.join(timeout=5.0)

    rows = sorted(state.rows, key=lambda r: r.submit_time)
    summary = compute_summary(rows, state.samples)

    print()
    print_file_table(rows)
    print_summary(summary)

    write_csv(rows, summary, output_path)
    print(f'Results saved to {output_path}')

    failed = sum(1 for r in rows if not r.success)
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
