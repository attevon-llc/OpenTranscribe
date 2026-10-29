#!/usr/bin/env python3
"""Ratchet for frontend ESLint warnings.

Runs ``npx eslint . -f json`` in ``frontend/`` and counts messages at warning severity per
``path::ruleId`` (path relative to the repo root). The counts are compared with
``baselines/eslint.txt``: they may only shrink. An error-severity message is always a failure
and is never baselined. Any eslint output that is not parseable fails closed (exit 2).

Usage:
    check_eslint_ratchet.py [--root DIR] [--baseline FILE] [--eslint-json PATH]
                            [--timeout SEC] [--tighten]
    check_eslint_ratchet.py --write-baseline --reason TEXT [--force]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path


def _load_lib():
    """Import the sibling ``_baseline`` module whatever the caller's cwd or sys.path."""
    path = Path(__file__).resolve().parent / '_baseline.py'
    spec = importlib.util.spec_from_file_location('_baseline', path)
    if spec is None or spec.loader is None:
        raise ImportError(f'cannot load {path}')
    module = sys.modules.get(spec.name) or importlib.util.module_from_spec(spec)
    if spec.name not in sys.modules:
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


bl = _load_lib()

DEFAULT_TIMEOUT = 900


def _run_eslint(root: Path, timeout: int) -> str:
    """Run eslint in ``frontend/`` and return stdout. Exit 0 and 1 are both valid results."""
    frontend = root / 'frontend'
    if not frontend.is_dir():
        raise bl.ScanError(f'{frontend} is not a directory')
    try:
        proc = subprocess.run(
            ['npx', '--no-install', 'eslint', '.', '-f', 'json'],
            cwd=frontend,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise bl.ScanError(f'eslint did not run: {exc}') from exc
    if proc.returncode not in (0, 1):
        raise bl.ScanError(f'eslint exited {proc.returncode}: {proc.stderr.strip()[:500]}')
    return proc.stdout


def parse(raw: str, root: Path) -> tuple[dict[str, int], list[str]]:
    """Return ``(warning counts by path::ruleId, error lines)`` from eslint JSON output."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise bl.ScanError(f'eslint output is not JSON: {exc}') from exc
    if not isinstance(data, list):
        raise bl.ScanError('eslint JSON output is not a list of file results')
    counts: Counter[str] = Counter()
    errors: list[str] = []
    for result in data:
        if not isinstance(result, dict) or 'filePath' not in result or 'messages' not in result:
            raise bl.ScanError('eslint JSON entry lacks filePath/messages')
        file_path = Path(result['filePath'])
        try:
            rel = file_path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel = file_path.as_posix()
        for msg in result['messages']:
            rule = msg.get('ruleId') or 'parse-error'
            severity = msg.get('severity')
            if severity == 2:
                errors.append(f'{rel}:{msg.get("line")}: error {rule}: {msg.get("message")}')
            elif severity == 1:
                counts[f'{rel}::{rule}'] += 1
            else:
                raise bl.ScanError(f'unknown eslint severity {severity!r} in {rel}')
    return dict(counts), errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--root', type=Path, default=None, help='repo root (default: auto-detect)')
    parser.add_argument('--baseline', type=Path, default=None)
    parser.add_argument(
        '--eslint-json', type=Path, default=None, help='read a prior eslint -f json run'
    )
    parser.add_argument('--timeout', type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument('--tighten', action='store_true', help='lower values / drop stale entries')
    bl.add_baseline_args(parser)
    args = parser.parse_args(argv)

    try:
        root = args.root or bl.find_root(
            Path(__file__).resolve().parent, ('frontend/eslint.config.js',)
        )
        baseline_path = (
            args.baseline or Path(__file__).resolve().parent / 'baselines' / 'eslint.txt'
        )
        if args.eslint_json:
            raw = args.eslint_json.read_text(encoding='utf-8')
        else:
            raw = _run_eslint(root, args.timeout)
        current, errors = parse(raw, root)
        if args.write_baseline:
            if errors:
                print('\n'.join(errors), file=sys.stderr)
                print('error: eslint errors are never baselined', file=sys.stderr)
                return 1
            written: int = bl.run_write_baseline(args, baseline_path, current)
            return written
        baseline = bl.load(baseline_path)
    except (bl.ScanError, ValueError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2

    problems = list(errors)
    problems.extend(bl.compare(current, baseline, unit='warnings'))
    if args.tighten and not errors:
        loosened = [k for k in current if k not in baseline or current[k] > baseline[k].value]
        if loosened:
            print('\n'.join(bl.compare(current, baseline, unit='warnings')), file=sys.stderr)
            return 1
        changed = bl.tighten(baseline_path, current, baseline)
        print(f'tightened {changed} baseline entries')
        return 0
    if problems:
        print('\n'.join(problems), file=sys.stderr)
        print(f'{len(problems)} eslint ratchet problem(s)', file=sys.stderr)
        return 1
    print(f'eslint ratchet ok: {sum(current.values())} warnings in {len(current)} buckets')
    return 0


if __name__ == '__main__':
    sys.exit(main())
