#!/usr/bin/env python3
"""Ratchet guard: mypy over the tooling packages under ``scripts/`` may not gain errors.

The mypy pre-commit hook only sees ``backend/``; the guards, shared libraries and audit tooling
that decide whether a commit may land were never type-checked. This runs the pinned mypy with the
repo config over ``scripts/{guards,lib,audit_tests,model_download,cleanup_test_data}`` (the ones
that exist) and ratchets the ``path::error-code`` counts against
``baselines/scripts_mypy.txt``. The baseline only shrinks (``--tighten``); a mypy crash or a
version other than the ``backend/requirements-dev.txt`` pin exits 2.

Usage::

    check_scripts_mypy.py [--root DIR] [--baseline FILE]
    check_scripts_mypy.py --tighten
    check_scripts_mypy.py --write-baseline --reason TEXT [--force]
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _baseline
import check_hidden_mypy as hidden

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ('guards', 'lib', 'audit_tests', 'model_download', 'cleanup_test_data')


def _mypy_env(root: Path) -> dict[str, str]:
    """The environment mypy runs in, with the import roots the tooling scripts rely on.

    These scripts import each other as top-level modules (``cleanup_test_data.api``,
    ``_baseline``), which works at run time because the interpreter puts the script's directory
    on ``sys.path``. mypy does not know that, treats the import as missing and (with
    ``ignore_missing_imports``) types it ``Any``, silently skipping every cross-module check.
    ``MYPYPATH`` names those roots; see mypy docs, "Running mypy and managing imports" >
    "How imports are found" (https://mypy.readthedocs.io/en/stable/running_mypy.html).
    """
    roots = [root / 'scripts', root / 'scripts' / 'guards']
    existing = os.environ.get('MYPYPATH')
    parts = [str(p) for p in roots] + ([existing] if existing else [])
    return {**os.environ, 'MYPYPATH': os.pathsep.join(parts)}


def scripts_errors(root: Path) -> dict[str, int]:
    """Run mypy over the existing tooling packages; ``path::code`` -> error count."""
    hidden.check_mypy_version(root)
    targets = [f'scripts/{p}' for p in PACKAGES if (root / 'scripts' / p).is_dir()]
    if not targets:
        msg = f'none of the scripts/ tooling packages {PACKAGES} exist under {root}'
        raise _baseline.ScanError(msg)
    with tempfile.TemporaryDirectory(prefix='ot-scripts-mypy.') as cache:
        proc = subprocess.run(  # nosec B603  our own interpreter
            [
                sys.executable,
                '-m',
                'mypy',
                '--config-file',
                'pyproject.toml',
                '--cache-dir',
                cache,
                '--no-color-output',
                '--no-error-summary',
                '--show-error-codes',
                '--no-pretty',
                *targets,
            ],
            cwd=root,
            env=_mypy_env(root),
            capture_output=True,
            text=True,
            check=False,
        )
    if proc.returncode not in (0, 1):
        msg = f'mypy crashed (exit {proc.returncode}): {(proc.stderr or proc.stdout).strip()[:500]}'
        raise _baseline.ScanError(msg)
    found: dict[str, int] = hidden.parse_mypy_output(proc.stdout)
    if proc.returncode == 1 and not found:
        msg = f'mypy exited 1 but no error line could be parsed: {proc.stdout[:500]!r}'
        raise _baseline.ScanError(msg)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--root', type=Path, default=REPO_ROOT)
    parser.add_argument('--baseline', type=Path, default=None)
    parser.add_argument('--tighten', action='store_true', help='lower values / drop stale entries')
    _baseline.add_baseline_args(parser)
    args = parser.parse_args(argv)
    path = args.baseline or args.root / 'scripts/guards/baselines/scripts_mypy.txt'
    try:
        current = scripts_errors(args.root)
        if args.write_baseline:
            code: int = _baseline.run_write_baseline(args, path, current)
            return code
        baseline = _baseline.load(path)
    except (_baseline.ScanError, ValueError, OSError) as exc:
        sys.stderr.write(f'error: {exc}\n')
        return 2
    if args.tighten:
        changed = _baseline.tighten(path, current, baseline) if path.exists() else 0
        sys.stderr.write(f'tightened {changed} line(s) in {path.name}\n')
        baseline = _baseline.load(path)
    errors = _baseline.compare(current, baseline, unit='mypy error(s)')
    if errors:
        sys.stderr.write(
            'scripts mypy ratchet failed; fix the typing, the baseline only shrinks:\n'
        )
        for line in errors:
            sys.stderr.write(f'  {line}\n')
        return 1
    sys.stdout.write(f'scripts mypy: {sum(current.values())} error(s), within the baseline\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
