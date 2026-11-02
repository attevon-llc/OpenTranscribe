"""Task-id lookups through psql in the Postgres container."""

import time

import redis

from e2e_bench.config import DB_CONTAINER, DB_NAME, DB_USER


def db_query(sql: str) -> list[list[str]]:
    """Run a SQL query via docker exec and return rows."""
    import subprocess

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
        return []
    return [line.split('\t') for line in result.stdout.strip().split('\n') if line.strip()]


def get_task_id_for_file(file_uuid: str) -> str:
    """Get the active_task_id for a file from the DB (with retry)."""
    for _attempt in range(5):
        rows = db_query(f"SELECT active_task_id FROM media_file WHERE uuid = '{file_uuid}'")
        if rows and rows[0][0].strip():
            return rows[0][0].strip()
        time.sleep(1)
    return ''


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
    best_key = ''
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
    return ''
