#!/usr/bin/env python3
"""Guard: no new unused code (vulture, min confidence 60) in production sources.

Runs vulture over ``backend/app scripts``. Tests are not scanned, so code used only by tests
is reported: production code with no production caller is dead. Routes, celery tasks,
validators, signal handlers and event listeners are registered by decorator and are ignored.

Only the kinds ``function``, ``method``, ``class`` and ``import`` are ratcheted. Variables,
attributes and properties are out of scope: at this confidence vulture cannot tell an ORM
column assignment, a settings constant or a pydantic field from a dead name, so those
findings are noise rather than debt. A reviewed false positive goes into
``scripts/guards/vulture_whitelist.py`` with a reason on its line; that file is passed to
vulture as an extra path so its names count as used.

Findings that existed when the guard landed live in ``baselines/vulture.txt`` as
``path::kind::name``; the file can only shrink (see ``_baseline.py``). Any vulture failure,
a version other than the pinned one, or an unparsable file fails closed (exit 2).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _baseline

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = REPO_ROOT / 'scripts' / 'guards' / 'baselines' / 'vulture.txt'
WHITELIST = 'scripts/guards/vulture_whitelist.py'
TARGETS = ('backend/app', 'scripts')
PINNED_VULTURE = '2.16'
MIN_CONFIDENCE = '60'
IGNORE_DECORATORS = (
    '@*router.*,@app.*,@celery_app.task,@*.connect,@field_validator,@model_validator,'
    '@computed_field,@event.listens_for'
)
KINDS = frozenset({'function', 'method', 'class', 'import'})
_FINDING = re.compile(r"^(?P<path>.+?):(?P<line>\d+): unused (?P<kind>[\w ]+?) '(?P<name>.+)' \(")


def _vulture(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603  fixed argv, our own interpreter
        [sys.executable, '-m', 'vulture', *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def check_vulture_version(root: Path) -> None:
    """Raise ``ScanError`` unless the vulture in use is exactly the pinned version."""
    proc = _vulture(root, '--version')
    found = proc.stdout.strip()
    if proc.returncode != 0 or found != f'vulture {PINNED_VULTURE}':
        msg = f'vulture {PINNED_VULTURE} required, got {found or proc.stderr.strip()!r}'
        raise _baseline.ScanError(msg)


def findings(root: Path) -> dict[str, int]:
    """Map ``path::kind::name`` to its occurrence count, class-body fields excluded."""
    targets = [t for t in TARGETS if (root / t).is_dir()]
    if not targets:
        msg = f'none of the scan targets {TARGETS} exist under {root}'
        raise _baseline.ScanError(msg)
    extra = [WHITELIST] if (root / WHITELIST).is_file() else []
    proc = _vulture(
        root,
        *targets,
        *extra,
        '--min-confidence',
        MIN_CONFIDENCE,
        '--ignore-decorators',
        IGNORE_DECORATORS,
    )
    if proc.returncode not in (0, 3):
        msg = (
            f'vulture failed (exit {proc.returncode}): {proc.stderr.strip() or proc.stdout.strip()}'
        )
        raise _baseline.ScanError(msg)
    found: dict[str, int] = {}
    for raw in proc.stdout.splitlines():
        match = _FINDING.match(raw)
        if match is None:
            msg = f'unrecognised vulture output: {raw!r}'
            raise _baseline.ScanError(msg)
        path, kind, name = Path(match['path']).as_posix(), match['kind'], match['name']
        if path == WHITELIST or kind not in KINDS:
            continue
        key = f'{path}::{kind}::{name}'
        found[key] = found.get(key, 0) + 1
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, default=BASELINE_PATH)
    parser.add_argument('--tighten', action='store_true', help='Lower baseline values, drop stale.')
    parser.add_argument('--root', type=Path, default=REPO_ROOT)
    _baseline.add_baseline_args(parser)
    args = parser.parse_args(argv)

    try:
        check_vulture_version(args.root)
        found = findings(args.root)
    except (_baseline.ScanError, SyntaxError, OSError) as exc:
        sys.stderr.write(f'ERROR: {exc}\n')
        return 2
    if args.write_baseline:
        code: int = _baseline.run_write_baseline(args, args.baseline, found)
        return code
    try:
        baseline = _baseline.load(args.baseline)
    except ValueError as exc:
        sys.stderr.write(f'ERROR: {exc}\n')
        return 2
    errors = _baseline.compare(found, baseline, unit='finding(s)')
    if args.tighten:
        changed = _baseline.tighten(args.baseline, found, baseline) if args.baseline.exists() else 0
        sys.stderr.write(f'tightened {changed} baseline line(s)\n')
        errors = [e for e in errors if 'new violation' in e or 'grew past' in e]
    if errors:
        sys.stderr.write(
            'ERROR: unused code. Delete it, or if a framework calls it by name add it to '
            f'{WHITELIST} with the reason.\n'
        )
        for e in errors:
            sys.stderr.write(f'  {e}\n')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
