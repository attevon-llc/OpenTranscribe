#!/usr/bin/env python3
"""Ratchet for ``noUncheckedIndexedAccess`` errors in the frontend.

``arr[i]`` and record lookups are typed as present by default, so an out-of-range index or a missing
key is ``undefined`` at runtime with no compiler warning. ``frontend/tsconfig.nuia.json`` extends
the real tsconfig with ``noUncheckedIndexedAccess`` (TypeScript tsconfig reference); the real
config is error-free under ``npm run check``, so every error in this run is one the flag introduced.
The guard runs ``svelte-check --tsconfig ./tsconfig.nuia.json --output machine-verbose`` and counts
ERROR diagnostics per file. Production files are held at zero; ``*.test.ts`` files are baselined
until a later pass converts them, after which the flag can move into the main tsconfig.

Counts may only shrink against ``baselines/nuia.txt``. Output that is not the expected shape
fails closed (exit 2).

Usage:
    check_nuia_ratchet.py [--root DIR] [--baseline FILE] [--svelte-check-output PATH]
                          [--timeout SEC] [--tighten]
    check_nuia_ratchet.py --write-baseline --reason TEXT [--force]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _baseline as bl

_HERE = Path(__file__).resolve().parent
_DEFAULT_BASELINE = _HERE / 'baselines' / 'nuia.txt'
_LINE = re.compile(r'^\d+ (?P<body>\{.*\})\s*$')
_TIMEOUT_SECONDS = 900


def parse(text: str) -> dict[str, int]:
    """ERROR diagnostics per file as ``frontend/<path>`` -> count."""
    if ' COMPLETED ' not in text:
        msg = 'svelte-check output has no COMPLETED line (tool crash?)'
        raise bl.ScanError(msg)
    counts: Counter[str] = Counter()
    for line in text.splitlines():
        match = _LINE.match(line)
        if not match:
            continue
        try:
            record = json.loads(match['body'])
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get('type') != 'ERROR':
            continue
        filename = record.get('filename')
        if not isinstance(filename, str):
            msg = f'error diagnostic without a filename: {line[:120]!r}'
            raise bl.ScanError(msg)
        counts[f'frontend/{filename}'] += 1
    return dict(counts)


def _run(root: Path, timeout: int) -> str:
    frontend = root / 'frontend'
    if not (frontend / 'tsconfig.nuia.json').is_file():
        msg = f'{frontend}/tsconfig.nuia.json is missing'
        raise bl.ScanError(msg)
    try:
        # Generates .svelte-kit/tsconfig.json, which tsconfig.json extends.
        sync = subprocess.run(
            ['npx', '--no-install', 'svelte-kit', 'sync'],
            cwd=frontend,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if sync.returncode != 0:
            msg = f'svelte-kit sync failed ({sync.returncode}): {sync.stderr.strip()[:300]}'
            raise bl.ScanError(msg)
        proc = subprocess.run(
            [
                'npx',
                '--no-install',
                'svelte-check',
                '--tsconfig',
                './tsconfig.nuia.json',
                '--output',
                'machine-verbose',
            ],
            cwd=frontend,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        msg = f'svelte-check did not run: {exc}'
        raise bl.ScanError(msg) from exc
    if proc.returncode not in (0, 1):
        msg = f'svelte-check exited {proc.returncode}: {proc.stderr.strip()[:500]}'
        raise bl.ScanError(msg)
    return proc.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--root', type=Path, default=None, help='repo root (default: auto)')
    parser.add_argument('--baseline', type=Path, default=_DEFAULT_BASELINE)
    parser.add_argument('--svelte-check-output', type=Path, default=None, help='prior run output')
    parser.add_argument('--timeout', type=int, default=_TIMEOUT_SECONDS)
    parser.add_argument('--tighten', action='store_true', help='lower values / drop stale lines')
    bl.add_baseline_args(parser)
    args = parser.parse_args(argv)
    try:
        if args.svelte_check_output:
            raw = args.svelte_check_output.read_text(encoding='utf-8')
        else:
            root: Path = args.root or bl.find_root(_HERE, ('frontend/tsconfig.nuia.json',))
            raw = _run(root, args.timeout)
        current = parse(raw)
        if args.write_baseline:
            written: int = bl.run_write_baseline(args, args.baseline, current)
            return written
        baseline = bl.load(args.baseline)
        if args.tighten:
            print(f'tightened {bl.tighten(args.baseline, current, baseline)} line(s)')
            baseline = bl.load(args.baseline)
    except (bl.ScanError, ValueError, OSError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    problems = bl.compare(current, baseline, unit='unchecked index access error(s)')
    if problems:
        print('\n'.join(problems), file=sys.stderr)
        print(f'{len(problems)} noUncheckedIndexedAccess ratchet problem(s)', file=sys.stderr)
        return 1
    print(f'noUncheckedIndexedAccess ratchet ok: {sum(current.values())} in {len(current)} files')
    return 0


if __name__ == '__main__':
    sys.exit(main())
