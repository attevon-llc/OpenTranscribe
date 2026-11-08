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
compares them with ``/api/openapi.json`` so a renamed route fails loudly instead of 404ing 20 times.
"""

from __future__ import annotations

import argparse
import csv
import re
import statistics
import sys
import time
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlparse

PASSWORD = 'loadtest-password'
IDENTITIES = {  # seeded by app.scripts.scale_dataset: 1 admin, 2 heavy owner, 3 ordinary user
    'admin': 'perf-1@loadtest.invalid',
    'heavy': 'perf-2@loadtest.invalid',
    'member': 'perf-3@loadtest.invalid',
}
TIMEOUT_STATUS = 599  # recorded for a client-side timeout
OPENAPI_PATH = '/api/openapi.json'  # the app mounts its schema under /api
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


def _client(base_url: str, email: str, timeout: float):
    import httpx

    client = httpx.Client(base_url=base_url, timeout=timeout, follow_redirects=False)
    resp = client.post('/api/auth/login', data={'username': email, 'password': PASSWORD})
    resp.raise_for_status()  # the session is an httpOnly cookie, kept by the client jar
    return client


def _first_uuid(resp) -> str | None:
    body = resp.json()
    rows = body if isinstance(body, list) else body.get('items') or body.get('results') or []
    return rows[0].get('uuid') if rows else None


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

        paths = set(httpx.get(args.base_url + OPENAPI_PATH).json()['paths'])
        wanted = {p for _, p, _ in USER_CALLS + ADMIN_ONLY}
        wanted |= {p.split('{')[0].rstrip('/') for _, p, _ in TEMPLATED}
        missing = sorted(
            w for w in wanted if w not in paths and not any(x.startswith(w) for x in paths)
        )
        print('missing routes:', missing or 'none')
        return 1 if missing else 0

    import httpx

    fields = ['seq', 'identity', 'name', 'path', 'params', 'iteration', 'status', 'ms', 'bytes']
    state = {'seq': 0, 'bad': 0}
    fh = (out / 'calls.csv').open('w', newline='')
    writer = csv.DictWriter(fh, fieldnames=fields)
    writer.writeheader()

    def call(client, who, name, path, params, i):
        """One GET, one flushed CSV row; a timeout is recorded as status 599, never raised."""
        state['seq'] += 1
        started = time.perf_counter()
        try:
            resp = client.get(path, params=params)
            status, size = resp.status_code, len(resp.content)
        except httpx.TimeoutException:
            resp, status, size = None, TIMEOUT_STATUS, 0
        ms = round((time.perf_counter() - started) * 1000, 1)
        state['bad'] += status >= 400
        writer.writerow(
            {
                'seq': state['seq'],
                'identity': who,
                'name': name,
                'path': path,
                'params': repr(params),
                'iteration': i,
                'status': status,
                'ms': ms,
                'bytes': size,
            }
        )
        fh.flush()
        return ms, resp

    try:
        for who, email in IDENTITIES.items():
            client = _client(args.base_url, email, args.timeout)
            calls = list(USER_CALLS) + (ADMIN_ONLY if who == 'admin' else [])
            for name, path, extra in TEMPLATED:
                lookup = '/api/speaker-profiles/profiles' if extra == 'profile' else '/api/speakers'
                _, resp = call(client, who, 'lookup.' + extra, lookup, {}, 0)
                ident = _first_uuid(resp) if resp is not None and resp.status_code == 200 else None
                if ident:
                    calls.append((name, path.replace('{' + extra + '}', ident), {}))
                else:
                    print(f'{who}: no {extra} uuid resolved; {name} skipped', file=sys.stderr)
            for name, path, params in calls:
                for i in range(args.repeat):
                    ms, _ = call(client, who, name, path, params, i)
                    if ms > args.slow_ms and i + 1 >= args.slow_repeat:
                        break  # an endpoint this slow is capped, not repeated 20 times
    finally:
        fh.close()
    print(f'{state["seq"]} calls -> {out / "calls.csv"}; non-2xx/3xx: {state["bad"]}')
    return 0


def parse_access_log(lines: Iterable[str]) -> list[dict[str, str]]:
    """Authenticated GET lines of the backend access log, in order, health probes dropped."""
    found = [m.groupdict() for line in lines if (m := ACCESS_RE.search(line))]
    return [m for m in found if m['m'] == 'GET' and not m['route'].startswith('/health')]


def _route_matches(path: str, route: str) -> bool:
    return (
        path.split('?')[0] == route
        or re.sub(r'/[0-9a-f-]{36}', '/{uuid}', path) == route
        or ('{' in route and path.startswith(route.split('{')[0]))
    )


def attribute_dbq(
    calls: list[dict[str, str]], log: list[dict[str, str]]
) -> list[dict[str, object]]:
    """Pair each call with its own access-log line (the driver is sequential).

    Keying by route alone gave every ``/api/files`` variant and identity one shared statement
    count. A call with status 599 never completed client-side; its server line lands later and
    out of order, so it is skipped on both sides. Returns one dict per call with ``dbq`` (None
    when no line matched) and counts skipped log lines so a misalignment is visible.
    """
    out: list[dict[str, object]] = []
    li = 0
    for call in calls:
        if int(call['status']) == TIMEOUT_STATUS:
            out.append({**call, 'dbq': None, 'server_ms': None})
            continue
        probe = li
        while probe < len(log) and not _route_matches(call['path'], log[probe]['route']):
            probe += 1
        if probe >= len(log):
            out.append({**call, 'dbq': None, 'server_ms': None})
            continue
        out.append({**call, 'dbq': int(log[probe]['dbq']), 'server_ms': float(log[probe]['ms'])})
        li = probe + 1
    return out


def summarise(args: argparse.Namespace) -> int:
    log = parse_access_log(Path(args.access_log).read_text(errors='replace').splitlines())
    with open(args.calls, newline='') as fh:
        paired = attribute_dbq(list(csv.DictReader(fh)), log)
    groups: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in paired:
        groups[(str(row['identity']), str(row['name']))].append(row)
    writer = csv.writer(sys.stdout)
    writer.writerow(
        [
            'identity',
            'name',
            'n',
            'timeouts',
            'median_ms',
            'p95_ms',
            'median_dbq',
            'min_dbq',
            'max_dbq',
        ]
    )
    for (who, name), rows in sorted(groups.items()):
        times = sorted(float(str(r['ms'])) for r in rows)
        dbqs = [int(str(r['dbq'])) for r in rows if r['dbq'] is not None]
        timeouts = sum(int(str(r['status'])) == TIMEOUT_STATUS for r in rows)
        writer.writerow(
            [
                who,
                name,
                len(rows),
                timeouts,
                statistics.median(times),
                times[max(int(len(times) * 0.95) - 1, 0)],
                statistics.median(dbqs) if dbqs else '',
                min(dbqs) if dbqs else '',
                max(dbqs) if dbqs else '',
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
    p_run.add_argument('--timeout', type=float, default=900.0, help='per-call client timeout, s')
    p_run.add_argument('--slow-ms', type=float, default=30000.0)
    p_run.add_argument(
        '--slow-repeat', type=int, default=2, help='repeats kept once a call exceeds --slow-ms'
    )
    p_run.set_defaults(fn=run)
    p_sum = sub.add_parser('summarise')
    p_sum.add_argument('--calls', required=True)
    p_sum.add_argument('--access-log', required=True)
    p_sum.set_defaults(fn=summarise)
    args = parser.parse_args()
    return args.fn(args)


if __name__ == '__main__':
    sys.exit(main())
