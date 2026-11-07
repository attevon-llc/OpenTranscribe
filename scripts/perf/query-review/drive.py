#!/usr/bin/env python3
"""Deterministic endpoint driver for the #1337 query review (plan section 4.3 step 2).

Logs in as three seeded identities and calls each endpoint ``--repeat`` times, writing one CSV
row per call. The backend's access log (``... METHOD route status Xms user=U dbq=N``, from
``app/middleware/observability.py``) is the source of the per-request statement count: capture
``./opentr.sh logs backend`` (or ``docker logs``) of the measured stack into a file and run

    drive.py summarise --calls calls.csv --access-log access.log > summary.csv

Run (stack must be the isolated ``--fresh`` one; the driver refuses the dev stack's port):

    drive.py run --base-url http://localhost:5476 --out DIR [--repeat 20] [--list]

Needs ``httpx`` (backend venv). Seed identities: ``scripts/seed-query-review.sh``.
Paths were taken from the live route table (``app.main.app.routes``); ``run --check-routes``
compares them with ``/openapi.json`` so a renamed route fails loudly instead of 404ing 20 times.
"""

from __future__ import annotations

import argparse
import csv
import re
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

PASSWORD = 'loadtest-password'
IDENTITIES = {  # seeded by app.scripts.scale_dataset: 1 admin, 2 heavy owner, 3 ordinary user
    'admin': 'perf-1@loadtest.invalid',
    'heavy': 'perf-2@loadtest.invalid',
    'member': 'perf-3@loadtest.invalid',
}
LIVE_DEV_PORTS = {5174, 5175, 5176, 5177, 5178, 5179, 5180}
PAGE = '/api/files'
USER_CALLS: list[tuple[str, str, dict[str, object]]] = [
    ('files.default', PAGE, {}),
    ('files.page50', PAGE, {'page_size': 50}),
    ('files.search', PAGE, {'search': 'loadtest-1'}),
    ('files.tag', PAGE, {'tag': 'loadtest-tag-1'}),
    ('files.speaker', PAGE, {'speaker': 'Perf Person 1'}),
    ('files.held_true', PAGE, {'held': 'true'}),
    ('files.held_false', PAGE, {'held': 'false'}),
    ('files.hidden_only', PAGE, {'hidden': 'only'}),
    ('files.sort_upload_time', PAGE, {'sort_by': 'upload_time'}),
    ('files.sort_duration', PAGE, {'sort_by': 'duration'}),
    ('files.sort_file_size', PAGE, {'sort_by': 'file_size'}),
    ('files.sort_completed_at', PAGE, {'sort_by': 'completed_at'}),
    ('files.metadata_filters', '/api/files/metadata-filters', {}),
    ('files.owners', '/api/files/owners', {}),
    ('files.trash', '/api/files/trash', {}),
    ('search', '/api/search', {'q': 'loadtest'}),
    ('speakers.list', '/api/speakers', {}),
    ('speakers.for_filter', '/api/speakers', {'for_filter': 'true', 'q': ''}),
    ('profiles.list', '/api/speaker-profiles/profiles', {}),
    ('clusters.list', '/api/speaker-clusters', {}),
    ('clusters.inbox', '/api/speaker-clusters/unverified/inbox', {}),
    ('clusters.stats', '/api/speaker-clusters/stats', {}),
    ('tasks.list', '/api/tasks', {}),
]
ADMIN_ONLY: list[tuple[str, str, dict[str, object]]] = [
    ('tasks.system_health', '/api/tasks/system/health', {}),
    ('admin.stats', '/api/admin/stats', {}),
    ('system.stats', '/api/system/stats', {}),
    ('admin.holds', '/api/admin/holds', {}),
    ('admin.quarantined', '/api/admin/files/quarantined', {}),
]
# {profile_uuid} / {speaker_uuid} are filled from the first row each list returns.
TEMPLATED: list[tuple[str, str, str]] = [
    ('profiles.occurrences', '/api/speaker-profiles/profiles/{profile}/occurrences', 'profile'),
    ('speakers.cross_media', '/api/speakers/{speaker}/cross-media', 'speaker'),
]
ACCESS_RE = re.compile(
    r'(?P<m>GET|POST|PUT|PATCH|DELETE) (?P<route>\S+) (?P<status>\d{3}) '
    r'(?P<ms>[\d.]+)ms user=(?P<user>\S+) dbq=(?P<dbq>\d+)'
)


def _client(base_url: str, email: str):
    import httpx

    client = httpx.Client(base_url=base_url, timeout=120.0, follow_redirects=False)
    resp = client.post('/api/auth/login', data={'username': email, 'password': PASSWORD})
    resp.raise_for_status()  # the session is an httpOnly cookie, kept by the client jar
    return client


def _first_uuid(client, path: str, key: str) -> str | None:
    body = client.get(path).json()
    rows = body if isinstance(body, list) else body.get('items') or body.get('results') or []
    return rows[0].get(key) or rows[0].get('uuid') if rows else None


def run(args: argparse.Namespace) -> int:
    port = urlparse(args.base_url).port
    if port in LIVE_DEV_PORTS:
        print(
            f'Refusing {args.base_url}: that is the shared dev stack. Use a --fresh stack.',
            file=sys.stderr,
        )
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.check_routes:
        import httpx

        paths = set(httpx.get(args.base_url + '/openapi.json').json()['paths'])
        wanted = {p for _, p, _ in USER_CALLS + ADMIN_ONLY}
        wanted |= {p.split('{')[0].rstrip('/') for _, p, _ in TEMPLATED}
        missing = sorted(
            w for w in wanted if w not in paths and not any(x.startswith(w) for x in paths)
        )
        print('missing routes:', missing or 'none')
        return 1 if missing else 0

    rows: list[dict[str, object]] = []
    for who, email in IDENTITIES.items():
        client = _client(args.base_url, email)
        calls = list(USER_CALLS) + (ADMIN_ONLY if who == 'admin' else [])
        for name, path, extra in TEMPLATED:
            ident = _first_uuid(
                client,
                '/api/speaker-profiles/profiles' if extra == 'profile' else '/api/speakers',
                'uuid',
            )
            if ident:
                calls.append((name, path.replace('{' + extra + '}', ident), {}))
        for name, path, params in calls:
            for i in range(args.repeat):
                started = time.perf_counter()
                resp = client.get(path, params=params)
                rows.append(
                    {
                        'identity': who,
                        'name': name,
                        'path': path,
                        'iteration': i,
                        'status': resp.status_code,
                        'ms': round((time.perf_counter() - started) * 1000, 1),
                        'bytes': len(resp.content),
                    }
                )
    with (out / 'calls.csv').open('w', newline='') as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    bad = [r for r in rows if int(r['status']) >= 400]
    print(f'{len(rows)} calls -> {out / "calls.csv"}; non-2xx/3xx: {len(bad)}')
    for r in bad[:10]:
        print('  ', r['identity'], r['name'], r['status'])
    return 0


def summarise(args: argparse.Namespace) -> int:
    dbq: dict[str, list[int]] = defaultdict(list)
    for line in Path(args.access_log).read_text(errors='replace').splitlines():
        m = ACCESS_RE.search(line)
        if m:
            dbq[m['route']].append(int(m['dbq']))
    calls: dict[tuple[str, str], list[float]] = defaultdict(list)
    with open(args.calls, newline='') as fh:
        for row in csv.DictReader(fh):
            calls[(row['name'], row['path'].split('?')[0])].append(float(row['ms']))
    writer = csv.writer(sys.stdout)
    writer.writerow(['name', 'path', 'calls', 'median_ms', 'p95_ms', 'median_dbq'])
    for (name, path), times in sorted(calls.items()):
        route = re.sub(r'/[0-9a-f-]{36}', '/{uuid}', path)
        counts = dbq.get(route) or dbq.get(path) or []
        times.sort()
        writer.writerow(
            [
                name,
                path,
                len(times),
                statistics.median(times),
                times[max(int(len(times) * 0.95) - 1, 0)],
                statistics.median(counts) if counts else '',
            ]
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest='cmd', required=True)
    p_run = sub.add_parser('run')
    p_run.add_argument('--base-url', required=True)
    p_run.add_argument('--out', required=True)
    p_run.add_argument('--repeat', type=int, default=20)
    p_run.add_argument('--check-routes', action='store_true')
    p_run.set_defaults(fn=run)
    p_sum = sub.add_parser('summarise')
    p_sum.add_argument('--calls', required=True)
    p_sum.add_argument('--access-log', required=True)
    p_sum.set_defaults(fn=summarise)
    args = parser.parse_args()
    return args.fn(args)


if __name__ == '__main__':
    sys.exit(main())
