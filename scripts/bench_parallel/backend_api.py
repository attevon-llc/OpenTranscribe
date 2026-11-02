"""Backend access: auth token, psql-over-docker queries, reprocess/upload/delete calls."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import requests

from bench_parallel.config import BACKEND_URL, DB_CONTAINER, DB_NAME, DB_USER


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


#: Uploaded benchmark files are named ``bench-parallel-<hex8><ext>`` (registered in
#: scripts/cleanup-test-data.py ``MEDIA_FILENAME_SPECS``). The corpus filename stays the
#: label in every report; only the name the BACKEND sees changes, so a leftover upload can be
#: told from a user's own recording that happens to share a corpus filename.
BENCH_UPLOAD_PREFIX = 'bench-parallel-'

#: uuids of files this run uploaded, deleted at the end unless --keep-uploads.
UPLOADED_UUIDS: list[str] = []


def _upload_name(original: str) -> str:
    return f'{BENCH_UPLOAD_PREFIX}{uuid4().hex[:8]}{Path(original).suffix.lower()}'


def delete_uploaded_files(token: str, uuids: list[str]) -> int:
    """Delete this run's uploads (API delete, ``/force`` when the file is mid-processing).

    Returns how many could NOT be deleted so the caller can say so plainly.
    """
    failed = 0
    headers = {'Authorization': f'Bearer {token}'}
    for file_uuid in uuids:
        try:
            resp = requests.delete(
                f'{BACKEND_URL}/api/files/{file_uuid}', headers=headers, timeout=60
            )
            if resp.status_code >= 400:
                resp = requests.delete(
                    f'{BACKEND_URL}/api/files/{file_uuid}/force', headers=headers, timeout=60
                )
            if resp.status_code >= 400 and resp.status_code != 404:
                failed += 1
                print(f'  CLEANUP FAILED for {file_uuid}: {resp.status_code}', file=sys.stderr)
        except requests.RequestException as e:
            failed += 1
            print(f'  CLEANUP ERROR for {file_uuid}: {e}', file=sys.stderr)
    return failed


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
                files={'file': (_upload_name(p.name), fh, content_type)},
                timeout=3600,
            )
        if resp.status_code >= 400:
            print(
                f'  UPLOAD FAILED for {p.name}: {resp.status_code} {resp.text[:200]}',
                file=sys.stderr,
            )
            return ''
        j = resp.json()
        new_uuid = j.get('uuid') or j.get('id') or ''
        if new_uuid:
            UPLOADED_UUIDS.append(str(new_uuid))
        return str(new_uuid)
    except requests.RequestException as e:
        print(f'  UPLOAD ERROR for {p.name}: {e}', file=sys.stderr)
        return ''
