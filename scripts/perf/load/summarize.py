#!/usr/bin/env python3
"""Turn a load-test run directory into a markdown report with a pass/fail verdict.

    scripts/perf/load/summarize.py RUN_DIR [--thresholds scripts/perf/load/thresholds.toml]

Reads Locust's ``locust_stats.csv``, ``locust_failures.csv`` and ``locust_stats_history.csv``
(``--csv RUN_DIR/locust --csv-full-history``), the sampler's ``samples.csv`` and ``env.txt``
when present, and writes ``RUN_DIR/summary.md`` (also printed). Exit status: 0 every limit
held, 1 a limit was broken, 2 the run directory is unusable.

Each per-endpoint row is judged by its kind: Locust's request type says whether it is a read
(GET), a write (any other method), a chat stream (SSE), the live socket (WS) or a recording
through the pipeline (PIPELINE). The limits are in ``thresholds.toml``.
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
KIND_OF_TYPE = {'GET': 'read', 'SSE': 'stream', 'WS': 'socket', 'PIPELINE': 'pipeline'}
HISTORY_ROWS = 12


@dataclass
class Verdict:
    failures: list[str] = field(default_factory=list)

    def check(self, ok: bool, message: str) -> str:
        if not ok:
            self.failures.append(message)
        return 'ok' if ok else '**FAIL**'


def _num(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, '', 'N/A') else None
    except ValueError:
        return None


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline='') as fh:
        return list(csv.DictReader(fh))


def kind_of(request_type: str) -> str:
    return KIND_OF_TYPE.get(request_type, 'write')


def limits_for(kind: str, name: str, limits: dict[str, Any]) -> dict[str, float]:
    """The ``p95_ms`` / ``p99_ms`` limits of one row (a stream's first token has its own)."""
    section = limits.get(kind, {})
    if kind == 'stream' and name.endswith('first token'):
        return {'p95_ms': section['first_token_p95_ms']}
    return {k: v for k, v in section.items() if k in ('p95_ms', 'p99_ms')}


def endpoint_section(stats: list[dict[str, str]], limits: dict[str, Any], v: Verdict) -> list[str]:
    min_requests = limits['overall']['min_requests']
    lines = [
        '| Kind | Type | Name | Requests | Failures | p50 ms | p95 ms | p99 ms | Limit | Verdict |',
        '|---|---|---|---:|---:|---:|---:|---:|---|---|',
    ]
    rows = [r for r in stats if r['Name'] != 'Aggregated']
    rows.sort(key=lambda r: (kind_of(r['Type']), -(_num(r['95%']) or 0)))
    for r in rows:
        kind = kind_of(r['Type'])
        count = int(_num(r['Request Count']) or 0)
        p95, p99 = _num(r['95%']), _num(r['99%'])
        bounds = limits_for(kind, r['Name'], limits)
        verdict = 'too few'
        if count >= min_requests:
            ok = all(
                got is None or got <= bound
                for got, bound in ((p95, bounds.get('p95_ms')), (p99, bounds.get('p99_ms')))
                if bound is not None
            )
            verdict = v.check(ok, f'{r["Type"]} {r["Name"]}: p95 {p95} / p99 {p99} over {bounds}')
        limit = ', '.join(f'{k[:3]} {int(b)}' for k, b in bounds.items())
        lines.append(
            f'| {kind} | {r["Type"]} | {r["Name"]} | {count} | {r["Failure Count"]} | '
            f'{r["50%"]} | {r["95%"]} | {r["99%"]} | {limit} | {verdict} |'
        )
    return lines


def failure_section(failures: list[dict[str, str]]) -> list[str]:
    by_cause: dict[str, int] = defaultdict(int)
    for f in failures:
        by_cause[f['Error']] += int(_num(f['Occurrences']) or 0)
    lines = ['| Cause | Occurrences |', '|---|---:|']
    lines += [f'| {cause} | {n} |' for cause, n in sorted(by_cause.items(), key=lambda x: -x[1])]
    lines += ['', '| Method | Name | Cause | Occurrences |', '|---|---|---|---:|']
    lines += [
        f'| {f["Method"]} | {f["Name"]} | {f["Error"]} | {f["Occurrences"]} |' for f in failures
    ]
    return lines if failures else ['No failed requests.']


def history_section(history: list[dict[str, str]]) -> list[str]:
    rows = [h for h in history if h.get('Name') == 'Aggregated']
    if not rows:
        return ['No percentile history (run Locust with `--csv-full-history`).']
    step = max(1, len(rows) // HISTORY_ROWS)
    picked = rows[::step]
    if picked[-1] is not rows[-1]:
        picked.append(rows[-1])
    start = _num(rows[0]['Timestamp']) or 0
    lines = [
        '| t (s) | Users | Req/s | Fail/s | p50 ms | p95 ms | p99 ms |',
        '|---:|---:|---:|---:|---:|---:|---:|',
    ]
    for h in picked:
        t = int((_num(h['Timestamp']) or start) - start)
        lines.append(
            f'| {t} | {h["User Count"]} | {h["Requests/s"]} | {h["Failures/s"]} | '
            f'{h["50%"]} | {h["95%"]} | {h["99%"]} |'
        )
    return lines


def _series(samples: list[dict[str, str]]) -> dict[tuple[str, str], list[tuple[float, float]]]:
    out: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for s in samples:
        ts, value = _num(s['ts']), _num(s['value'])
        if ts is not None and value is not None:
            out[(s['source'], s['name'])].append((ts, value))
    return out


def sampler_section(samples: list[dict[str, str]], limits: dict[str, Any], v: Verdict) -> list[str]:
    if not samples:
        return ['No sampler data (`samples.csv` missing).']
    series = _series(samples)
    lim = limits['sampler']
    lines = [
        '| Source | Name | Min | Mean | Max | Last | Verdict |',
        '|---|---|---:|---:|---:|---:|---|',
    ]
    for (source, name), points in sorted(series.items()):
        values = [p[1] for p in points]
        verdict = ''
        if (source, name) == ('pg', 'idle_in_transaction_over_1s'):
            verdict = v.check(
                max(values) <= lim['max_idle_in_transaction'],
                f'{max(values):.0f} connections idle in a transaction for over 1 s',
            )
        if source == 'container' and name.startswith('mem_bytes:') and values[0] > 0:
            growth = values[-1] / values[0] - 1
            verdict = v.check(
                growth <= lim['max_memory_growth_share'],
                f'{name} grew {growth:.0%} from the first sample to the last',
            )
        lines.append(
            f'| {source} | {name} | {min(values):.1f} | {statistics.fmean(values):.1f} | '
            f'{max(values):.1f} | {values[-1]:.1f} | {verdict} |'
        )
    lines += ['', pool_saturation(series, lim, v)]
    return lines


def pool_saturation(
    series: dict[tuple[str, str], list[tuple[float, float]]], lim: dict[str, Any], v: Verdict
) -> str:
    """Share of samples where every pool connection was checked out (requests were queueing)."""
    used: dict[float, float] = defaultdict(float)
    capacity: dict[float, float] = defaultdict(float)
    for (source, name), points in series.items():
        if source != 'pool':
            continue
        if name.startswith('db_pool_checked_out'):
            target = used
        elif name.startswith('db_pool_capacity'):
            target = capacity
        else:
            continue
        for ts, value in points:
            target[ts] += value
    stamps = [ts for ts in capacity if capacity[ts] > 0]
    if not stamps:
        return 'Pool saturation: no `db_pool_capacity` samples.'
    share = sum(1 for ts in stamps if used[ts] >= capacity[ts]) / len(stamps)
    verdict = v.check(
        share <= lim['max_pool_saturated_share'],
        f'the DB pool was saturated in {share:.0%} of samples',
    )
    return f'Pool saturation: {share:.1%} of {len(stamps)} samples had every connection checked out ({verdict}).'


def summarize(run_dir: Path, limits: dict[str, Any]) -> tuple[str, Verdict]:
    stats = _read_csv(run_dir / 'locust_stats.csv')
    if not stats:
        raise FileNotFoundError(f'{run_dir}/locust_stats.csv is missing or empty')
    v = Verdict()
    total = next((r for r in stats if r['Name'] == 'Aggregated'), None)
    requests = int(_num(total['Request Count']) or 0) if total else 0
    failed = int(_num(total['Failure Count']) or 0) if total else 0
    rate = failed / requests if requests else 1.0
    v.check(
        requests > 0 and rate <= limits['overall']['max_error_rate'],
        f'error rate {rate:.2%} of {requests} requests (limit {limits["overall"]["max_error_rate"]:.2%})',
    )
    body = [
        *endpoint_section(stats, limits, v),
        '',
        '## Failures by cause',
        '',
        *failure_section(_read_csv(run_dir / 'locust_failures.csv')),
        '',
        '## Percentiles over time (all requests)',
        '',
        *history_section(_read_csv(run_dir / 'locust_stats_history.csv')),
        '',
        '## Stack samples',
        '',
        *sampler_section(_read_csv(run_dir / 'samples.csv'), limits, v),
    ]
    env = run_dir / 'env.txt'
    head = [
        f'# Load test: {run_dir.name}',
        '',
        f'**Verdict: {"PASS" if not v.failures else "FAIL"}** - {requests} requests, '
        f'{failed} failed ({rate:.2%}).',
        '',
        *[f'- {f}' for f in v.failures],
        '',
        '## Run',
        '',
        '```',
        env.read_text().rstrip() if env.is_file() else '(no env.txt)',
        '```',
        '',
        '## Endpoints',
        '',
    ]
    return '\n'.join(head + body) + '\n', v


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('run_dir', type=Path)
    parser.add_argument('--thresholds', type=Path, default=HERE / 'thresholds.toml')
    args = parser.parse_args(argv)
    try:
        limits = tomllib.loads(args.thresholds.read_text())
        report, verdict = summarize(args.run_dir, limits)
    except (OSError, tomllib.TOMLDecodeError, KeyError) as exc:
        print(f'summarize: {exc}', file=sys.stderr)
        return 2
    (args.run_dir / 'summary.md').write_text(report)
    print(report)
    return 1 if verdict.failures else 0


if __name__ == '__main__':
    sys.exit(main())
