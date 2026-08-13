#!/usr/bin/env python3
"""Self-contained client-side upload benchmark.

Works against either master or the branch — talks only to the REST API.
Uploads each fixture via the legacy multipart POST and times the wall
clock until status=completed. Emits a CSV so we can compare before/after.
"""

from __future__ import annotations

import csv
import hashlib
import mimetypes
import os
import sys
import time
from pathlib import Path

import requests

BACKEND = os.environ.get('BACKEND_URL', 'http://localhost:5174')
EMAIL = os.environ.get('BENCHMARK_EMAIL', 'admin@example.com')
PASSWORD = os.environ.get('BENCHMARK_PASSWORD', 'password')
POLL_INTERVAL = 3.0
POLL_TIMEOUT = 7200  # 2 hr per file


def login() -> str:
    r = requests.post(
        f'{BACKEND}/api/auth/login',
        data={'username': EMAIL, 'password': PASSWORD},
        timeout=30,
    )
    r.raise_for_status()
    return str(r.json()['access_token'])


def sha256_client(path: Path) -> tuple[str, float]:
    t = time.time()
    h = hashlib.sha256()
    with path.open('rb') as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest(), time.time() - t


def upload(token: str, path: Path, file_hash: str) -> tuple[str, float]:
    mime, _ = mimetypes.guess_type(path.name)
    mime = mime or 'application/octet-stream'
    headers = {'Authorization': f'Bearer {token}', 'X-File-Hash': file_hash}
    t = time.time()
    with path.open('rb') as fp:
        files = {'file': (path.name, fp, mime)}
        r = requests.post(
            f'{BACKEND}/api/files',
            headers=headers,
            files=files,
            timeout=3600,
        )
    r.raise_for_status()
    body = r.json()
    uuid = str(body.get('uuid') or body.get('id') or '')
    return uuid, time.time() - t


def poll(token: str, file_uuid: str) -> tuple[bool, str]:
    deadline = time.time() + POLL_TIMEOUT
    last_print = time.time()
    while time.time() < deadline:
        r = requests.get(
            f'{BACKEND}/api/files/{file_uuid}',
            headers={'Authorization': f'Bearer {token}'},
            timeout=20,
        )
        if r.status_code == 200:
            body = r.json()
            status = body.get('status', '')
            if status == 'completed':
                return True, 'ok'
            if status == 'error':
                return False, body.get('last_error_message', 'error')
            if time.time() - last_print > 30:
                elapsed = time.time() - (deadline - POLL_TIMEOUT)
                print(f'    … {status} ({elapsed:.0f}s)')
                last_print = time.time()
        time.sleep(POLL_INTERVAL)
    return False, 'timeout'


def main() -> None:
    fixtures_dir = Path(sys.argv[1] if len(sys.argv) > 1 else '/tmp/bench_fixtures')
    output_csv = sys.argv[2] if len(sys.argv) > 2 else '/tmp/baseline_upload.csv'

    if not fixtures_dir.is_dir():
        print(f'Dir not found: {fixtures_dir}', file=sys.stderr)
        sys.exit(1)

    token = login()
    print(f'Authenticated against {BACKEND}')

    fixtures = sorted(fixtures_dir.iterdir(), key=lambda p: p.stat().st_size)
    rows: list[dict] = []

    for fp in fixtures:
        name = fp.name
        size_mb = fp.stat().st_size / 1024 / 1024
        print(f'\n▶ {name} ({size_mb:.1f} MB)')

        fh, hash_s = sha256_client(fp)
        print(f'  hash {hash_s:.2f}s')

        t0 = time.time()
        try:
            file_uuid, put_s = upload(token, fp, fh)
        except Exception as e:
            print(f'  ✗ upload: {e}')
            continue
        if not file_uuid:
            print('  ✗ empty uuid')
            continue
        print(f'  http {put_s:.2f}s  uuid {file_uuid[:8]}…')

        ok, reason = poll(token, file_uuid)
        wall = time.time() - t0
        print(f'  {"✓ done" if ok else f"✗ {reason}"} in {wall:.1f}s')

        rows.append(
            {
                'fixture': name,
                'size_mb': round(size_mb, 1),
                'file_uuid': file_uuid,
                'client_hash_s': round(hash_s, 3),
                'http_put_s': round(put_s, 3),
                'end_to_end_wall_s': round(wall, 3),
                'status': 'ok' if ok else reason,
            }
        )

    with open(output_csv, 'w', newline='') as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                'fixture',
                'size_mb',
                'file_uuid',
                'client_hash_s',
                'http_put_s',
                'end_to_end_wall_s',
                'status',
            ],
        )
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print(f'\nCSV: {output_csv}\n')
    print(f'{"fixture":<22}{"size":>10}{"hash":>8}{"http":>8}{"end-to-end":>14}  status')
    for r in rows:
        print(
            f'{r["fixture"]:<22}{r["size_mb"]:>8.1f}MB{r["client_hash_s"]:>7.2f}s'
            f'{r["http_put_s"]:>7.2f}s{r["end_to_end_wall_s"]:>12.2f}s  {r["status"]}'
        )


if __name__ == '__main__':
    main()
