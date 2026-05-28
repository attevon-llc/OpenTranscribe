#!/usr/bin/env python3
"""End-to-end transcription pipeline benchmark.

Triggers a reprocess via the API, polls Redis for timing data,
calculates stage durations + inter-stage gaps, runs N iterations,
and outputs a CSV + summary table + VRAM map.

Requires:
    - ENABLE_BENCHMARK_TIMING=true in .env
    - ENABLE_VRAM_PROFILING=true in .env (optional, for VRAM data)
    - Backend running and accessible at BACKEND_URL
    - Redis accessible at REDIS_URL

Usage:
    BENCHMARK_EMAIL=admin@example.com BENCHMARK_PASSWORD=password \\
        python scripts/benchmark_e2e.py --file-uuid <UUID> [--iterations 3] [--output benchmark_results.csv]

    # Detailed report with VRAM breakdown and realtime factor
    python scripts/benchmark_e2e.py --file-uuid <UUID> --detailed

    # Longer timeout for 3hr+ files (default 10min, set to 60min)
    python scripts/benchmark_e2e.py --file-uuid <UUID> --timeout 3600

Credentials are read from environment variables to avoid exposure in shell
history and process listings:
    BENCHMARK_EMAIL     Login email    (default: admin@example.com)
    BENCHMARK_PASSWORD  Login password (default: password)
"""

import argparse
import csv
import json
import os
import sys
import time

import redis
import requests

# Force unbuffered stdout for real-time output in background mode
sys.stdout.reconfigure(line_buffering=True)

DEFAULT_BACKEND_URL = 'http://localhost:5174'
POLL_INTERVAL = 3.0
POLL_TIMEOUT = 3600  # 60 minutes max (3hr files need time)


def _load_redis_url() -> str:
    """Build Redis URL from BENCHMARK_REDIS_URL env var, then .env, then sensible default."""
    explicit = os.environ.get('BENCHMARK_REDIS_URL', '')
    if explicit:
        return explicit
    # Try to read password and port from project .env
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


DEFAULT_REDIS_URL = _load_redis_url()
INTER_ITERATION_WAIT = 10  # seconds between iterations for cleanup


def get_auth_token(backend_url: str, email: str, password: str, verify: bool = True) -> str:
    """Authenticate and return JWT token."""
    resp = requests.post(
        f'{backend_url}/api/auth/token',
        data={'username': email, 'password': password},
        verify=verify,
    )
    resp.raise_for_status()
    return resp.json()['access_token']


def get_file_metadata(
    backend_url: str, file_uuid: str, token: str, verify: bool = True,
) -> dict:
    """Get file metadata (duration, filename, segments, speakers)."""
    resp = requests.get(
        f'{backend_url}/api/files/{file_uuid}',
        headers={'Authorization': f'Bearer {token}'},
        verify=verify,
    )
    if resp.status_code == 200:
        data = resp.json()
        return {
            'filename': data.get('filename', 'unknown'),
            'duration': data.get('duration', 0),
            'file_size': data.get('file_size', 0),
            'content_type': data.get('content_type', ''),
            'segment_count': data.get('segment_count', 0),
            'speaker_count': data.get('speaker_count', 0),
            'whisper_model': data.get('whisper_model', ''),
            'status': data.get('status', ''),
        }
    return {}


DB_CONTAINER = 'opentranscribe-postgres'
DB_USER = 'postgres'
DB_NAME = 'opentranscribe'


def _db_query(sql: str) -> list[list[str]]:
    """Run a SQL query via docker exec and return rows."""
    import subprocess
    result = subprocess.run(
        ['docker', 'exec', DB_CONTAINER, 'psql', '-U', DB_USER, '-d', DB_NAME,
         '-t', '-A', '-F', '\t', '-c', sql],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        return []
    return [line.split('\t') for line in result.stdout.strip().split('\n') if line.strip()]


def trigger_reprocess(backend_url: str, file_uuid: str, token: str, verify: bool = True) -> bool:
    """Trigger reprocessing of a file. Returns True on success."""
    resp = requests.post(
        f'{backend_url}/api/files/{file_uuid}/reprocess',
        headers={'Authorization': f'Bearer {token}'},
        verify=verify,
    )
    resp.raise_for_status()
    return True


def get_task_id_for_file(file_uuid: str) -> str:
    """Get the active_task_id for a file from the DB (with retry)."""
    for _attempt in range(5):
        rows = _db_query(
            f"SELECT active_task_id FROM media_file WHERE uuid = '{file_uuid}'"
        )
        if rows and rows[0][0].strip():
            return rows[0][0].strip()
        time.sleep(1)
    return ""


def find_benchmark_task_id(
    r: redis.Redis,
    dispatch_time: float,
    match_field: str = 'dispatch_timestamp',
    max_delta: float = 30.0,
) -> str:
    """Find the benchmark Redis key whose marker is closest to dispatch_time.

    By default matches on ``dispatch_timestamp`` (reprocess-mode compatible);
    upload-mode callers can pass ``match_field='http_request_received'`` to
    resolve the task_id using the earliest marker we control client-side.
    """
    best_key = ""
    best_delta = float('inf')

    for key in r.scan_iter(match='benchmark:*'):
        key_str = key.decode() if isinstance(key, bytes) else key
        ts_raw = r.hget(key, match_field)
        if not ts_raw:
            continue
        ts = float(ts_raw.decode() if isinstance(ts_raw, bytes) else ts_raw)
        delta = abs(ts - dispatch_time)
        if delta < best_delta:
            best_delta = delta
            best_key = key_str.replace('benchmark:', '')

    if best_delta < max_delta:
        return best_key
    return ""


def upload_file_via_api(
    backend_url: str,
    token: str,
    fixture_path: str,
    verify: bool = True,
    extra_headers: dict | None = None,
) -> tuple[float, dict, float]:
    """POST ``fixture_path`` to ``/api/files`` (legacy upload flow).

    Returns (client_hash_duration_s, response_json, http_response_elapsed_s).
    Computes a SHA-256 hash client-side to mirror the real browser flow and
    so backend duplicate detection behaves identically.
    """
    import hashlib
    import mimetypes

    path = os.path.abspath(fixture_path)
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    filename = os.path.basename(path)
    content_type, _ = mimetypes.guess_type(filename)
    content_type = content_type or 'application/octet-stream'

    # Client-side SHA-256 hash (matches uploadService.ts behavior)
    hash_start = time.time()
    h = hashlib.sha256()
    with open(path, 'rb') as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b''):
            h.update(chunk)
    file_hash = h.hexdigest()
    hash_elapsed = time.time() - hash_start

    headers = {
        'Authorization': f'Bearer {token}',
        'X-File-Hash': file_hash,
    }
    if extra_headers:
        headers.update(extra_headers)

    put_start = time.time()
    with open(path, 'rb') as fp:
        files = {'file': (filename, fp, content_type)}
        resp = requests.post(
            f'{backend_url}/api/files',
            headers=headers,
            files=files,
            verify=verify,
            timeout=3600,
        )
    put_elapsed = time.time() - put_start
    resp.raise_for_status()
    return hash_elapsed, resp.json(), put_elapsed


def get_task_id_for_file_via_api(
    backend_url: str, file_uuid: str, token: str, verify: bool = True
) -> str:
    """Read active_task_id from the API (works outside of Docker host)."""
    try:
        resp = requests.get(
            f'{backend_url}/api/files/{file_uuid}',
            headers={'Authorization': f'Bearer {token}'},
            verify=verify,
            timeout=15,
        )
        if resp.status_code == 200:
            return str(resp.json().get('active_task_id') or '')
    except Exception:
        pass
    return ''


def poll_task_completion(
    backend_url: str, file_uuid: str, token: str,
    timeout: int = POLL_TIMEOUT, verify: bool = True,
) -> bool:
    """Poll until the file status is completed or error."""
    deadline = time.time() + timeout
    last_print = time.time()
    while time.time() < deadline:
        resp = requests.get(
            f'{backend_url}/api/files/{file_uuid}',
            headers={'Authorization': f'Bearer {token}'},
            verify=verify,
        )
        if resp.status_code == 200:
            status = resp.json().get('status', '')
            if status == 'completed':
                return True
            if status == 'error':
                err = resp.json().get('last_error_message', 'unknown')
                print(f'  ERROR: File reached error status: {err}', file=sys.stderr)
                return False
            # Progress update every 30s
            if time.time() - last_print > 30:
                elapsed = time.time() - (deadline - timeout)
                print(f'  ... still processing ({status}, {elapsed:.0f}s elapsed)')
                last_print = time.time()
        time.sleep(POLL_INTERVAL)
    print(f'  TIMEOUT: Poll exceeded {timeout}s', file=sys.stderr)
    return False


def collect_benchmark_data(r: redis.Redis, task_id: str) -> dict:
    """Collect all benchmark timing data from Redis."""
    key = f'benchmark:{task_id}'
    raw = r.hgetall(key)
    return {k.decode(): float(v.decode()) for k, v in raw.items()}


def collect_vram_data(r: redis.Redis, task_id: str) -> dict | None:
    """Collect VRAM profiler data from Redis."""
    key = f'gpu:profile:{task_id}'
    raw = r.get(key)
    if raw:
        return json.loads(raw)
    return None


def calculate_stages(data: dict) -> dict:
    """Calculate stage durations and inter-stage gaps.

    Covers every marker added by the Phase 1 timing audit plan plus the five
    original markers (dispatch_timestamp, preprocess_end, gpu_received,
    gpu_end, postprocess_received). Missing markers are simply skipped so the
    legacy reprocess flow (which only populates the original five) still
    reports clean numbers.
    """
    stages: dict[str, float] = {}

    def _d(end_key: str, start_key: str) -> None:
        start = data.get(start_key)
        end = data.get(end_key)
        if start and end and end >= start:
            stages[f'{start_key}__to__{end_key}'] = end - start

    # ---------- Stage 0-8: client + API ingress ----------
    client_hash_s = data.get('client_hash_start')
    client_hash_e = data.get('client_hash_end')
    if client_hash_s and client_hash_e:
        stages['client_hash_duration'] = client_hash_e - client_hash_s

    http_start = data.get('http_request_received')
    http_read = data.get('http_read_complete')
    http_valid = data.get('http_validation_end')
    minio_s = data.get('minio_put_start')
    minio_e = data.get('minio_put_end')
    thumb_s = data.get('thumbnail_start')
    thumb_e = data.get('thumbnail_end')
    db_s = data.get('db_commit_start')
    db_e = data.get('db_commit_end')
    http_end = data.get('http_response_end')
    dispatch = data.get('dispatch_timestamp')

    if http_start and http_read:
        stages['http_read_duration'] = http_read - http_start
    if http_read and http_valid:
        stages['http_validation_duration'] = http_valid - http_read
    if minio_s and minio_e:
        stages['minio_put_duration'] = minio_e - minio_s
    if thumb_s and thumb_e:
        stages['thumbnail_duration'] = thumb_e - thumb_s
    if db_s and db_e:
        stages['db_commit_duration'] = db_e - db_s
    if http_start and http_end:
        stages['http_total_duration'] = http_end - http_start

    # ---------- Stage 9-10: queue wait + preprocess ----------
    pre_pre = data.get('preprocess_task_prerun')
    pre_end = data.get('preprocess_end')
    if dispatch and pre_pre:
        stages['dispatch_to_preprocess_prerun'] = pre_pre - dispatch
    if pre_pre and pre_end:
        stages['preprocess_body_duration'] = pre_end - pre_pre
    if dispatch and pre_end:
        stages['preprocess_duration'] = pre_end - dispatch

    for name in ('media_download', 'ffmpeg', 'metadata', 'temp_upload'):
        _d(f'{name}_end', f'{name}_start')

    # ---------- Stage 11-12: GPU ----------
    gpu_recv = data.get('gpu_received')
    gpu_prerun = data.get('gpu_task_prerun')
    gpu_end = data.get('gpu_end')

    if pre_end and gpu_recv:
        stages['preprocess_to_gpu_gap'] = gpu_recv - pre_end
    if gpu_prerun and gpu_recv:
        stages['gpu_prerun_skew'] = abs(gpu_prerun - gpu_recv)
    if gpu_recv and gpu_end:
        stages['gpu_duration'] = gpu_end - gpu_recv

    _d('gpu_audio_load_end', 'gpu_audio_load_start')

    # ---------- Stage 13-14: postprocess ----------
    post_recv = data.get('postprocess_received')
    post_pre = data.get('postprocess_task_prerun')
    post_end = data.get('postprocess_end')
    completion = data.get('completion_notified')

    if gpu_end and post_recv:
        stages['gpu_to_postprocess_gap'] = post_recv - gpu_end
    if post_recv and post_end:
        stages['postprocess_duration'] = post_end - post_recv

    # ---------- Stage 16: user-perceived done ----------
    perceived_start = http_start or dispatch
    if perceived_start and completion:
        stages['user_perceived_duration'] = completion - perceived_start

    # ---------- Stage 17-22: async enrichment ----------
    for name in (
        'search_index',
        'search_index_chunks',
        'speaker_upsert',
        'waveform',
        'clustering',
        'summary',
    ):
        _d(f'{name}_end', f'{name}_start')

    # ---------- Stage 21: fully indexed done ----------
    async_ends = [
        data.get(k)
        for k in (
            'search_index_chunks_end',
            'search_index_end',
            'speaker_upsert_end',
            'waveform_end',
            'clustering_end',
            'summary_end',
            'post_end',
        )
        if data.get(k) is not None
    ]
    if perceived_start and async_ends:
        stages['fully_indexed_duration'] = max(async_ends) - perceived_start

    # ---------- Legacy totals kept for back-compat with older CSVs ----------
    if dispatch and gpu_end:
        stages['total_dispatch_to_gpu_end'] = gpu_end - dispatch
    if dispatch and post_recv:
        stages['total_dispatch_to_postprocess'] = post_recv - dispatch

    return stages


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def _fmt_duration(seconds: float) -> str:
    """Format seconds into human-readable duration."""
    if seconds < 60:
        return f'{seconds:.1f}s'
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f'{m}m{s:02d}s'
    h, m = divmod(m, 60)
    return f'{h}h{m:02d}m{s:02d}s'


def _fmt_size(bytes_val: int) -> str:
    """Format bytes into human-readable size."""
    if bytes_val < 1024:
        return f'{bytes_val}B'
    if bytes_val < 1024 ** 2:
        return f'{bytes_val / 1024:.1f}KB'
    if bytes_val < 1024 ** 3:
        return f'{bytes_val / (1024 ** 2):.1f}MB'
    return f'{bytes_val / (1024 ** 3):.2f}GB'


# ---------------------------------------------------------------------------
# Summary printers
# ---------------------------------------------------------------------------
def print_summary(all_results: list[dict]) -> None:
    """Print a compact summary table of benchmark results."""
    if not all_results:
        print('No results to summarize.')
        return

    stage_keys = set()
    for r in all_results:
        stage_keys.update(r.get('stages', {}).keys())
    stage_keys = sorted(stage_keys)

    print('\n' + '=' * 70)
    print('BENCHMARK SUMMARY')
    print('=' * 70)
    print(f'{"Stage":<35} {"Mean (s)":>10} {"Min (s)":>10} {"Max (s)":>10}')
    print('-' * 70)

    for key in stage_keys:
        values = [r['stages'][key] for r in all_results if key in r.get('stages', {})]
        if values:
            mean = sum(values) / len(values)
            print(f'{key:<35} {mean:>10.3f} {min(values):>10.3f} {max(values):>10.3f}')

    print('=' * 70)


def print_detailed_report(
    all_results: list[dict],
    all_vram: list[dict],
    file_meta: dict,
    file_uuid: str,
) -> None:
    """Print comprehensive detailed benchmark report."""
    if not all_results:
        print('No results to summarize.')
        return

    n = len(all_results)
    filename = file_meta.get('filename', 'unknown')
    audio_duration = file_meta.get('duration', 0)
    file_size = file_meta.get('file_size', 0)
    segment_count = file_meta.get('segment_count', 0)
    speaker_count = file_meta.get('speaker_count', 0)
    whisper_model = file_meta.get('whisper_model', 'unknown')

    # Calculate mean/min/max for each stage
    def _stats(key: str) -> tuple[float, float, float]:
        vals = [r['stages'][key] for r in all_results if key in r.get('stages', {})]
        if not vals:
            return 0, 0, 0
        return sum(vals) / len(vals), min(vals), max(vals)

    pre_mean, pre_min, pre_max = _stats('preprocess_duration')
    gap1_mean, _, _ = _stats('preprocess_to_gpu_gap')
    gpu_mean, gpu_min, gpu_max = _stats('gpu_duration')
    gap2_mean, _, _ = _stats('gpu_to_postprocess_gap')
    total_mean, total_min, total_max = _stats('total_dispatch_to_postprocess')

    # If total not available, estimate from components
    if total_mean == 0:
        total_mean = pre_mean + gap1_mean + gpu_mean + gap2_mean
        total_min = pre_min + gpu_min
        total_max = pre_max + gpu_max

    # Percentages
    def _pct(val: float) -> str:
        return f'{(val / total_mean * 100):.1f}%' if total_mean > 0 else 'N/A'

    # Realtime factor
    rt_factor = audio_duration / gpu_mean if gpu_mean > 0 else 0
    rt_total = audio_duration / total_mean if total_mean > 0 else 0

    # WAV size estimate (16kHz mono PCM_S16LE)
    wav_size_mb = (audio_duration * 16000 * 2) / (1024 ** 2) if audio_duration > 0 else 0

    print('\n' + '=' * 76)
    print('SINGLE FILE BENCHMARK REPORT')
    print('=' * 76)
    print(f'File:            {filename}')
    print(f'UUID:            {file_uuid}')
    print(f'Audio Duration:  {_fmt_duration(audio_duration)} ({audio_duration:.0f}s)')
    print(f'File Size:       {_fmt_size(file_size)}')
    print(f'WAV Size (est):  {wav_size_mb:.0f} MB (16kHz mono PCM_S16LE)')
    print(f'Whisper Model:   {whisper_model}')
    print(f'Iterations:      {n}')

    print(f'\n{"PIPELINE STAGE BREAKDOWN":}')
    print('-' * 76)
    print(f'{"Stage":<40} {"Mean":>8} {"Min":>8} {"Max":>8} {"% Total":>8}')
    print('-' * 76)
    print(f'{"1. CPU Preprocess":<40} {pre_mean:>7.1f}s {pre_min:>7.1f}s {pre_max:>7.1f}s {_pct(pre_mean):>8}')
    print(f'{"2. Queue: CPU -> GPU":<40} {gap1_mean:>7.1f}s {"":>8} {"":>8} {_pct(gap1_mean):>8}')
    print(f'{"3. GPU Transcribe + Diarize":<40} {gpu_mean:>7.1f}s {gpu_min:>7.1f}s {gpu_max:>7.1f}s {_pct(gpu_mean):>8}')
    print(f'{"4. Queue: GPU -> Postprocess":<40} {gap2_mean:>7.1f}s {"":>8} {"":>8} {_pct(gap2_mean):>8}')
    print('-' * 76)
    print(f'{"TOTAL (dispatch to postprocess)":<40} {total_mean:>7.1f}s {total_min:>7.1f}s {total_max:>7.1f}s {"100.0%":>8}')

    # VRAM profile from the last successful run
    valid_vram = [v for v in all_vram if v is not None]
    if valid_vram:
        last = valid_vram[-1]
        steps = last.get('steps', [])

        if steps:
            print(f'\n{"VRAM PROFILE (last run)":}')
            print('-' * 76)
            print(f'{"Step":<30} {"Duration":>10} {"Before MB":>10} {"After MB":>10} {"Delta MB":>10}')
            print('-' * 76)

            # Group steps: snapshots (duration=0) and timed steps
            snapshots = [s for s in steps if s.get('name', '').startswith('snapshot:')]
            timed = [s for s in steps if not s.get('name', '').startswith('snapshot:')]

            for step in timed:
                name = step.get('name', 'unknown')
                dur = step.get('duration_s', 0)
                before = step.get('device_used_before_mb', 0)
                after = step.get('device_used_after_mb', 0)
                delta = step.get('device_delta_mb', 0)
                sign = '+' if delta >= 0 else ''
                print(f'{name:<30} {_fmt_duration(dur):>10} {before:>9.0f} {after:>9.0f} {sign}{delta:>9.0f}')

            # Summary metrics from report
            peak_device = last.get('peak_device_used_mb', 0)
            peak_pytorch = last.get('peak_pytorch_mb', 0)
            total_prof_dur = last.get('total_duration_s', 0)
            audio_dur_prof = last.get('audio_duration_s', 0)
            num_spk = last.get('num_speakers', 0)

            print(f'\n  Peak device VRAM:     {peak_device:.0f} MB')
            print(f'  Peak PyTorch alloc:   {peak_pytorch:.0f} MB')
            if total_prof_dur > 0:
                print(f'  Profiled duration:    {_fmt_duration(total_prof_dur)}')
            if num_spk > 0:
                print(f'  Speakers detected:    {num_spk}')

            # Extract sub-stage timings from VRAM profile
            transcription_step = next((s for s in timed if s['name'] == 'transcription'), None)
            diarization_step = next((s for s in timed if s['name'] == 'diarization'), None)
            model_load_step = next((s for s in timed if s['name'] == 'model_load_transcriber'), None)
            speaker_step = next((s for s in timed if s['name'] == 'speaker_assignment'), None)

            if transcription_step or diarization_step:
                print(f'\n{"GPU SUB-STAGE BREAKDOWN":}')
                print('-' * 76)
                print(f'{"Sub-stage":<30} {"Duration":>10} {"% of GPU":>10} {"Realtime":>10}')
                print('-' * 76)

                if model_load_step:
                    dur = model_load_step['duration_s']
                    pct = (dur / gpu_mean * 100) if gpu_mean > 0 else 0
                    print(f'{"  Model load/warmup":<30} {_fmt_duration(dur):>10} {pct:>9.1f}% {"":>10}')

                if transcription_step:
                    dur = transcription_step['duration_s']
                    pct = (dur / gpu_mean * 100) if gpu_mean > 0 else 0
                    rt = audio_duration / dur if dur > 0 else 0
                    print(f'{"  Whisper transcription":<30} {_fmt_duration(dur):>10} {pct:>9.1f}% {rt:>9.1f}x')

                if diarization_step:
                    dur = diarization_step['duration_s']
                    pct = (dur / gpu_mean * 100) if gpu_mean > 0 else 0
                    rt = audio_duration / dur if dur > 0 else 0
                    print(f'{"  PyAnnote diarization":<30} {_fmt_duration(dur):>10} {pct:>9.1f}% {rt:>9.1f}x')

                if speaker_step:
                    dur = speaker_step['duration_s']
                    pct = (dur / gpu_mean * 100) if gpu_mean > 0 else 0
                    print(f'{"  Speaker assignment":<30} {_fmt_duration(dur):>10} {pct:>9.1f}% {"":>10}')

                # Accounted vs unaccounted GPU time
                accounted = sum(
                    s['duration_s'] for s in timed
                    if s['name'] in ('model_load_transcriber', 'transcription', 'diarization', 'speaker_assignment')
                )
                unaccounted = gpu_mean - accounted
                if unaccounted > 1:
                    pct = (unaccounted / gpu_mean * 100) if gpu_mean > 0 else 0
                    print(f'{"  Other (DB save, cleanup)":<30} {_fmt_duration(unaccounted):>10} {pct:>9.1f}% {"":>10}')

    else:
        print('\n  (No VRAM profile data — set ENABLE_VRAM_PROFILING=true in .env)')

    # Performance metrics
    print(f'\n{"PERFORMANCE METRICS":}')
    print('-' * 76)
    print(f'  Realtime Factor (GPU only):  {rt_factor:.1f}x '
          f'({_fmt_duration(audio_duration)} audio in {_fmt_duration(gpu_mean)})')
    print(f'  Realtime Factor (total):     {rt_total:.1f}x '
          f'({_fmt_duration(audio_duration)} audio in {_fmt_duration(total_mean)})')
    gpu_util = (gpu_mean / total_mean * 100) if total_mean > 0 else 0
    print(f'  GPU Utilization:             {gpu_util:.1f}% (GPU time / total wall clock)')
    print(f'  Segments:                    {segment_count}')
    print(f'  Speakers:                    {speaker_count}')

    # Projection for 1400 files
    if rt_factor > 0:
        print(f'\n{"QUICK PROJECTIONS (based on this file)":}')
        print('-' * 76)
        print(f'  If avg file = {_fmt_duration(audio_duration)} at {rt_factor:.1f}x realtime:')
        for workers in [1, 2, 4, 5, 9]:
            # Sub-linear scaling: ~15% overhead per additional concurrent task
            if workers == 1:
                eff = 1.0
            else:
                eff = workers * (1 / (1 + 0.15 * (workers - 1)))
            time_per_file = total_mean / eff
            total_1400 = 1400 * time_per_file
            print(f'    {workers} worker(s):  ~{_fmt_duration(total_1400)} for 1400 files '
                  f'({total_1400 / 3600:.1f} hours)')

    print('=' * 76)


def print_vram_summary(all_vram: list[dict]) -> None:
    """Print VRAM profiler summary (compact mode)."""
    valid = [v for v in all_vram if v is not None]
    if not valid:
        print('\n  (No VRAM profile data — set ENABLE_VRAM_PROFILING=true in .env)')
        return

    print('\n' + '=' * 70)
    print('VRAM PROFILE (last run)')
    print('=' * 70)

    last = valid[-1]
    steps = last.get('steps', [])

    # VRAMProfiler.get_report() returns steps as a list of dicts
    if isinstance(steps, list) and steps:
        # Separate snapshots and timed steps
        timed = [s for s in steps if not s.get('name', '').startswith('snapshot:')]
        snapshots = [s for s in steps if s.get('name', '').startswith('snapshot:')]

        if snapshots:
            print(f'\n{"Snapshot":<35} {"Device Used (MB)":>17} {"Free (MB)":>12}')
            print('-' * 65)
            for snap in snapshots:
                name = snap.get('name', '').replace('snapshot:', '')
                used = snap.get('device_used_after_mb', 0)
                free = snap.get('device_free_mb', 0)
                print(f'{name:<35} {used:>17.0f} {free:>12.0f}')

        if timed:
            print(f'\n{"Step":<30} {"Duration (s)":>12} {"Peak VRAM (MB)":>15}')
            print('-' * 60)
            for step in timed:
                name = step.get('name', 'unknown')
                dur = step.get('duration_s', 0)
                peak = step.get('pt_peak_mb', 0)
                print(f'{name:<30} {dur:>12.3f} {peak:>15.0f}')

        # Derived metrics from snapshots
        snap_map = {s['name'].replace('snapshot:', ''): s for s in snapshots}
        ps = snap_map.get('pipeline_start', {})
        at = snap_map.get('after_transcriber_loaded', {})
        mw = snap_map.get('models_warm_no_inference', {})

        if ps and at:
            whisper_vram = at.get('device_used_after_mb', 0) - ps.get('device_used_after_mb', 0)
            print(f'\nWhisper warm VRAM:  {whisper_vram:.0f} MB')

        if at and mw:
            pyannote_vram = mw.get('device_used_after_mb', 0) - at.get('device_used_after_mb', 0)
            print(f'PyAnnote warm VRAM: {pyannote_vram:.0f} MB')

    elif isinstance(steps, dict) and steps:
        # Legacy dict format (shouldn't happen with current VRAMProfiler, but safe)
        print(f'\n{"Step":<30} {"Duration (s)":>12} {"Peak VRAM (MB)":>15}')
        print('-' * 60)
        for name, step in steps.items():
            dur = step.get('duration_s', 0)
            peak = step.get('peak_allocated_mb', step.get('pt_peak_mb', 0))
            print(f'{name:<30} {dur:>12.3f} {peak:>15.0f}')

    print('=' * 70)


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
    parser.add_argument('--mode', choices=['reprocess', 'upload'], default='reprocess',
                        help='reprocess: re-run pipeline on existing file (default). '
                             'upload: POST a fixture to /api/files end-to-end.')
    parser.add_argument('--file-uuid',
                        help='UUID of file to benchmark (required for --mode reprocess)')
    parser.add_argument('--fixture-file',
                        help='Path to a file to upload (required for --mode upload)')
    parser.add_argument('--flow', choices=['legacy', 'presigned'], default='legacy',
                        help='Upload flow to use when --mode upload. legacy: POST to '
                             '/api/files. presigned: new direct-to-MinIO flow (future).')
    parser.add_argument('--iterations', type=int, default=3,
                        help='Number of iterations (default: 3). In upload mode each '
                             'iteration creates a new MediaFile row.')
    parser.add_argument('--output', default='benchmark_results.csv', help='Output CSV path')
    parser.add_argument('--backend-url', default=DEFAULT_BACKEND_URL, help='Backend URL')
    parser.add_argument('--redis-url', default=DEFAULT_REDIS_URL, help='Redis URL')
    parser.add_argument('--timeout', type=int, default=POLL_TIMEOUT,
                        help=f'Max seconds to wait for completion (default: {POLL_TIMEOUT})')
    parser.add_argument('--detailed', action='store_true',
                        help='Print detailed report with VRAM breakdown and realtime factor')
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
            print(f'  Duration: {_fmt_duration(dur)} ({dur:.0f}s)')
            print(f'  Size: {_fmt_size(file_meta.get("file_size", 0))}')
        else:
            print(f'  WARNING: Could not fetch file metadata', file=sys.stderr)

    r = redis.from_url(args.redis_url, decode_responses=False)

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
            except Exception as e:
                print(f'  Upload failed: {e}', file=sys.stderr)
                continue
            current_file_uuid = str(upload_resp.get('uuid') or upload_resp.get('id') or '')
            if not current_file_uuid:
                print(f'  Upload returned no UUID', file=sys.stderr)
                continue
            print(f'  Uploaded file {current_file_uuid} '
                  f'(hash={client_hash_elapsed:.1f}s, put={put_elapsed:.1f}s)')
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
            args.backend_url, current_file_uuid, token,
            timeout=args.timeout, verify=verify,
        )
        wall_elapsed = time.time() - wall_start

        if not success:
            print(f'  Skipping iteration {i + 1} (failed/timeout)')
            continue

        print(f'  Completed in {_fmt_duration(wall_elapsed)}')

        # Brief delay for Redis data propagation
        time.sleep(3)

        # Resolve benchmark task_id if not already known
        if not task_id:
            resolved_id = find_benchmark_task_id(r, dispatch_time)
            if resolved_id:
                task_id = resolved_id
            else:
                print(f'  WARNING: Could not resolve task_id from Redis or DB')
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
            print(f'  Preprocess: {pre_dur:.1f}s | GPU: {gpu_dur:.1f}s | '
                  f'Gaps: {stages.get("preprocess_to_gpu_gap", 0):.1f}s + '
                  f'{stages.get("gpu_to_postprocess_gap", 0):.1f}s')
        if vram_data:
            print(f'  VRAM profile: {len(vram_data.get("steps", []))} steps captured')
        else:
            print(f'  VRAM profile: None (enable ENABLE_VRAM_PROFILING=true)')

    # Refresh file metadata after processing (to get segment/speaker counts)
    if all_results:
        last_uuid = all_results[-1]['file_uuid']
        file_meta = get_file_metadata(args.backend_url, last_uuid, token, verify=verify)

    # Write CSV
    if all_results:
        stage_keys = set()
        for r_item in all_results:
            stage_keys.update(r_item.get('stages', {}).keys())
        stage_keys = sorted(stage_keys)

        with open(args.output, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(
                ['iteration', 'task_id', 'file_uuid', 'wall_elapsed'] + stage_keys
            )
            for r_item in all_results:
                row = [
                    r_item['iteration'],
                    r_item['task_id'],
                    r_item.get('file_uuid', ''),
                    f"{r_item['wall_elapsed']:.3f}",
                ]
                row += [r_item['stages'].get(k, '') for k in stage_keys]
                writer.writerow(row)

        print(f'\nCSV written to {args.output}')

    # Print report
    last_uuid = all_results[-1]['file_uuid'] if all_results else (args.file_uuid or '')
    if args.detailed:
        print_detailed_report(all_results, all_vram, file_meta, last_uuid)
    else:
        print_summary(all_results)
        print_vram_summary(all_vram)


if __name__ == '__main__':
    main()
