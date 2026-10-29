#!/usr/bin/env python3
"""Guard: per-function complexity and per-file broad-except counts may not grow.

Two ratchets over ``backend/app scripts backend/alembic`` (tests excluded), both read
from ruff run with ``--isolated --ignore-noqa`` so neither a config ignore nor a
``noqa`` comment can hide a finding:

* ``C901`` at 15: key ``path::qualname`` (``#2``, ``#3`` for a repeated qualname in one
  file, in source order), value = McCabe score. A function over 15 must be in
  ``baselines/complexity.txt``; it may not grow. The repo's own ``max-complexity``
  setting is deliberately left alone: this cap is stricter and independent of it.
* ``BLE001`` (broad ``except``): key ``path``, value = count in the file, in
  ``baselines/broad_except.txt``; it may not grow.

Baselines can only shrink (see ``_baseline.py``); ``--tighten`` lowers them and
``--write-baseline --reason TEXT`` creates them. Any ruff failure, a version other than
the pinned one, or an unparsable file fails closed (exit 2).
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _baseline

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINES = REPO_ROOT / 'scripts' / 'guards' / 'baselines'
TARGETS = ('backend/app', 'scripts', 'backend/alembic')
PINNED_RUFF = '0.16.7'
MAX_COMPLEXITY = 15
_SCORE = re.compile(r'`(?P<name>[^`]+)` is too complex \((?P<score>\d+) >')


def _ruff(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603  fixed argv, our own interpreter
        [sys.executable, '-m', 'ruff', *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def check_ruff_version(root: Path) -> None:
    """Raise ``ScanError`` unless the ruff in use is exactly the pinned version."""
    proc = _ruff(root, '--version')
    found = proc.stdout.strip()
    if proc.returncode != 0 or found != f'ruff {PINNED_RUFF}':
        msg = f'ruff {PINNED_RUFF} required, got {found or proc.stderr.strip()!r}'
        raise _baseline.ScanError(msg)


def run_ruff(root: Path, *rule_args: str) -> list[dict[str, object]]:
    """Run ruff over the targets; raise ``ScanError`` on a crash or an unparsable file."""
    for target in TARGETS:
        if not (root / target).is_dir():
            msg = f'scan target {target} is missing under {root}'
            raise _baseline.ScanError(msg)
    proc = _ruff(
        root,
        'check',
        '--isolated',
        '--ignore-noqa',
        '--no-cache',
        '--output-format',
        'json',
        *rule_args,
        *TARGETS,
    )
    if proc.returncode not in (0, 1):
        msg = f'ruff failed (exit {proc.returncode}): {proc.stderr.strip()}'
        raise _baseline.ScanError(msg)
    try:
        diagnostics: list[dict[str, object]] = json.loads(proc.stdout or '[]')
    except json.JSONDecodeError as exc:
        msg = f'ruff produced unreadable output: {exc}'
        raise _baseline.ScanError(msg) from exc
    syntax = [d for d in diagnostics if d.get('code') in (None, 'invalid-syntax')]
    if syntax:
        names = ', '.join(sorted({str(d['filename']) for d in syntax}))
        msg = f'ruff could not parse: {names}'
        raise _baseline.ScanError(msg)
    return diagnostics


def _rel(root: Path, filename: object) -> str:
    return Path(str(filename)).resolve().relative_to(root.resolve()).as_posix()


def _qualnames(path: Path) -> dict[tuple[int, str], str]:
    """Map ``(def line, bare name)`` to the dotted qualified name for every function."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    found: dict[tuple[int, str], str] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, f'{prefix}{child.name}.')
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                found[(child.lineno, child.name)] = f'{prefix}{child.name}'
                walk(child, f'{prefix}{child.name}.<locals>.')
            else:
                walk(child, prefix)

    walk(tree, '')
    return found


def complexity_violations(root: Path) -> dict[str, int]:
    rows = run_ruff(
        root,
        '--select',
        'C901',
        '--config',
        f'lint.mccabe.max-complexity={MAX_COMPLEXITY}',
    )
    rows.sort(key=lambda d: (str(d['filename']), d['location']['row']))  # type: ignore[index]
    names: dict[str, dict[tuple[int, str], str]] = {}
    found: dict[str, int] = {}
    for d in rows:
        match = _SCORE.search(str(d['message']))
        if match is None:
            msg = f'unrecognised C901 message: {d["message"]!r}'
            raise _baseline.ScanError(msg)
        filename = str(d['filename'])
        if filename not in names:
            names[filename] = _qualnames(Path(filename))
        row = d['location']['row']  # type: ignore[index]
        qual = names[filename].get((row, match['name']), match['name'])
        base = f'{_rel(root, filename)}::{qual}'
        key, n = base, 1
        while key in found:
            n += 1
            key = f'{base}#{n}'
        found[key] = int(match['score'])
    return found


def broad_except_counts(root: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for d in run_ruff(root, '--select', 'BLE001'):
        rel = _rel(root, d['filename'])
        counts[rel] = counts.get(rel, 0) + 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--complexity-baseline', type=Path, default=BASELINES / 'complexity.txt')
    parser.add_argument(
        '--broad-except-baseline', type=Path, default=BASELINES / 'broad_except.txt'
    )
    parser.add_argument('--tighten', action='store_true', help='Lower baseline values, drop stale.')
    parser.add_argument('--root', type=Path, default=REPO_ROOT)
    _baseline.add_baseline_args(parser)
    parser.add_argument(
        '--only',
        choices=('complexity', 'broad_except'),
        help='with --write-baseline: write just this baseline (each needs its own reason)',
    )
    args = parser.parse_args(argv)

    try:
        check_ruff_version(args.root)
        complexity = complexity_violations(args.root)
        broad = broad_except_counts(args.root)
    except (_baseline.ScanError, SyntaxError, OSError) as exc:
        sys.stderr.write(f'ERROR: {exc}\n')
        return 2

    if args.write_baseline:
        if args.only != 'broad_except':
            code: int = _baseline.run_write_baseline(args, args.complexity_baseline, complexity)
            if code or args.only:
                return code
        written: int = _baseline.run_write_baseline(args, args.broad_except_baseline, broad)
        return written

    errors: list[str] = []
    for label, current, path, unit in (
        ('complexity above 15', complexity, args.complexity_baseline, 'CC'),
        ('broad except', broad, args.broad_except_baseline, 'handler(s)'),
    ):
        try:
            baseline = _baseline.load(path)
        except ValueError as exc:
            sys.stderr.write(f'ERROR: {exc}\n')
            return 2
        found = _baseline.compare(current, baseline, unit=unit)
        if args.tighten:
            changed = _baseline.tighten(path, current, baseline) if path.exists() else 0
            sys.stderr.write(f'tightened {changed} line(s) in {path.name}\n')
            found = [e for e in found if 'new violation' in e or 'grew past' in e]
        errors += [f'[{label}] {e}' for e in found]

    if errors:
        sys.stderr.write(
            'ERROR: complexity / broad-except ratchet failed. Decompose the function or '
            'catch the specific exception; the baselines only shrink.\n'
        )
        for e in errors:
            sys.stderr.write(f'  {e}\n')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
