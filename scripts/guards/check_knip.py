#!/usr/bin/env python3
"""Ratchet guard: frontend dead code (knip) and import cycles (madge).

Runs ``npx knip --reporter json`` and ``npm run cycles`` in ``frontend/`` and compares them with
two baselines that can only shrink:

* ``scripts/guards/baselines/knip.txt`` keyed ``type::path::symbol`` (``type`` is knip's issue
  category: ``exports``, ``types``, ``files``, ``dependencies`` ...), value = occurrences.
* ``scripts/guards/baselines/madge_cycles.txt`` keyed ``cycle::a > b > ...``, value 1. The chain
  is rotated to start at its smallest module so the same cycle always has the same key.

Both tools exit non-zero when they find something, so the exit code cannot tell findings from a
crash. The guard fails closed (exit 2) instead on: output that is not the expected shape, a
knip ``ERROR:`` line on stderr (knip then reports unrelated files as unused), or a madge run
that prints neither a cycle list nor its all-clear line.

Usage::

    check_knip.py [--root DIR] [--baseline FILE] [--madge-baseline FILE]
    check_knip.py --knip-json PATH --madge-output PATH     # offline, for tests
    check_knip.py --tighten
    check_knip.py --write-baseline --reason TEXT [--cycle-reason TEXT] [--force]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DEFAULT_BASELINE = _HERE / 'baselines' / 'knip.txt'
_DEFAULT_MADGE_BASELINE = _HERE / 'baselines' / 'madge_cycles.txt'
_TOOL_TIMEOUT_SECONDS = 600
_CYCLE_LINE = re.compile(r'^\s*\d+\)\s+(?P<chain>.+?)\s*$')
_ALL_CLEAR = 'No circular dependency found'
_NON_ISSUE_KEYS = frozenset({'file'})


def _load_baseline_lib():
    path = _HERE / '_baseline.py'
    spec = importlib.util.spec_from_file_location('_guard_baseline_knip', path)
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


def _symbol(item: object) -> str:
    if isinstance(item, dict) and isinstance(item.get('name'), str):
        name: str = item['name']
        return name
    if isinstance(item, list):  # knip "duplicates": a group of names exporting the same thing
        return '|'.join(_symbol(i) for i in item)
    msg = f'unrecognised knip issue item: {item!r}'
    raise bl.ScanError(msg)


def parse_knip(text: str) -> dict[str, int]:
    """Findings from knip's JSON reporter as ``type::path::symbol`` -> occurrences."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f'knip output is not JSON ({exc})'
        raise bl.ScanError(msg) from None
    issues = data.get('issues') if isinstance(data, dict) else None
    if not isinstance(issues, list):
        msg = 'knip JSON has no "issues" list'
        raise bl.ScanError(msg)
    findings: dict[str, int] = {}
    for record in issues:
        if not isinstance(record, dict) or not isinstance(record.get('file'), str):
            msg = f'unrecognised knip issue record: {str(record)[:120]!r}'
            raise bl.ScanError(msg)
        for kind, items in record.items():
            if kind in _NON_ISSUE_KEYS or not isinstance(items, list):
                continue
            for item in items:
                key = f'{kind}::{record["file"]}::{_symbol(item)}'
                findings[key] = findings.get(key, 0) + 1
    return findings


def normalise_cycle(chain: str) -> str:
    """``b > c > a`` -> ``a > b > c``-rotation starting at the smallest module."""
    nodes = [n.strip() for n in chain.split('>') if n.strip()]
    if not nodes:
        msg = f'empty madge cycle: {chain!r}'
        raise bl.ScanError(msg)
    start = nodes.index(min(nodes))
    return ' > '.join(nodes[start:] + nodes[:start])


def parse_madge(text: str) -> dict[str, int]:
    """Cycles from ``madge --circular`` output as ``cycle::<chain>`` -> 1."""
    cycles: dict[str, int] = {}
    for line in text.splitlines():
        match = _CYCLE_LINE.match(line)
        if match:
            cycles[f'cycle::{normalise_cycle(match["chain"])}'] = 1
    if not cycles and _ALL_CLEAR not in text:
        msg = 'madge printed neither a cycle list nor its all-clear line (tool crash?)'
        raise bl.ScanError(msg)
    return cycles


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_TOOL_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        msg = f'cannot run {" ".join(argv)}: {exc}'
        raise bl.ScanError(msg) from None


def run_knip(frontend: Path) -> str:
    # Generates .svelte-kit/tsconfig.json, which the vite/vitest configs extend; without it knip
    # cannot load them and reports every test file as unused.
    sync = _run(['npx', '--no-install', 'svelte-kit', 'sync'], frontend)
    if sync.returncode != 0:
        msg = f'svelte-kit sync failed ({sync.returncode}): {sync.stderr.strip()[:300]}'
        raise bl.ScanError(msg)
    proc = _run(['npx', '--no-install', 'knip', '--reporter', 'json'], frontend)
    errors = [line for line in proc.stderr.splitlines() if line.startswith('ERROR')]
    if errors:
        msg = 'knip reported errors: ' + '; '.join(errors[:3])
        raise bl.ScanError(msg)
    return proc.stdout


def run_madge(frontend: Path) -> str:
    proc = _run(['npm', 'run', 'cycles'], frontend)
    return proc.stdout + proc.stderr


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8')
    except OSError as exc:
        msg = f'cannot read {path}: {exc}'
        raise bl.ScanError(msg) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--root', type=Path, default=None, help='repo root (default: auto)')
    parser.add_argument('--baseline', type=Path, default=_DEFAULT_BASELINE, help='knip baseline')
    parser.add_argument('--madge-baseline', type=Path, default=_DEFAULT_MADGE_BASELINE)
    parser.add_argument('--knip-json', type=Path, default=None, help='use this knip output')
    parser.add_argument('--madge-output', type=Path, default=None, help='use this madge output')
    parser.add_argument('--tighten', action='store_true', help='lower or drop baseline entries')
    parser.add_argument('--cycle-reason', default='', help='reason for cycles (default: --reason)')
    bl.add_baseline_args(parser)
    args = parser.parse_args(argv)
    try:

        def frontend_dir() -> Path:
            root: Path = args.root or bl.find_root(_HERE, ('frontend/package.json',))
            return root / 'frontend'

        knip_text = _read(args.knip_json) if args.knip_json else run_knip(frontend_dir())
        madge_text = _read(args.madge_output) if args.madge_output else run_madge(frontend_dir())
        knip = parse_knip(knip_text)
        cycles = parse_madge(madge_text)
        pairs = ((args.baseline, knip), (args.madge_baseline, cycles))
        if args.write_baseline:
            code: int = bl.run_write_baseline(args, args.baseline, knip)
            if code:
                return code
            args.reason = args.cycle_reason or args.reason
            written: int = bl.run_write_baseline(args, args.madge_baseline, cycles)
            return written
        errors: list[str] = []
        for path, current in pairs:
            baseline = bl.load(path)
            if args.tighten:
                print(f'{path.name}: tightened {bl.tighten(path, current, baseline)} line(s)')
                baseline = bl.load(path)
            errors += bl.compare(current, baseline, unit='finding(s)')
    except (bl.ScanError, ValueError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    for line in errors:
        print(line)
    if errors:
        print(f'{len(errors)} knip/madge ratchet violation(s)', file=sys.stderr)
        return 1
    print(f'ok: {len(knip)} knip finding(s), {len(cycles)} cycle(s), all baselined')
    return 0


if __name__ == '__main__':
    sys.exit(main())
