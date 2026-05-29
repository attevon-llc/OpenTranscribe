#!/usr/bin/env python3
"""Progressive parallel transcription benchmark.

Tests reprocessing pipeline throughput with increasing parallelism.
Dispatches batches of 1, 3, 5, 8, 12, 20 files simultaneously, measures:
  - Per-file wall time (dispatch to completion)
  - Batch wall time (all files start to last finish)
  - Pipeline stage durations from Redis (preprocess, GPU, postprocess, gaps)
  - GPU VRAM usage sampled every 2s via nvidia-smi
  - Per-file VRAM profiles from Redis

Requires:
    - ENABLE_BENCHMARK_TIMING=true in .env
    - ENABLE_VRAM_PROFILING=true in .env (optional, for per-task VRAM)
    - All services running with --gpu-scale
    - PostgreSQL accessible via docker exec

Usage:
    source backend/venv/bin/activate
    python scripts/benchmark_parallel.py [--batches 1,3,5] [--output benchmarks/]

    # Recommended: use fixed corpus for repeatable whitepaper-quality results
    python scripts/benchmark_parallel.py --corpus-file docs/benchmark-corpus/corpus.json \\
        --batches 1,4,8,10,12,16 --gpu-id 2

    # Duration curve (sequential, one file at a time, full corpus)
    python scripts/benchmark_parallel.py --corpus-file docs/benchmark-corpus/corpus.json \\
        --sequential --output benchmarks/duration_curve/

    # Single file first to find bottlenecks
    python scripts/benchmark_parallel.py --batches 1

    # Progressive scaling
    python scripts/benchmark_parallel.py --batches 1,3,5,8,12,20

    # Custom GPU device to monitor
    python scripts/benchmark_parallel.py --gpu-id 2
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import redis
import requests

# Force unbuffered stdout for real-time output in background mode
sys.stdout.reconfigure(line_buffering=True)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BACKEND_URL = os.environ.get('BENCHMARK_BACKEND_URL', 'http://localhost:5174')
POLL_INTERVAL = 5.0  # seconds between status polls


def _load_redis_url() -> str:
    """Build Redis URL from BENCHMARK_REDIS_URL env var, then .env, then sensible default."""
    explicit = os.environ.get('BENCHMARK_REDIS_URL', '')
    if explicit:
        return explicit
    env_file = os.path.join(os.path.dirname(__file__), '..', '.env')
    password = ''
    port = '5177'
    try:
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line.startswith('REDIS_PASSWORD='):
                    password = line.split('=', 1)[1].split('#', 1)[0].strip().strip('"\'')
                elif line.startswith('REDIS_PORT=') and 'already set' not in line:
                    # Strip inline comments + whitespace (e.g. "5177  # debug").
                    port = line.split('=', 1)[1].split('#', 1)[0].strip().strip('"\'')
    except OSError:
        pass
    if password:
        return f'redis://:{password}@localhost:{port}/0'
    return f'redis://localhost:{port}/0'


REDIS_URL = _load_redis_url()
POLL_TIMEOUT = int(os.environ.get('BENCHMARK_POLL_TIMEOUT', '14400'))  # 4 hours default
VRAM_SAMPLE_INTERVAL = 2.0  # seconds between nvidia-smi samples
STATS_SAMPLE_INTERVAL = 2.0  # seconds between docker-stats CPU/RAM samples
GPU_DEVICE_ID = 0  # host GPU to monitor (overridable via --gpu-id)
# Container/DB names are overridable so the bench stack (otbench-*) and the dev
# stack (opentranscribe-*) never get confused. The orchestrator sets these.
DB_CONTAINER = os.environ.get('BENCHMARK_DB_CONTAINER', 'opentranscribe-postgres')
DB_USER = 'postgres'
DB_NAME = 'opentranscribe'


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class VramSample:
    timestamp: float
    used_mb: int
    total_mb: int
    free_mb: int
    utilization_pct: int
    temp_c: int


@dataclass
class CpuSample:
    """One docker-stats sample aggregated across the monitored worker containers."""

    timestamp: float
    cpu_pct: float  # summed CPU% across containers (100% == one core)
    mem_mb: float  # summed RSS across containers


@dataclass
class FileResult:
    uuid: str
    filename: str
    duration_s: float
    task_id: str = ''
    wall_start: float = 0.0
    wall_end: float = 0.0
    wall_elapsed: float = 0.0
    status: str = ''
    stages: dict = field(default_factory=dict)
    vram_profile: dict | None = None


@dataclass
class BatchResult:
    batch_size: int
    wall_start: float = 0.0
    wall_end: float = 0.0
    wall_elapsed: float = 0.0
    file_results: list[FileResult] = field(default_factory=list)
    vram_samples: list[VramSample] = field(default_factory=list)
    vram_peak_mb: int = 0
    vram_avg_mb: float = 0.0
    cpu_samples: list[CpuSample] = field(default_factory=list)
    cpu_pct_peak: float = 0.0
    cpu_pct_avg: float = 0.0
    ram_mb_peak: float = 0.0
    ram_mb_avg: float = 0.0


def _percentile(values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (pct in 0..100). Empty -> 0.0."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    rank = (pct / 100.0) * (len(s) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(s) - 1)
    frac = rank - lo
    return s[lo] + (s[hi] - s[lo]) * frac


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def get_auth_token() -> str:
    email = os.environ.get('BENCHMARK_EMAIL', 'admin@example.com')
    password = os.environ.get('BENCHMARK_PASSWORD', 'password')
    last_exc: Exception | None = None
    for attempt in range(5):
        try:
            resp = requests.post(
                f'{BACKEND_URL}/api/auth/token',
                data={'username': email, 'password': password},
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()['access_token']
        except requests.RequestException as e:
            last_exc = e
            if attempt < 4:
                time.sleep(3)
    raise RuntimeError(f'auth failed after retries: {last_exc}')


# Token manager: auto-refreshes when token is near expiry
_token_cache: dict[str, str | float] = {'token': '', 'expires_at': 0.0}
TOKEN_LIFETIME = 1200  # refresh every 20 min (tokens typically last 30 min)


def get_valid_token() -> str:
    """Return a valid token, refreshing if needed."""
    if time.time() < _token_cache['expires_at']:
        return str(_token_cache['token'])
    token = get_auth_token()
    _token_cache['token'] = token
    _token_cache['expires_at'] = time.time() + TOKEN_LIFETIME
    return token


# ---------------------------------------------------------------------------
# Database helpers (via docker exec psql)
# ---------------------------------------------------------------------------
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
        print(f'  DB ERROR: {result.stderr.strip()}', file=sys.stderr)
        return []
    rows = []
    for line in result.stdout.strip().split('\n'):
        line = line.strip()
        if line:
            rows.append(line.split('\t'))
    return rows


def get_benchmark_files(
    count: int,
    min_duration: int = 10800,
    max_duration: int = 0,
) -> list[dict]:
    """Get the N longest completed files with actual data in storage."""
    duration_filter = f'duration >= {min_duration}'
    if max_duration > 0:
        duration_filter += f' AND duration <= {max_duration}'
    rows = db_query(
        f'SELECT uuid, filename, duration, file_size '
        f'FROM media_file '
        f"WHERE {duration_filter} AND file_size > 0 AND status = 'completed' "
        f'ORDER BY duration DESC '
        f'LIMIT {count}'
    )
    files = []
    for row in rows:
        files.append(
            {
                'uuid': row[0].strip(),
                'filename': row[1].strip(),
                'duration': float(row[2].strip()),
                'file_size': int(row[3].strip()),
            }
        )
    return files


def get_active_task_id(file_uuid: str) -> str:
    """Get the active_task_id for a file from the database."""
    rows = db_query(f"SELECT active_task_id FROM media_file WHERE uuid = '{file_uuid}'")
    if rows and rows[0][0].strip():
        return rows[0][0].strip()
    return ''


def get_task_timestamps(task_id: str) -> dict:
    """Get task created_at and completed_at from the task table."""
    rows = db_query(f"SELECT created_at, completed_at, status FROM task WHERE id = '{task_id}'")
    if rows:
        return {
            'created_at': rows[0][0].strip() if rows[0][0].strip() else None,
            'completed_at': rows[0][1].strip() if len(rows[0]) > 1 and rows[0][1].strip() else None,
            'status': rows[0][2].strip() if len(rows[0]) > 2 else '',
        }
    return {}


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------
def trigger_reprocess(token: str, file_uuid: str) -> bool:
    """Trigger full reprocessing of a file. Returns True on success."""
    try:
        resp = requests.post(
            f'{BACKEND_URL}/api/files/{file_uuid}/reprocess',
            headers={'Authorization': f'Bearer {token}'},
            json={},
            timeout=30,
        )
        if resp.status_code >= 400:
            print(
                f'  REPROCESS FAILED for {file_uuid}: {resp.status_code} {resp.text[:200]}',
                file=sys.stderr,
            )
            return False
        return True
    except requests.RequestException as e:
        print(f'  REPROCESS ERROR for {file_uuid}: {e}', file=sys.stderr)
        return False


def get_file_status(token: str, file_uuid: str) -> str:
    """Get current file status via API (uses info endpoint for lightweight response)."""
    try:
        resp = requests.get(
            f'{BACKEND_URL}/api/files/{file_uuid}/info',
            headers={'Authorization': f'Bearer {token}'},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get('status', 'unknown')
    except requests.RequestException:
        pass
    return 'unknown'


def upload_file(token: str, path: str) -> str:
    """Upload a fresh file via the real user flow (POST /api/files + X-File-Hash).

    Returns the new MediaFile uuid (which auto-triggers the transcription
    pipeline), or '' on failure. Used by --upload mode so each level simulates
    a user uploading files rather than reprocessing existing ones.
    """
    import hashlib
    import mimetypes

    p = Path(path)
    if not p.exists():
        print(f'  UPLOAD FAILED: file not found {path}', file=sys.stderr)
        return ''
    h = hashlib.sha256()
    with open(p, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    content_type = mimetypes.guess_type(p.name)[0] or 'application/octet-stream'
    try:
        with open(p, 'rb') as fh:
            resp = requests.post(
                f'{BACKEND_URL}/api/files',
                headers={'Authorization': f'Bearer {token}', 'X-File-Hash': h.hexdigest()},
                files={'file': (p.name, fh, content_type)},
                timeout=3600,
            )
        if resp.status_code >= 400:
            print(
                f'  UPLOAD FAILED for {p.name}: {resp.status_code} {resp.text[:200]}',
                file=sys.stderr,
            )
            return ''
        j = resp.json()
        return j.get('uuid') or j.get('id') or ''
    except requests.RequestException as e:
        print(f'  UPLOAD ERROR for {p.name}: {e}', file=sys.stderr)
        return ''


# ---------------------------------------------------------------------------
# VRAM monitoring
# ---------------------------------------------------------------------------
class VramMonitor:
    """Background thread that samples GPU VRAM via nvidia-smi."""

    def __init__(self, gpu_id: int = GPU_DEVICE_ID):
        self.gpu_id = gpu_id
        self.samples: list[VramSample] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self):
        while not self._stop.is_set():
            sample = self._sample()
            if sample:
                self.samples.append(sample)
            self._stop.wait(VRAM_SAMPLE_INTERVAL)

    def _sample(self) -> VramSample | None:
        try:
            result = subprocess.run(
                [
                    'nvidia-smi',
                    f'--id={self.gpu_id}',
                    '--query-gpu=memory.used,memory.total,memory.free,utilization.gpu,temperature.gpu',
                    '--format=csv,noheader,nounits',
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                parts = [p.strip() for p in result.stdout.strip().split(',')]
                return VramSample(
                    timestamp=time.time(),
                    used_mb=int(parts[0]),
                    total_mb=int(parts[1]),
                    free_mb=int(parts[2]),
                    utilization_pct=int(parts[3]),
                    temp_c=int(parts[4]),
                )
        except Exception:
            pass
        return None

    def get_results(self) -> tuple[list[VramSample], int, float]:
        """Returns (samples, peak_used_mb, avg_used_mb)."""
        if not self.samples:
            return [], 0, 0.0
        peak = max(s.used_mb for s in self.samples)
        avg = sum(s.used_mb for s in self.samples) / len(self.samples)
        return self.samples, peak, avg


class DockerStatsMonitor:
    """Background thread sampling CPU% and RAM of the worker containers via `docker stats`.

    Captures the host-side compute cost (CPU-bound preprocess/postprocess, RAM
    pressure) alongside the GPU profile, so the whitepaper can attribute
    bottlenecks. Sampling is a single short-lived `docker stats --no-stream`
    call per interval — negligible overhead, never perturbs the pipeline.
    """

    def __init__(self, containers: list[str]):
        self.containers = [c for c in containers if c]
        self.samples: list[CpuSample] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        if not self.containers:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=8)

    def _run(self):
        while not self._stop.is_set():
            sample = self._sample()
            if sample:
                self.samples.append(sample)
            self._stop.wait(STATS_SAMPLE_INTERVAL)

    def _sample(self) -> CpuSample | None:
        try:
            result = subprocess.run(
                [
                    'docker',
                    'stats',
                    '--no-stream',
                    '--format',
                    '{{.CPUPerc}}\t{{.MemUsage}}',
                    *self.containers,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return None
            cpu_total = 0.0
            mem_total = 0.0
            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                parts = line.split('\t')
                if len(parts) < 2:
                    continue
                cpu_total += _parse_cpu_pct(parts[0])
                mem_total += _parse_mem_usage(parts[1])
            return CpuSample(timestamp=time.time(), cpu_pct=cpu_total, mem_mb=mem_total)
        except Exception:
            return None

    def get_results(self) -> tuple[list[CpuSample], float, float, float, float]:
        """Returns (samples, cpu_peak, cpu_avg, ram_peak_mb, ram_avg_mb)."""
        if not self.samples:
            return [], 0.0, 0.0, 0.0, 0.0
        cpu_peak = max(s.cpu_pct for s in self.samples)
        cpu_avg = sum(s.cpu_pct for s in self.samples) / len(self.samples)
        ram_peak = max(s.mem_mb for s in self.samples)
        ram_avg = sum(s.mem_mb for s in self.samples) / len(self.samples)
        return self.samples, cpu_peak, cpu_avg, ram_peak, ram_avg


def _parse_cpu_pct(text: str) -> float:
    """'123.45%' -> 123.45."""
    try:
        return float(text.strip().rstrip('%'))
    except ValueError:
        return 0.0


def _parse_mem_usage(text: str) -> float:
    """'1.5GiB / 62GiB' -> 1536.0 (MB), taking the used side only."""
    used = text.split('/')[0].strip()
    num = ''.join(ch for ch in used if ch.isdigit() or ch == '.')
    unit = used[len(num) :].strip().lower()
    try:
        val = float(num)
    except ValueError:
        return 0.0
    if unit.startswith('gi') or unit.startswith('gb'):
        return val * 1024
    if unit.startswith('ki') or unit.startswith('kb'):
        return val / 1024
    if unit.startswith('b'):
        return val / (1024 * 1024)
    # mib / mb (and bare numbers) default to MB
    return val


# ---------------------------------------------------------------------------
# Redis data collection
# ---------------------------------------------------------------------------
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
    print(f'\n  [{_ts()}] Triggering {batch_size} {action} requests...')
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
    print(f'\n  [{_ts()}] Polling for completion (timeout: {POLL_TIMEOUT / 60:.0f}min)...')
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
                            f'    [{_ts()}] DONE: {fr.filename[:45]} '
                            f'({_fmt_duration(fr.wall_elapsed)}) '
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
                            f'    [{_ts()}] ERROR: {fr.filename[:45]} '
                            f'({_fmt_duration(fr.wall_elapsed)})'
                        )
            # Small delay between per-file status checks
            time.sleep(0.1)

        if pending:
            # Print status every 60 seconds
            if time.time() - last_status_print > 60:
                elapsed = time.time() - result.wall_start
                print(
                    f'    [{_ts()}] {len(completed)} done, {len(pending)} pending '
                    f'({_fmt_duration(elapsed)} elapsed)'
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
    print(f'\n  [{_ts()}] Collecting benchmark data from Redis...')
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
    _print_batch_summary(result)

    return result


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------
def _ts() -> str:
    return datetime.now().strftime('%H:%M:%S')


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f'{seconds:.1f}s'
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f'{m}m{s:02d}s'
    h, m = divmod(m, 60)
    return f'{h}h{m:02d}m{s:02d}s'


def _print_batch_summary(result: BatchResult):
    """Print summary for a single batch."""
    completed = [fr for fr in result.file_results if fr.status == 'completed']
    errored = [fr for fr in result.file_results if fr.status == 'error']

    print(f'\n  {"─" * 70}')
    print(f'  BATCH {result.batch_size} SUMMARY')
    print(f'  {"─" * 70}')
    print(f'  Total wall time:  {_fmt_duration(result.wall_elapsed)}')
    print(f'  Completed:        {len(completed)}/{result.batch_size}')
    if errored:
        print(f'  Errors:           {len(errored)}')
    print(f'  VRAM peak:        {result.vram_peak_mb} MB')
    print(f'  VRAM avg:         {result.vram_avg_mb:.0f} MB')
    if result.cpu_samples:
        print(f'  CPU peak/avg:     {result.cpu_pct_peak:.0f}% / {result.cpu_pct_avg:.0f}%')
        print(f'  RAM peak/avg:     {result.ram_mb_peak:.0f} / {result.ram_mb_avg:.0f} MB')

    if completed:
        wall_times = [fr.wall_elapsed for fr in completed]
        print('\n  Per-file wall time:')
        print(f'    {"File":<45} {"Wall":>10} {"GPU":>10} {"Audio":>8}')
        print(f'    {"─" * 45} {"─" * 10} {"─" * 10} {"─" * 8}')
        for fr in completed:
            gpu_dur = fr.stages.get('3_gpu_transcribe', 0)
            audio_hrs = fr.duration_s / 3600
            print(
                f'    {fr.filename[:45]:<45} '
                f'{_fmt_duration(fr.wall_elapsed):>10} '
                f'{_fmt_duration(gpu_dur):>10} '
                f'{audio_hrs:.1f}h'
            )

        avg_wall = sum(wall_times) / len(wall_times)
        print(f'\n    Avg wall time/file:   {_fmt_duration(avg_wall)}')
        print(f'    Min:                  {_fmt_duration(min(wall_times))}')
        print(f'    Max:                  {_fmt_duration(max(wall_times))}')

        # GPU stage breakdown
        gpu_times = [fr.stages.get('3_gpu_transcribe', 0) for fr in completed if fr.stages]
        if gpu_times and any(gpu_times):
            print('\n  Pipeline stage averages:')
            stage_keys = [
                '1_preprocess',
                '2_cpu_to_gpu_queue',
                '3_gpu_transcribe',
                '4_gpu_to_post_queue',
            ]
            for key in stage_keys:
                vals = [fr.stages.get(key, 0) for fr in completed if fr.stages and key in fr.stages]
                if vals:
                    avg = sum(vals) / len(vals)
                    print(
                        f'    {key:<25} avg={_fmt_duration(avg):>10}  '
                        f'min={_fmt_duration(min(vals)):>10}  max={_fmt_duration(max(vals)):>10}'
                    )

    # GPU queue contention analysis
    _print_queue_analysis(completed)
    print()


def _print_queue_analysis(completed: list[FileResult]):
    """Analyze GPU queue contention — how much time files spent waiting."""
    files_with_gpu = [
        fr for fr in completed if fr.stages.get('_gpu_recv') and fr.stages.get('_gpu_end')
    ]
    if not files_with_gpu:
        return

    # Sort by GPU received time
    files_with_gpu.sort(key=lambda f: f.stages['_gpu_recv'])

    print('\n  GPU scheduling timeline:')
    print(f'    {"File":<35} {"Queue Wait":>10} {"GPU Start":>12} {"GPU End":>12} {"GPU Dur":>10}')
    print(f'    {"─" * 35} {"─" * 10} {"─" * 12} {"─" * 12} {"─" * 10}')

    t0 = files_with_gpu[0].stages.get('_dispatch', files_with_gpu[0].stages['_gpu_recv'])
    for fr in files_with_gpu:
        queue_wait = fr.stages.get('2_cpu_to_gpu_queue', 0)
        gpu_start = fr.stages['_gpu_recv'] - t0
        gpu_end = fr.stages['_gpu_end'] - t0
        gpu_dur = gpu_end - gpu_start
        print(
            f'    {fr.filename[:35]:<35} '
            f'{_fmt_duration(queue_wait):>10} '
            f'{_fmt_duration(gpu_start):>12} '
            f'{_fmt_duration(gpu_end):>12} '
            f'{_fmt_duration(gpu_dur):>10}'
        )


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------
def write_reports(
    all_batches: list[BatchResult],
    output_dir: Path,
    corpus_total_audio_h: float | None = None,
    corpus_name: str | None = None,
    run_label: str = '',
):
    """Write CSV reports, machine-readable metrics.json, and final summary."""
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    # 1. Per-file details CSV
    csv_path = output_dir / f'benchmark_files_{timestamp}.csv'
    with open(csv_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                'batch_size',
                'file_uuid',
                'filename',
                'audio_duration_s',
                'audio_hours',
                'wall_elapsed_s',
                'status',
                'task_id',
                'preprocess_s',
                'cpu_to_gpu_queue_s',
                'gpu_transcribe_s',
                'gpu_to_post_queue_s',
                'total_to_gpu_end_s',
                'total_to_postprocess_s',
            ]
        )
        for batch in all_batches:
            for fr in batch.file_results:
                writer.writerow(
                    [
                        batch.batch_size,
                        fr.uuid,
                        fr.filename,
                        f'{fr.duration_s:.0f}',
                        f'{fr.duration_s / 3600:.2f}',
                        f'{fr.wall_elapsed:.1f}',
                        fr.status,
                        fr.task_id,
                        fr.stages.get('1_preprocess', ''),
                        fr.stages.get('2_cpu_to_gpu_queue', ''),
                        fr.stages.get('3_gpu_transcribe', ''),
                        fr.stages.get('4_gpu_to_post_queue', ''),
                        fr.stages.get('total_to_gpu_end', ''),
                        fr.stages.get('total_to_postprocess', ''),
                    ]
                )
    print(f'\nPer-file CSV: {csv_path}')

    # 2. Batch summary CSV (+ per-stage p50/p95, CPU/RAM)
    summary_path = output_dir / f'benchmark_summary_{timestamp}.csv'
    summary_rows: list[dict] = []
    with open(summary_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                'batch_size',
                'batch_wall_s',
                'batch_wall_fmt',
                'files_completed',
                'files_errored',
                'avg_file_wall_s',
                'min_file_wall_s',
                'max_file_wall_s',
                'avg_gpu_s',
                'min_gpu_s',
                'max_gpu_s',
                'preprocess_p50_s',
                'preprocess_p95_s',
                'cpu_to_gpu_p50_s',
                'cpu_to_gpu_p95_s',
                'gpu_transcribe_p50_s',
                'gpu_transcribe_p95_s',
                'gpu_to_post_p50_s',
                'gpu_to_post_p95_s',
                'vram_peak_mb',
                'vram_avg_mb',
                'gpu_util_pct_avg',
                'cpu_pct_peak',
                'cpu_pct_avg',
                'ram_mb_peak',
                'ram_mb_avg',
                'throughput_audio_hrs_per_wall_hr',
                'speedup_vs_single',
            ]
        )
        single_batch_wall = None
        stage_keys = [
            '1_preprocess',
            '2_cpu_to_gpu_queue',
            '3_gpu_transcribe',
            '4_gpu_to_post_queue',
        ]
        for batch in all_batches:
            completed = [fr for fr in batch.file_results if fr.status == 'completed']
            if not completed:
                continue
            walls = [fr.wall_elapsed for fr in completed]
            gpus = [fr.stages.get('3_gpu_transcribe', 0) for fr in completed if fr.stages]
            # Aggregate total-throughput RTF: ALL audio processed divided by the
            # batch wall-clock window (first dispatch -> last completion). This is
            # the steady-state system throughput, NOT a per-file end-to-end ratio.
            total_audio_s = sum(fr.duration_s for fr in completed)
            throughput = (
                (total_audio_s / 3600) / (batch.wall_elapsed / 3600) if batch.wall_elapsed else 0
            )

            if single_batch_wall is None:
                single_batch_wall = batch.wall_elapsed
            speedup = (
                (single_batch_wall * len(completed)) / batch.wall_elapsed
                if batch.wall_elapsed
                else 0
            )
            avg_util = (
                sum(s.utilization_pct for s in batch.vram_samples) / len(batch.vram_samples)
                if batch.vram_samples
                else 0
            )
            # Per-stage percentiles across completed files.
            pct: dict[str, float] = {}
            for key in stage_keys:
                vals = [fr.stages[key] for fr in completed if fr.stages and key in fr.stages]
                pct[f'{key}_p50'] = _percentile(vals, 50)
                pct[f'{key}_p95'] = _percentile(vals, 95)

            row = {
                'batch_size': batch.batch_size,
                'batch_wall_s': round(batch.wall_elapsed, 1),
                'files_completed': len(completed),
                'files_errored': len([fr for fr in batch.file_results if fr.status == 'error']),
                'avg_file_wall_s': round(sum(walls) / len(walls), 1),
                'min_file_wall_s': round(min(walls), 1),
                'max_file_wall_s': round(max(walls), 1),
                'avg_gpu_s': round(sum(gpus) / len(gpus), 1) if gpus else None,
                'preprocess_p50_s': round(pct['1_preprocess_p50'], 1),
                'preprocess_p95_s': round(pct['1_preprocess_p95'], 1),
                'gpu_transcribe_p50_s': round(pct['3_gpu_transcribe_p50'], 1),
                'gpu_transcribe_p95_s': round(pct['3_gpu_transcribe_p95'], 1),
                'vram_peak_mb': batch.vram_peak_mb,
                'vram_avg_mb': round(batch.vram_avg_mb, 0),
                'gpu_util_pct_avg': round(avg_util, 0),
                'cpu_pct_peak': round(batch.cpu_pct_peak, 1),
                'cpu_pct_avg': round(batch.cpu_pct_avg, 1),
                'ram_mb_peak': round(batch.ram_mb_peak, 0),
                'ram_mb_avg': round(batch.ram_mb_avg, 0),
                'throughput_audio_hrs_per_wall_hr': round(throughput, 2),
                'speedup_vs_single': round(speedup, 2),
            }
            summary_rows.append(row)

            writer.writerow(
                [
                    batch.batch_size,
                    f'{batch.wall_elapsed:.1f}',
                    _fmt_duration(batch.wall_elapsed),
                    len(completed),
                    len([fr for fr in batch.file_results if fr.status == 'error']),
                    f'{sum(walls) / len(walls):.1f}',
                    f'{min(walls):.1f}',
                    f'{max(walls):.1f}',
                    f'{sum(gpus) / len(gpus):.1f}' if gpus else '',
                    f'{min(gpus):.1f}' if gpus else '',
                    f'{max(gpus):.1f}' if gpus else '',
                    f'{pct["1_preprocess_p50"]:.1f}',
                    f'{pct["1_preprocess_p95"]:.1f}',
                    f'{pct["2_cpu_to_gpu_queue_p50"]:.1f}',
                    f'{pct["2_cpu_to_gpu_queue_p95"]:.1f}',
                    f'{pct["3_gpu_transcribe_p50"]:.1f}',
                    f'{pct["3_gpu_transcribe_p95"]:.1f}',
                    f'{pct["4_gpu_to_post_queue_p50"]:.1f}',
                    f'{pct["4_gpu_to_post_queue_p95"]:.1f}',
                    batch.vram_peak_mb,
                    f'{batch.vram_avg_mb:.0f}',
                    f'{avg_util:.0f}',
                    f'{batch.cpu_pct_peak:.1f}',
                    f'{batch.cpu_pct_avg:.1f}',
                    f'{batch.ram_mb_peak:.0f}',
                    f'{batch.ram_mb_avg:.0f}',
                    f'{throughput:.2f}',
                    f'{speedup:.2f}',
                ]
            )
    print(f'Summary CSV:  {summary_path}')

    # 3. VRAM timeline CSV
    vram_path = output_dir / f'benchmark_vram_{timestamp}.csv'
    with open(vram_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(
            ['batch_size', 'elapsed_s', 'used_mb', 'total_mb', 'free_mb', 'util_pct', 'temp_c']
        )
        for batch in all_batches:
            if batch.vram_samples:
                t0 = batch.vram_samples[0].timestamp
                for s in batch.vram_samples:
                    writer.writerow(
                        [
                            batch.batch_size,
                            f'{s.timestamp - t0:.1f}',
                            s.used_mb,
                            s.total_mb,
                            s.free_mb,
                            s.utilization_pct,
                            s.temp_c,
                        ]
                    )
    print(f'VRAM CSV:     {vram_path}')

    # 4. CPU/RAM timeline CSV (worker container compute cost)
    cpu_path = output_dir / f'benchmark_cpu_{timestamp}.csv'
    with open(cpu_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['batch_size', 'elapsed_s', 'cpu_pct', 'mem_mb'])
        for batch in all_batches:
            if batch.cpu_samples:
                t0 = batch.cpu_samples[0].timestamp
                for cs in batch.cpu_samples:
                    writer.writerow(
                        [
                            batch.batch_size,
                            f'{cs.timestamp - t0:.1f}',
                            f'{cs.cpu_pct:.1f}',
                            f'{cs.mem_mb:.0f}',
                        ]
                    )
    print(f'CPU/RAM CSV:  {cpu_path}')

    # 5. Machine-readable metrics.json — the collator's single source of truth.
    metrics = {
        'run_label': run_label,
        'timestamp': timestamp,
        'corpus_name': corpus_name,
        'corpus_total_audio_h': corpus_total_audio_h,
        'batches': summary_rows,
    }
    metrics_path = output_dir / 'metrics.json'
    with open(metrics_path, 'w') as f:
        json.dump(metrics, f, indent=2)
    print(f'Metrics JSON: {metrics_path}')

    # 6. Final comparison table
    _print_final_summary(
        all_batches,
        single_batch_wall,
        corpus_total_audio_h=corpus_total_audio_h,
        corpus_name=corpus_name,
    )


def _print_final_summary(
    all_batches: list[BatchResult],
    single_wall: float | None,
    corpus_total_audio_h: float | None = None,
    corpus_name: str | None = None,
):
    """Print the final scaling comparison."""
    print(f'\n{"=" * 100}')
    print('PARALLEL SCALING SUMMARY')
    if corpus_name:
        print(f'Corpus: {corpus_name}  ({corpus_total_audio_h:.2f}h total audio)')
    print(f'{"=" * 100}')
    print(
        f'{"Batch":>6} {"Wall Time":>12} {"Avg/File":>12} {"GPU Avg":>12} '
        f'{"VRAM Peak":>10} {"GPU Util%":>10} {"Throughput":>12} {"Speedup":>8}'
    )
    print(f'{"─" * 6} {"─" * 12} {"─" * 12} {"─" * 12} {"─" * 10} {"─" * 10} {"─" * 12} {"─" * 8}')

    for batch in all_batches:
        completed = [fr for fr in batch.file_results if fr.status == 'completed']
        if not completed:
            continue
        walls = [fr.wall_elapsed for fr in completed]
        gpus = [fr.stages.get('3_gpu_transcribe', 0) for fr in completed if fr.stages]
        total_audio_s = sum(fr.duration_s for fr in completed)
        throughput = (
            (total_audio_s / 3600) / (batch.wall_elapsed / 3600) if batch.wall_elapsed else 0
        )
        avg_util = (
            sum(s.utilization_pct for s in batch.vram_samples) / len(batch.vram_samples)
            if batch.vram_samples
            else 0
        )

        speedup = ''
        if single_wall:
            sp = (single_wall * len(completed)) / batch.wall_elapsed if batch.wall_elapsed else 0
            speedup = f'{sp:.2f}x'

        avg_gpu = _fmt_duration(sum(gpus) / len(gpus)) if gpus else 'N/A'

        print(
            f'{batch.batch_size:>6} '
            f'{_fmt_duration(batch.wall_elapsed):>12} '
            f'{_fmt_duration(sum(walls) / len(walls)):>12} '
            f'{avg_gpu:>12} '
            f'{batch.vram_peak_mb:>8}MB '
            f'{avg_util:>9.0f}% '
            f'{throughput:>10.2f}x '
            f'{speedup:>8}'
        )

    print(f'{"=" * 100}')
    print('Throughput = audio hours processed per wall-clock hour')
    print('Speedup = ideal sequential time / actual batch time (linear=batch_size)')
    if corpus_total_audio_h:
        # Full-corpus aggregate RTF: the whitepaper headline metric
        for batch in all_batches:
            completed = [fr for fr in batch.file_results if fr.status == 'completed']
            if not completed or not batch.wall_elapsed:
                continue
            submitted_audio_h = sum(fr.duration_s for fr in batch.file_results) / 3600
            if abs(submitted_audio_h - corpus_total_audio_h) < 0.5:
                full_rtf = corpus_total_audio_h / (batch.wall_elapsed / 3600)
                print(
                    f'\n*** Full-corpus aggregate RTF (batch={batch.batch_size}): '
                    f'{full_rtf:.1f}x  '
                    f'({corpus_total_audio_h:.1f}h audio in {_fmt_duration(batch.wall_elapsed)}) ***'
                )

    # Reprocessing projection based on measured throughput
    _print_reprocess_projection(all_batches)


def _print_reprocess_projection(all_batches: list[BatchResult]):
    """Query DB for total audio hours and project reprocessing time."""
    rows = db_query(
        'SELECT COUNT(*), COALESCE(SUM(duration), 0), '
        'COALESCE(AVG(duration), 0), COALESCE(MIN(duration), 0), '
        'COALESCE(MAX(duration), 0) '
        "FROM media_file WHERE status = 'completed' AND duration > 0"
    )
    if not rows:
        return

    total_files = int(rows[0][0])
    total_duration_s = float(rows[0][1])
    avg_duration_s = float(rows[0][2])
    total_hours = total_duration_s / 3600

    if total_files == 0:
        return

    # Get duration distribution
    dist_rows = db_query(
        'SELECT '
        '  CASE '
        "    WHEN duration < 300 THEN '< 5min' "
        "    WHEN duration < 1800 THEN '5-30min' "
        "    WHEN duration < 3600 THEN '30-60min' "
        "    WHEN duration < 7200 THEN '1-2hr' "
        "    WHEN duration < 10800 THEN '2-3hr' "
        "    ELSE '3hr+' "
        '  END AS bucket, '
        '  COUNT(*), SUM(duration) / 3600.0 '
        'FROM media_file '
        "WHERE status = 'completed' AND duration > 0 "
        'GROUP BY 1 ORDER BY MIN(duration)'
    )

    print(f'\n{"=" * 90}')
    print('REPROCESSING PROJECTION')
    print(f'{"=" * 90}')
    print(f'  Total completed files:     {total_files}')
    print(f'  Total audio duration:      {total_hours:.1f} hours ({total_hours / 24:.1f} days)')
    print(f'  Average file duration:     {_fmt_duration(avg_duration_s)}')

    if dist_rows:
        print('\n  Duration Distribution:')
        print(f'    {"Bucket":<12} {"Files":>8} {"Hours":>10}')
        print(f'    {"─" * 12} {"─" * 8} {"─" * 10}')
        for row in dist_rows:
            bucket = row[0].strip()
            count = int(row[1].strip())
            hours = float(row[2].strip())
            print(f'    {bucket:<12} {count:>8} {hours:>9.1f}')

    # Use best measured throughput for projections
    best_throughput = 0
    best_batch = 0
    for batch in all_batches:
        completed = [fr for fr in batch.file_results if fr.status == 'completed']
        if not completed or batch.wall_elapsed == 0:
            continue
        total_audio_s = sum(fr.duration_s for fr in completed)
        throughput = (total_audio_s / 3600) / (batch.wall_elapsed / 3600)
        if throughput > best_throughput:
            best_throughput = throughput
            best_batch = batch.batch_size

    # Single-file throughput (batch=1)
    single_throughput = 0
    for batch in all_batches:
        if batch.batch_size == 1:
            completed = [fr for fr in batch.file_results if fr.status == 'completed']
            if completed and batch.wall_elapsed > 0:
                total_audio_s = sum(fr.duration_s for fr in completed)
                single_throughput = (total_audio_s / 3600) / (batch.wall_elapsed / 3600)
            break

    if single_throughput > 0 or best_throughput > 0:
        print('\n  Projected Reprocessing Times:')
        print(f'    {"Config":<35} {"Throughput":>12} {"Est. Time":>12}')
        print(f'    {"─" * 35} {"─" * 12} {"─" * 12}')

        if single_throughput > 0:
            est_hrs = total_hours / single_throughput
            print(
                f'    {"1 worker (measured)":35} {single_throughput:>10.1f}x {_fmt_duration(est_hrs * 3600):>12}'
            )

        if best_throughput > 0 and best_batch > 1:
            est_hrs = total_hours / best_throughput
            print(
                f'    {f"{best_batch} workers (measured)":35} {best_throughput:>10.1f}x {_fmt_duration(est_hrs * 3600):>12}'
            )

        # Extrapolate for higher worker counts (sub-linear scaling)
        if single_throughput > 0:
            for workers in [5, 9]:
                if workers <= best_batch:
                    continue
                # ~15% overhead per additional concurrent task
                eff = workers * (1 / (1 + 0.15 * (workers - 1)))
                projected = single_throughput * eff
                est_hrs = total_hours / projected
                print(
                    f'    {f"{workers} workers (projected)":35} {projected:>10.1f}x {_fmt_duration(est_hrs * 3600):>12}'
                )

    print(f'{"=" * 90}')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description='Progressive parallel transcription benchmark',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--batches',
        default='1,3,5,8,12,20',
        help='Comma-separated batch sizes to test (default: 1,3,5,8,12,20)',
    )
    parser.add_argument(
        '--output',
        default='benchmarks',
        help='Output directory for CSV results (default: benchmarks/)',
    )
    parser.add_argument(
        '--gpu-id',
        type=int,
        default=GPU_DEVICE_ID,
        help=f'Host GPU device ID to monitor VRAM (default: {GPU_DEVICE_ID})',
    )
    parser.add_argument(
        '--cooldown',
        type=int,
        default=30,
        help='Seconds to wait between batches for GPU to settle (default: 30)',
    )
    parser.add_argument(
        '--min-duration',
        type=int,
        default=60,
        help='Minimum file duration in seconds for selection (default: 60 = 1 minute)',
    )
    parser.add_argument(
        '--max-duration',
        type=int,
        default=0,
        help='Maximum file duration in seconds (default: 0 = no limit). '
        'Use with --min-duration to select files in a narrow range for fair comparison.',
    )
    parser.add_argument(
        '--file-uuids',
        default='',
        help='Comma-separated UUIDs to use instead of auto-selecting from DB. '
        'Ensures consistent file selection across test runs.',
    )
    parser.add_argument(
        '--sequential',
        action='store_true',
        help='Process each file individually in sequence (for duration curve testing). '
        'Ignores --batches and runs batch_size=1 for each file in the list.',
    )
    parser.add_argument(
        '--corpus-file',
        default='',
        help='Path to corpus JSON (e.g. docs/benchmark-corpus/corpus.json). '
        'Loads a fixed, ordered file list for repeatable whitepaper-quality runs. '
        'Overrides --file-uuids and auto-selection; --batches N takes the first N files '
        'from the active profile.',
    )
    parser.add_argument(
        '--profile',
        default='by_duration',
        help='Corpus profile to use when --corpus-file is set. '
        'by_duration (default): duration-ascending, good for VRAM ceiling tests — '
        'short files fail fast on OOM. '
        'mixed: round-robin across tiers so --batches 4 = one file per tier; '
        'best for real-world scheduler and throughput tests. '
        'mixed_hard: hardest file from each tier, always 4 files. '
        'Any custom profile name defined in the corpus JSON "profiles" section.',
    )
    parser.add_argument(
        '--shuffle',
        action='store_true',
        help='Randomly shuffle the file order after profile selection. '
        'Simulates a real-world queue where files arrive in unpredictable order — '
        'no size bias. Combine with --profile mixed for the most realistic stress test.',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be done without triggering reprocessing',
    )
    parser.add_argument(
        '--upload',
        action='store_true',
        help='Upload fresh files (real user flow: POST /api/files) instead of reprocessing '
        'existing UUIDs. Each file is read from --audio-dir/<filename> in the corpus. '
        'Requires a clean DB (no duplicate hashes) — the soak orchestrator clears bench data '
        'before each level. corpus.json UUIDs are ignored; fresh UUIDs come from the uploads.',
    )
    parser.add_argument(
        '--audio-dir',
        default='benchmark/test_audio',
        help='Directory holding the source audio files (matched by corpus filename) for '
        '--upload mode (default: benchmark/test_audio).',
    )
    parser.add_argument(
        '--stats-containers',
        default=os.environ.get('BENCHMARK_STATS_CONTAINERS', ''),
        help='Comma-separated container names to sample CPU%%/RAM from via docker stats '
        '(e.g. otbench-celery-worker,otbench-celery-cpu-worker). Empty = skip CPU/RAM sampling.',
    )
    parser.add_argument(
        '--run-label',
        default='',
        help='Label embedded in metrics.json identifying this run '
        '(e.g. phase1_a6000_solo_conc8). Used by the collator.',
    )
    args = parser.parse_args()

    batch_sizes = [int(x.strip()) for x in args.batches.split(',')]
    max_files = max(batch_sizes)
    output_dir = Path(args.output)
    stats_containers = [c.strip() for c in args.stats_containers.split(',') if c.strip()]
    corpus: dict = {}

    print('=' * 80)
    print('PARALLEL TRANSCRIPTION BENCHMARK')
    print('=' * 80)
    print(f'Batch sizes:    {batch_sizes}')
    print(f'GPU monitor:    GPU {args.gpu_id}')
    print(f'Output dir:     {output_dir}')
    print(f'Backend:        {BACKEND_URL}')

    # Get files — corpus file (preferred) > explicit UUIDs > auto-select from DB
    if args.corpus_file:
        corpus_path = Path(args.corpus_file)
        if not corpus_path.exists():
            print(f'ERROR: corpus file not found: {corpus_path}', file=sys.stderr)
            sys.exit(1)
        with open(corpus_path) as fh:
            corpus = json.load(fh)
        corpus_files = list(corpus['files'])

        # Apply profile ordering
        profiles = corpus.get('profiles', {})
        profile_name = args.profile
        if profile_name in profiles:
            profile = profiles[profile_name]
            indices = profile['indices']
            corpus_files = [corpus['files'][i] for i in indices]
            profile_desc = profile.get('description', '')
            print(f'  Profile: {profile_name} — {profile_desc[:80]}')
        elif profile_name != 'by_duration':
            print(
                f'  WARNING: profile "{profile_name}" not found in corpus, '
                'falling back to by_duration order',
                file=sys.stderr,
            )

        total_h = sum(f['duration_s'] for f in corpus_files) / 3600
        print(
            f'\nCorpus: {corpus_path.name}  —  {len(corpus_files)} files, {total_h:.2f}h total audio'
        )
        tier_counts: dict[int, int] = {}
        for f in corpus_files:
            tier_counts[f['tier']] = tier_counts.get(f['tier'], 0) + 1
        tier_info = corpus.get('tiers', {})
        for t, cnt in sorted(tier_counts.items()):
            label = tier_info.get(str(t), {}).get('label', f'tier {t}')
            rng = tier_info.get(str(t), {}).get('range', '')
            print(f'  Tier {t} ({label:10s} {rng}): {cnt} files')

        # Map corpus fields to the internal format in profile order
        files = [
            {
                'uuid': f['uuid'],
                'filename': f['filename'],
                'duration': float(f['duration_s']),
                'file_size': int(f.get('size_mb', 0)) * 1024 * 1024,
                'tier': f.get('tier'),
            }
            for f in corpus_files
        ]

        if args.shuffle:
            random.shuffle(files)
            print('  Order: shuffled (random)')
    elif args.file_uuids:
        uuids = [u.strip() for u in args.file_uuids.split(',') if u.strip()]
        print(f'\nUsing {len(uuids)} specified files...')
        uuid_list = "','".join(uuids)
        rows = db_query(
            f'SELECT uuid, filename, duration, file_size '
            f"FROM media_file WHERE uuid IN ('{uuid_list}') AND file_size > 0 "
            f'ORDER BY duration DESC'
        )
        files = []
        for row in rows:
            files.append(
                {
                    'uuid': row[0].strip(),
                    'filename': row[1].strip(),
                    'duration': float(row[2].strip()),
                    'file_size': int(row[3].strip()),
                }
            )
    else:
        dur_desc = f'>= {args.min_duration}s'
        if args.max_duration > 0:
            dur_desc += f', <= {args.max_duration}s'
        print(f'\nFetching {max_files} longest completed files ({dur_desc})...')
        files = get_benchmark_files(
            max_files,
            min_duration=args.min_duration,
            max_duration=args.max_duration,
        )
    if len(files) < max_files:
        print(f'WARNING: Only {len(files)} files available (need {max_files})')
        # Trim batch sizes to what we have
        batch_sizes = [b for b in batch_sizes if b <= len(files)]

    print(f'Found {len(files)} files:')
    for i, f in enumerate(files):
        hrs = f['duration'] / 3600
        print(f'  {i + 1:>2}. {f["filename"][:55]:<55} {hrs:.1f}h ({f["duration"]:.0f}s)')

    if args.dry_run:
        print('\n[DRY RUN] Would run these batches:')
        for bs in batch_sizes:
            print(f'  Batch {bs}: {", ".join(f["filename"][:30] for f in files[:bs])}')
        return

    # Authenticate
    print('\nAuthenticating...')
    token = get_valid_token()
    print('Authenticated.')

    # Connect to Redis
    r = redis.from_url(REDIS_URL, decode_responses=False)
    r.ping()
    print('Redis connected.')

    # Sequential mode: process each file individually (for duration curve testing)
    if args.sequential:
        batch_sizes = [1] * len(files)

    # Run batches
    all_batches: list[BatchResult] = []
    file_offset = 0
    for i, batch_size in enumerate(batch_sizes):
        if i > 0:
            print(f'\n  Cooling down {args.cooldown}s before next batch...')
            time.sleep(args.cooldown)

        # In sequential mode, advance through the file list one at a time
        if args.sequential:
            batch_files = files[file_offset : file_offset + 1]
            file_offset += 1
            if not batch_files:
                break
        else:
            batch_files = files

        batch_result = run_batch(
            token,
            r,
            batch_files,
            batch_size,
            args.gpu_id,
            upload_mode=args.upload,
            audio_dir=args.audio_dir,
            stats_containers=stats_containers,
        )
        all_batches.append(batch_result)

    # Write reports
    corpus_total_h = sum(f['duration'] for f in files) / 3600 if args.corpus_file else None
    corpus_label = Path(args.corpus_file).name if args.corpus_file else None
    write_reports(
        all_batches,
        output_dir,
        corpus_total_audio_h=corpus_total_h,
        corpus_name=corpus_label,
        run_label=args.run_label,
    )

    print(f'\nBenchmark complete at {_ts()}')


if __name__ == '__main__':
    main()
