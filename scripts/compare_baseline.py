#!/usr/bin/env python3
"""Compare master baseline vs branch after for the upload-speed A/B benchmark."""

from __future__ import annotations

import csv
import sys


def load(path: str) -> dict[str, dict]:
    with open(path) as f:
        return {r['fixture']: r for r in csv.DictReader(f)}


def main() -> None:
    if len(sys.argv) < 3:
        print('Usage: compare_baseline.py <master.csv> <branch.csv>')
        sys.exit(1)

    master = load(sys.argv[1])
    branch = load(sys.argv[2])

    header = f'{"fixture":<24}{"size":>8}{"master_s":>11}{"branch_s":>11}{"speedup":>9}{"saved_s":>9}{"realtime_x":>12}'
    print(header)
    print('-' * len(header))

    speedups: list[float] = []
    for name in sorted(master, key=lambda n: float(master[n]['size_mb'])):
        if name not in branch:
            print(f'{name:<24}  (no branch result)')
            continue
        m = float(master[name]['end_to_end_wall_s'])
        b = float(branch[name]['end_to_end_wall_s'])
        audio_s = float(branch[name].get('audio_s') or master[name].get('audio_s') or 0)
        size_mb = master[name]['size_mb']
        speedup = m / b if b > 0 else 0
        saved = m - b
        realtime = audio_s / b if b > 0 and audio_s > 0 else 0
        speedups.append(speedup)
        rt_str = f'{realtime:.1f}×' if realtime > 0 else 'n/a'
        print(
            f'{name:<24}{size_mb:>6}MB'
            f'{m:>10.1f}s{b:>10.1f}s'
            f'{speedup:>8.2f}×'
            f'{saved:>+8.1f}s'
            f'{rt_str:>11}'
        )

    if speedups:
        avg = sum(speedups) / len(speedups)
        lo, hi = min(speedups), max(speedups)
        print('-' * len(header))
        print(
            f'{"Average speedup":<24}{"":>8}{"":>11}{"":>11}{avg:>8.2f}×  (range {lo:.2f}×–{hi:.2f}×)'
        )


if __name__ == '__main__':
    main()
