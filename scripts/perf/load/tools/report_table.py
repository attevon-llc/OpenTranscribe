#!/usr/bin/env python3
"""One markdown block per run for RESULTS.md: RPS, per-group p50/p95/p99, error classes, saturation.

Usage: BACKEND_IP=<api container ip> report_table.py RUN_DIR [RUN_DIR ...]  > block.md

- Group percentiles: request-count-weighted mixture of each Locust row's percentile curve
  (50..100%) from locust_stats.csv. An approximation, but exact when a group has one row.
- RPS: total requests / span of the Aggregated rows in locust_stats_history.csv where users > 0.
- Error classes: locust_failures.csv bucketed; HTTP 500s split by the backend log into
  QueuePool timeouts and other 500s.
- Saturation: server-side in-flight requests reconstructed from the access log (end time and
  duration of each request), API connections idle in a transaction (pg probe, backend IP),
  QueuePool TimeoutError count, Redis timeout count, /metrics scrape failures from samples.csv.
"""

from __future__ import annotations

import bisect
import csv
import gzip
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

PCTS = [50, 66, 75, 80, 90, 95, 98, 99, 99.9, 99.99, 100]
COLS = ['50%', '66%', '75%', '80%', '90%', '95%', '98%', '99%', '99.9%', '99.99%', '100%']
BACKEND_IP = os.environ.get('BACKEND_IP', '')  # the API container IP (pg_probe client column)

GROUPS = [
    ('auth (login, me, session)', lambda _t, n: n.split()[0] in ('login', 'me', 'session')),
    ('gallery (files.list*)', lambda _t, n: n.startswith('files.list')),
    (
        'speaker pages (speakers/profiles/clusters)',
        lambda t, n: (
            t == 'GET' and n.split()[0].split('.')[0] in ('speakers', 'profiles', 'clusters')
        ),
    ),
    (
        'file page reads',
        lambda t, n: (
            t == 'GET'
            and n.split()[0]
            in (
                'files.detail',
                'comments.list',
                'tags.for_files',
                'collections.for_files',
                'files.analytics',
                'files.summary_status',
                'files.post_processing',
                'topics.suggestions',
                'summary.get',
                'files.stream_url',
                'files.waveform',
                'files.segments',
                'files.subtitles',
                'files.export',
                'files.captions',
            )
        ),
    ),
    ('search', lambda t, n: t == 'GET' and n.split()[0].startswith('search')),
    (
        'admin',
        lambda t, n: (
            t == 'GET'
            and n.split()[0].split('.')[0] in ('admin', 'system', 'tasks')
            and n.split()[0] not in ('system.capabilities',)
        ),
    ),
    ('other reads', lambda t, _n: t == 'GET'),
    ('writes', lambda t, _n: t not in ('GET', 'SSE', 'WS', 'PIPELINE')),
    ('chat SSE first token', lambda t, n: t == 'SSE' and n.endswith('first token')),
    ('chat SSE whole answer', lambda t, _n: t == 'SSE'),
    ('WebSocket', lambda t, _n: t == 'WS'),
    ('pipeline upload->completed', lambda t, _n: t == 'PIPELINE'),
]


def num(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, '', 'N/A') else None
    except ValueError:
        return None


def group_of(t: str, n: str) -> str:
    for name, pred in GROUPS:
        if pred(t, n):
            return name
    return 'other'


def mixture_pct(rows: list[dict], q: float) -> float | None:
    """Weighted mixture: P(X <= x) = sum_i w_i F_i(x); invert by bisection over candidate x."""
    curves = []
    total = 0
    for r in rows:
        w = int(num(r['Request Count']) or 0)
        xs = [num(r[c]) for c in COLS]
        if w == 0 or any(x is None for x in xs):
            continue
        curves.append((w, xs))
        total += w
    if not total:
        return None

    def cdf(x: float) -> float:
        acc = 0.0
        for w, xs in curves:
            i = bisect.bisect_right(xs, x)
            frac = 0.0 if i == 0 else PCTS[min(i, len(PCTS)) - 1] / 100
            acc += w * frac
        return acc / total

    candidates = sorted({x for _, xs in curves for x in xs})
    for x in candidates:
        if cdf(x) >= q / 100 - 1e-9:
            return x
    return candidates[-1]


def read_csv(p: Path) -> list[dict]:
    if not p.is_file():
        return []
    with p.open(newline='') as fh:
        return list(csv.DictReader(fh))


def error_class(cause: str) -> str:
    """Bucket a Locust failure cause. Match status codes only in their own syntax, never a
    bare substring: request UUIDs inside "no response (URL ...)" causes contain digits."""
    m = re.search(r'HTTP (\d{3})|code=(\d{3})|\((\d{3})\)', cause)
    code = next((g for g in (m.groups() if m else ()) if g), None)
    if 'no response' in cause and 'timed out' in cause:
        return 'client timeout (60 s)'
    if 'no response' in cause or 'connection' in cause.lower():
        return 'connection error'
    if code == '429':
        return '429 rate limited'
    if code == '401':
        return '401 session lost'
    if code in ('204', '403', '404', '409'):
        return 'scenario artifact (204/403/404/409)'
    if code:
        return code
    if 'JSONDecodeError' in cause:
        return 'WS closed by server (non-JSON frame)'
    return 'other: ' + cause[:60]


LOG_LINE = re.compile(
    r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) INFO:access:\[[^\]]*\] \w+ (\S+) (\d{3}) ([\d.]+)ms'
)


def backend_stats(run: Path) -> dict:
    out: dict = {
        'pool_timeouts': 0,
        'redis_timeouts': 0,
        'inflight_max': 0,
        'inflight_p95': 0,
        'inflight_mean': 0.0,
        'status': Counter(),
    }
    p = run / 'backend.log.gz'
    if not p.is_file():
        return out
    intervals = []
    with gzip.open(p, 'rt', errors='replace') as fh:
        for line in fh:
            if 'QueuePool limit' in line and (
                'TimeoutError' in line
                or 'Error retrieving user' in line
                or 'Unexpected error' in line
                or 'Session error' in line
            ):
                out['pool_timeouts'] += 1
            if (
                'redis.exceptions.TimeoutError' in line
                or 'redis.exceptions.MaxConnectionsError' in line
            ):
                out['redis_timeouts'] += 1
            m = LOG_LINE.match(line)
            if m and m.group(2) != '/metrics':
                end = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S,%f').timestamp()
                intervals.append((end - float(m.group(4)) / 1000, end))
                out['status'][m.group(3)] += 1
    if intervals:
        events = sorted([(s, 1) for s, _ in intervals] + [(e, -1) for _, e in intervals])
        cur = 0
        samples = []
        step, nxt = 1.0, events[0][0]
        for t, d in events:
            while nxt <= t:
                samples.append(cur)
                nxt += step
            cur += d
            out['inflight_max'] = max(out['inflight_max'], cur)
        if samples:
            s = sorted(samples)
            out['inflight_p95'] = s[int(0.95 * (len(s) - 1))]
            out['inflight_mean'] = sum(s) / len(s)
    return out


def probe_stats(run: Path) -> dict:
    p = run / 'pgprobe.csv'
    if not p.is_file():
        return {}
    per_ts: Counter = Counter()
    xacts = []
    with p.open() as fh:
        next(fh, None)
        for line in fh:
            parts = line.rstrip('\n').split('|')
            if len(parts) < 8 or parts[2] != BACKEND_IP or not parts[3].startswith('idle in'):
                continue
            per_ts[parts[0]] += 1
            xacts.append(float(parts[4]))
    if not per_ts:
        return {'api_idle_tx_max': 0, 'api_idle_tx_mean': 0, 'xact_max': 0}
    vals = list(per_ts.values())
    return {
        'api_idle_tx_max': max(vals),
        'api_idle_tx_mean': sum(vals) / len(vals),
        'xact_max': max(xacts),
    }


def sampler_stats(run: Path) -> dict:
    rows = read_csv(run / 'samples.csv')
    ts = {r['ts'] for r in rows}
    metric_err = sum(1 for r in rows if r['source'] == 'error' and r['name'].startswith('pool:'))
    pool = [num(r['value']) for r in rows if r['name'] == 'db_pool_checked_out']
    cpu_backend = [num(r['value']) for r in rows if r['name'] == 'cpu_pct:backend']
    cpu_pg = [num(r['value']) for r in rows if r['name'] == 'cpu_pct:postgres']
    redis_mem = [num(r['value']) for r in rows if r['name'] == 'mem_bytes:redis']
    return {
        'sweeps': len(ts),
        'metrics_failed': metric_err,
        'pool_samples': len(pool),
        'pool_max': max(pool) if pool else None,
        'cpu_backend_mean': sum(cpu_backend) / len(cpu_backend) if cpu_backend else None,
        'cpu_backend_max': max(cpu_backend) if cpu_backend else None,
        'cpu_pg_mean': sum(cpu_pg) / len(cpu_pg) if cpu_pg else None,
        'redis_mem_max_gb': max(redis_mem) / 1e9 if redis_mem else None,
    }


def fmt(v: float | None) -> str:
    if v is None:
        return '-'
    return f'{v / 1000:.1f}s' if v >= 1000 else f'{v:.0f}'


def block(run: Path) -> str:
    stats = [r for r in read_csv(run / 'locust_stats.csv') if r['Name'] != 'Aggregated']
    agg = next((r for r in read_csv(run / 'locust_stats.csv') if r['Name'] == 'Aggregated'), None)
    hist = [
        h
        for h in read_csv(run / 'locust_stats_history.csv')
        if h.get('Name') == 'Aggregated' and (num(h['User Count']) or 0) > 0
    ]
    span = (num(hist[-1]['Timestamp']) - num(hist[0]['Timestamp'])) if len(hist) > 1 else 0
    total = int(num(agg['Request Count']) or 0) if agg else 0
    failed = int(num(agg['Failure Count']) or 0) if agg else 0
    peak_users = max((int(num(h['User Count']) or 0) for h in hist), default=0)
    verdict = '?'
    summ = run / 'summary.md'
    if summ.is_file():
        m = re.search(r'\*\*Verdict: (\w+)\*\*', summ.read_text())
        verdict = m.group(1) if m else '?'
    lines = [f'#### `{run.name}`', '']
    lines.append(
        f'Peak users {peak_users}; {total} requests over {span:.0f} s = **{(total / span if span else 0):.1f} req/s**; '
        f'failed {failed} (**{(failed / total * 100 if total else 0):.2f}%**); summarize.py verdict **{verdict}**.'
    )
    lines += [
        '',
        '| Group | Requests | Failures | p50 | p95 | p99 |',
        '|---|---:|---:|---:|---:|---:|',
    ]
    by: dict[str, list[dict]] = defaultdict(list)
    for r in stats:
        by[group_of(r['Type'], r['Name'])].append(r)
    for name, _ in GROUPS:
        rows = by.get(name)
        if not rows:
            continue
        req = sum(int(num(r['Request Count']) or 0) for r in rows)
        fl = sum(int(num(r['Failure Count']) or 0) for r in rows)
        lines.append(
            f'| {name} | {req} | {fl} | {fmt(mixture_pct(rows, 50))} | {fmt(mixture_pct(rows, 95))} | {fmt(mixture_pct(rows, 99))} |'
        )
    fails = read_csv(run / 'locust_failures.csv')
    classes: Counter = Counter()
    for f in fails:
        classes[error_class(f['Error'])] += int(num(f['Occurrences']) or 0)
    b = backend_stats(run)
    pr = probe_stats(run)
    sa = sampler_stats(run)
    lines += [
        '',
        'Errors by class: '
        + (', '.join(f'{k} {v}' for k, v in classes.most_common()) or 'none')
        + '.',
    ]
    lines.append(
        f'Backend log: {b["pool_timeouts"]} QueuePool-timeout error log lines, '
        f'{b["redis_timeouts"]} Redis timeouts/max-connections; HTTP status {dict(b["status"].most_common(6))}.'
    )
    sat = f'Saturation: server-side in-flight requests mean {b["inflight_mean"]:.0f} / p95 {b["inflight_p95"]} / max {b["inflight_max"]}'
    if pr:
        sat += (
            f'; API connections idle in a transaction >1 s (pg probe) mean {pr["api_idle_tx_mean"]:.0f} / max {pr["api_idle_tx_max"]}, '
            f'longest API transaction {pr["xact_max"]:.0f} s'
        )
    sat += (
        f'; sampler: /metrics scrape failed in {sa["metrics_failed"]} of {sa["sweeps"]} sweeps'
        + (f', db_pool_checked_out max {sa["pool_max"]:.0f}' if sa['pool_max'] is not None else '')
        + (
            f'; backend CPU mean {sa["cpu_backend_mean"]:.0f}% / max {sa["cpu_backend_max"]:.0f}%'
            if sa['cpu_backend_mean'] is not None
            else ''
        )
        + (f'; Postgres CPU mean {sa["cpu_pg_mean"]:.0f}%' if sa['cpu_pg_mean'] is not None else '')
        + (
            f'; Redis memory max {sa["redis_mem_max_gb"]:.1f} GB'
            if sa['redis_mem_max_gb'] is not None
            else ''
        )
        + '.'
    )
    lines.append(sat)
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    for arg in sys.argv[1:]:
        print(block(Path(arg)))
