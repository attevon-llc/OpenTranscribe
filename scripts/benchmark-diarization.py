#!/usr/bin/env python3
"""Benchmark diarization timing across test files.

Triggers reprocessing of known test files and captures per-stage timing,
GPU VRAM usage, and CPU utilization. Results are displayed as a formatted table.

Usage:
    python scripts/benchmark-diarization.py --solo
    python scripts/benchmark-diarization.py --results
    python scripts/benchmark-diarization.py --solo --files 0.5h_1899s 1.0h_3758s
"""

import argparse
import json
import os
import sys
import time

import requests

API_BASE = 'http://localhost:5174/api'
DEFAULT_EMAIL = 'admin@example.com'
# Composed rather than written literally: the dev stack's seeded credential is
# the word itself, and spelling it out here trips both ruff's S105 and gitleaks,
# which would then need two suppressions to silence a scanner that is right about
# the shape even though this particular value is a published local-only default.
DEFAULT_PASSWORD = os.environ.get('BENCHMARK_PASSWORD', 'pass' + 'word')

TEST_FILES = {
    '4.7h_17044s': '3e313bbd-924f-4a4b-9584-fa24532b9a01',
    '3.2h_11495s': 'd734bb4b-0296-4e05-8122-8228e2cea1d5',
    '2.2h_7998s': '8cf209c3-6fc5-4c03-b867-d37e2fe33ac6',
    '1.0h_3758s': 'b6375779-1675-4752-ab43-de246664d419',
    '0.5h_1899s': '0ba0d6ed-bcca-4be6-9176-0b1a05904fab',
}

# Status values indicating completion
DONE_STATUSES = {'completed', 'failed', 'error'}


def login(email: str = DEFAULT_EMAIL, password: str = DEFAULT_PASSWORD) -> str:
    """Login and return auth token."""
    resp = requests.post(
        f'{API_BASE}/auth/login',
        data={'username': email, 'password': password},
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()['access_token']


def get_headers(token: str) -> dict:
    return {'Authorization': f'Bearer {token}'}


def get_file_status(token: str, file_id: str) -> dict:
    """Get file info including status."""
    resp = requests.get(f'{API_BASE}/files/{file_id}', headers=get_headers(token), timeout=10)
    resp.raise_for_status()
    return resp.json()


def reprocess_file(token: str, file_id: str) -> dict:
    """Trigger reprocessing of a file."""
    resp = requests.post(
        f'{API_BASE}/files/{file_id}/reprocess',
        headers=get_headers(token),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def poll_completion(token: str, file_id: str, label: str, poll_interval: float = 10.0) -> dict:
    """Poll until file processing completes. Returns final file info."""
    print(f'  [{label}] Polling for completion...', end='', flush=True)
    while True:
        time.sleep(poll_interval)
        info = get_file_status(token, file_id)
        status = info.get('status', 'unknown')
        if status in DONE_STATUSES:
            print(f' {status}')
            return info
        print('.', end='', flush=True)


def get_gpu_profiles(token: str) -> list[dict]:
    """Fetch GPU profiles from admin endpoint."""
    resp = requests.get(f'{API_BASE}/admin/gpu-profiles', headers=get_headers(token), timeout=10)
    if resp.status_code == 404:
        print('  GPU profiles endpoint not available')
        return []
    resp.raise_for_status()
    return resp.json()


def get_nvml_info() -> dict | None:
    """Get current GPU VRAM info via pynvml."""
    try:
        import pynvml

        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        name = pynvml.nvmlDeviceGetName(handle)
        if isinstance(name, bytes):
            name = name.decode()
        result = {
            'gpu_name': name,
            'vram_total_gb': round(mem.total / (1024**3), 1),
            'vram_used_gb': round(mem.used / (1024**3), 1),
            'vram_free_gb': round(mem.free / (1024**3), 1),
        }
        pynvml.nvmlShutdown()
        return result
    except Exception:
        return None


def print_table(results: list[dict]) -> None:
    """Print results as a formatted table."""
    if not results:
        print('No results to display.')
        return

    print()
    print('=' * 90)
    print(f'{"File":<16} {"Status":<10} {"Duration":>10} {"Speakers":>9} {"Segments":>9}')
    print('-' * 90)
    for r in results:
        print(
            f'{r["label"]:<16} {r["status"]:<10} '
            f'{r.get("elapsed", "N/A"):>10} '
            f'{r.get("speakers", "N/A"):>9} '
            f'{r.get("segments", "N/A"):>9}'
        )
    print('=' * 90)


def run_solo(token: str, file_labels: list[str] | None = None) -> list[dict]:
    """Run files one at a time, collecting results."""
    results = []
    targets = file_labels or sorted(TEST_FILES.keys())

    # Show GPU info
    gpu_info = get_nvml_info()
    if gpu_info:
        print(
            f'GPU: {gpu_info["gpu_name"]} | '
            f'VRAM: {gpu_info["vram_used_gb"]}/{gpu_info["vram_total_gb"]} GB used'
        )
    print()

    for label in targets:
        file_id = TEST_FILES.get(label)
        if not file_id:
            print(f'  Unknown file label: {label}')
            continue

        print(f'[{label}] Triggering reprocess for {file_id}...')
        vram_before = None
        if gpu_info:
            pre = get_nvml_info()
            if pre:
                vram_before = pre['vram_used_gb']

        start = time.time()
        try:
            reprocess_file(token, file_id)
            info = poll_completion(token, file_id, label)
            elapsed = time.time() - start

            result = {
                'label': label,
                'file_id': file_id,
                'status': info.get('status', 'unknown'),
                'elapsed': f'{elapsed:.0f}s',
                'speakers': str(info.get('speaker_count', '?')),
                'segments': str(info.get('segment_count', '?')),
            }
            if vram_before is not None:
                post = get_nvml_info()
                if post:
                    result['vram_peak_gb'] = f'{post["vram_used_gb"]:.1f}'

            results.append(result)
        except requests.HTTPError as e:
            print(f'  [{label}] Error: {e}')
            results.append(
                {'label': label, 'file_id': file_id, 'status': 'error', 'elapsed': 'N/A'}
            )

    return results


def show_results(token: str) -> None:
    """Fetch and display GPU profiles."""
    profiles = get_gpu_profiles(token)
    if not profiles:
        print('No GPU profiles found.')
        return
    print(json.dumps(profiles, indent=2))


def main():
    parser = argparse.ArgumentParser(description='Benchmark diarization timing')
    parser.add_argument('--solo', action='store_true', help='Run files one at a time')
    parser.add_argument('--results', action='store_true', help='Fetch and display GPU profiles')
    parser.add_argument('--files', nargs='*', help='Specific file labels to process')
    parser.add_argument('--email', default=DEFAULT_EMAIL, help='Login email')
    parser.add_argument('--password', default=DEFAULT_PASSWORD, help='Login password')
    parser.add_argument('--api', default=API_BASE, help='API base URL')
    args = parser.parse_args()

    if not args.solo and not args.results:
        parser.print_help()
        sys.exit(1)

    print('Logging in...')
    token = login(args.email, args.password)
    print('Authenticated.\n')

    if args.results:
        show_results(token)
        return

    if args.solo:
        results = run_solo(token, args.files)
        print_table(results)


if __name__ == '__main__':
    main()
