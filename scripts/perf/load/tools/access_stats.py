#!/usr/bin/env python3
"""Aggregate the backend's `INFO:access:` lines (server-side ms and dbq per request) per route.

Usage: access_stats.py RUN_DIR [--top N]   (reads RUN_DIR/backend.log.gz, writes RUN_DIR/access_stats.csv)
Server time vs Locust's client time separates time spent INSIDE the app from queueing in front of it.
"""

from __future__ import annotations

import csv
import gzip
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

LINE = re.compile(
    r'INFO:access:\[[^\]]*\] (\w+) (\S+) (\d{3}) ([\d.]+)ms(?: user=\S+)?(?: dbq=(\d+))?'
)


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def main() -> int:
    run = Path(sys.argv[1])
    top = int(sys.argv[sys.argv.index('--top') + 1]) if '--top' in sys.argv else 30
    ms: dict[tuple[str, str], list[float]] = defaultdict(list)
    dbq: dict[tuple[str, str], list[int]] = defaultdict(list)
    status: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    with gzip.open(run / 'backend.log.gz', 'rt', errors='replace') as fh:
        for line in fh:
            m = LINE.search(line)
            if not m or m.group(2) == '/metrics':
                continue
            key = (m.group(1), m.group(2))
            ms[key].append(float(m.group(4)))
            if m.group(5):
                dbq[key].append(int(m.group(5)))
            status[key][m.group(3)] += 1
    rows = []
    for key, values in ms.items():
        rows.append(
            {
                'method': key[0],
                'route': key[1],
                'n': len(values),
                'server_p50_ms': round(pct(values, 50), 1),
                'server_p95_ms': round(pct(values, 95), 1),
                'server_max_ms': round(max(values), 1),
                'server_total_s': round(sum(values) / 1000, 1),
                'dbq_mean': round(statistics.fmean(dbq[key]), 1) if dbq[key] else '',
                'dbq_max': max(dbq[key]) if dbq[key] else '',
                'status': ' '.join(f'{k}:{v}' for k, v in sorted(status[key].items())),
            }
        )
    rows.sort(key=lambda r: -r['server_total_s'])
    with (run / 'access_stats.csv').open('w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ['route'])
        w.writeheader()
        w.writerows(rows)
    for r in rows[:top]:
        print(
            f'{r["method"]:6} {r["route"][:60]:60} n={r["n"]:6} p50={r["server_p50_ms"]:8} '
            f'p95={r["server_p95_ms"]:8} max={r["server_max_ms"]:9} tot={r["server_total_s"]:7}s '
            f'dbq={r["dbq_mean"]} {r["status"]}'
        )
    return 0


if __name__ == '__main__':
    sys.exit(main())
