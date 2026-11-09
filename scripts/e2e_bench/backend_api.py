"""Backend HTTP access: auth, file metadata, reprocess, upload and task polling."""

import logging
import os
import sys
import time

import requests

from e2e_bench.config import POLL_INTERVAL, POLL_TIMEOUT

logger = logging.getLogger(__name__)


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
    backend_url: str,
    file_uuid: str,
    token: str,
    verify: bool = True,
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


def trigger_reprocess(backend_url: str, file_uuid: str, token: str, verify: bool = True) -> bool:
    """Trigger reprocessing of a file. Returns True on success."""
    resp = requests.post(
        f'{backend_url}/api/files/{file_uuid}/reprocess',
        headers={'Authorization': f'Bearer {token}'},
        verify=verify,
    )
    resp.raise_for_status()
    return True


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
    except (requests.RequestException, ValueError):
        logger.warning('could not read active_task_id for %s', file_uuid, exc_info=True)
    return ''


def poll_task_completion(
    backend_url: str,
    file_uuid: str,
    token: str,
    timeout: int = POLL_TIMEOUT,
    verify: bool = True,
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
