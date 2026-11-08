#!/usr/bin/env python3
"""Per-stage view of a ramp/spike run from locust_stats_history.csv (Aggregated rows).

Usage: ramp_stages.py RUN_DIR
For each held user level: seconds held, mean req/s, mean fail/s (error share), and the median of
the 5-second-window p50/p95 (Locust's history percentiles are cumulative-window per row; the
"current" columns are what is reported here).
"""

from __future__ import annotations

import csv
import statistics
import sys
from pathlib import Path


def num(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, '', 'N/A') else None
    except ValueError:
        return None


def main() -> int:
    run = Path(sys.argv[1])
    rows = [
        r
        for r in csv.DictReader((run / 'locust_stats_history.csv').open())
        if r['Name'] == 'Aggregated'
    ]
    # Consecutive rows whose user count stays within 2% (+-2) form one held segment.
    segments: list[list[dict]] = []
    for r in rows:
        u = int(num(r['User Count']) or 0)
        if u == 0:
            continue
        if segments:
            last = int(num(segments[-1][0]['User Count']) or 0)
            if abs(u - last) <= max(2, 0.02 * last):
                segments[-1].append(r)
                continue
        segments.append([r])
    print(f'#### `{run.name}` per held stage\n')
    print(
        '| Users | Held (s) | req/s | fail/s | error share | p50 ms (median of windows) | p95 ms (median of windows) |'
    )
    print('|---:|---:|---:|---:|---:|---:|---:|')
    for rs in segments:
        ts = [num(r['Timestamp']) or 0 for r in rs]
        if max(ts) - min(ts) < 30:
            continue
        u = int(num(rs[0]['User Count']) or 0)
        rps = statistics.fmean(num(r['Requests/s']) or 0 for r in rs)
        fps = statistics.fmean(num(r['Failures/s']) or 0 for r in rs)
        p50 = [num(r['50%']) for r in rs if num(r['50%']) is not None]
        p95 = [num(r['95%']) for r in rs if num(r['95%']) is not None]
        print(
            f'| {u} | {max(ts) - min(ts):.0f} | {rps:.1f} | {fps:.1f} | {fps / rps * 100 if rps else 0:.0f}% | '
            f'{statistics.median(p50) if p50 else "-"} | {statistics.median(p95) if p95 else "-"} |'
        )
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
