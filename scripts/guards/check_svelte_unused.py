#!/usr/bin/env python3
"""Ratchet for unused locals, parameters and imports in ``.svelte`` files.

``@typescript-eslint/no-unused-vars`` crashes on svelte-eslint-parser nodes (an implicit reactive
declaration such as ``$: x = 1`` has no definition; reproduced with typescript-eslint 8.72.1 and
eslint-plugin-svelte 3.24.1 / svelte-eslint-parser 1.10.1), so it is off for ``.svelte`` files and
the TypeScript compiler does the job instead. ``frontend/tsconfig.unused.json`` extends the real
tsconfig with ``noUnusedLocals`` and ``noUnusedParameters``; this guard runs
``svelte-check --output machine-verbose`` against it and counts the diagnostics with codes 6133
(declared but never read), 6192 (all imports unused), 6196 (declared but never used) and 6198
(all destructured elements unused) per ``path::TS<code>``. ``.ts`` files are left to eslint.

Counts may only shrink against ``baselines/svelte_unused.txt``. Output that is not the expected
shape fails closed (exit 2).

Usage:
    check_svelte_unused.py [--root DIR] [--baseline FILE] [--svelte-check-output PATH]
                           [--timeout SEC] [--tighten]
    check_svelte_unused.py --write-baseline --reason TEXT [--force]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DEFAULT_BASELINE = _HERE / 'baselines' / 'svelte_unused.txt'
_UNUSED_CODES = frozenset({6133, 6192, 6196, 6198})
_LINE = re.compile(r'^\d+ (?P<body>\{.*\})\s*$')
_TIMEOUT_SECONDS = 900


def _load_baseline_lib():
    path = _HERE / '_baseline.py'
    spec = importlib.util.spec_from_file_location('_guard_baseline_svelte_unused', path)
    if spec is None or spec.loader is None:
        msg = f'cannot load {path}'
        raise RuntimeError(msg)
    module = sys.modules.get(spec.name)
    if module is None:
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


bl = _load_baseline_lib()


def parse(text: str) -> dict[str, int]:
    """Unused-symbol diagnostics in ``.svelte`` files as ``frontend/<path>::TS<code>`` -> count."""
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
        if not isinstance(record, dict) or record.get('code') not in _UNUSED_CODES:
            continue
        filename = record.get('filename')
        if not isinstance(filename, str):
            msg = f'unused-symbol diagnostic without a filename: {line[:120]!r}'
            raise bl.ScanError(msg)
        if filename.endswith('.svelte'):
            counts[f'frontend/{filename}::TS{record["code"]}'] += 1
    return dict(counts)


def _run(root: Path, timeout: int) -> str:
    frontend = root / 'frontend'
    if not (frontend / 'tsconfig.unused.json').is_file():
        msg = f'{frontend}/tsconfig.unused.json is missing'
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
                './tsconfig.unused.json',
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
            root: Path = args.root or bl.find_root(_HERE, ('frontend/tsconfig.unused.json',))
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
    problems = bl.compare(current, baseline, unit='unused symbol(s)')
    if problems:
        print('\n'.join(problems), file=sys.stderr)
        print(f'{len(problems)} svelte unused-symbol ratchet problem(s)', file=sys.stderr)
        return 1
    print(f'svelte unused ratchet ok: {sum(current.values())} in {len(current)} buckets')
    return 0


if __name__ == '__main__':
    sys.exit(main())
