#!/usr/bin/env python3
"""Guard: no new unused code (vulture, min confidence 60) in production sources.

Runs vulture over ``backend/app backend/scripts scripts``. Tests are not scanned, so code used only by tests
is reported: production code with no production caller is dead. Routes, celery tasks,
validators, signal handlers, event listeners and Locust tasks/hooks are registered by decorator
and are ignored.

Only the kinds ``function``, ``method``, ``class`` and ``import`` are ratcheted. Variables,
attributes and properties are out of scope: at this confidence vulture cannot tell an ORM
column assignment, a settings constant or a pydantic field from a dead name, so those
findings are noise rather than debt. A reviewed false positive goes into
``scripts/guards/vulture_whitelist.py`` under a comment block tagged ``[extension-seam]``, ``[test-observation]``,
``[framework-dispatch]`` or ``[operator-entrypoint]`` plus a written reason (enforced by ``whitelist_problems``); that file is passed to
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
TARGETS = ('backend/app', 'backend/scripts', 'scripts')
PINNED_VULTURE = '2.16'
MIN_CONFIDENCE = '60'
IGNORE_DECORATORS = (
    '@*router.*,@app.*,@celery_app.task,@*.connect,@field_validator,@model_validator,'
    '@computed_field,@event.listens_for,'
    # Locust (scripts/perf/load/locustfile.py) registers tasks and event hooks by decorator.
    '@task,@tag,@events.*'
)
TAGS = (
    '[extension-seam]',
    '[test-observation]',
    '[framework-dispatch]',
    '[operator-entrypoint]',
)
_MIN_REASON_CHARS = 20
_ENTRY = re.compile(r'^\s*_\.\w+,\s*$')
KINDS = frozenset({'function', 'method', 'class', 'import'})
_FINDING = re.compile(r"^(?P<path>.+?):(?P<line>\d+): unused (?P<kind>[\w ]+?) '(?P<name>.+)' \(")


def _vulture(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # fixed argv, our own interpreter  # nosec B603
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


def whitelist_problems(text: str) -> list[str]:
    """Return one message per whitelist block lacking a tag or a written reason.

    A block is the run of ``#`` comment lines directly above one or more ``_.name,`` entries
    (a blank line or a new comment run after entries starts the next block). The comment text
    must contain one of ``TAGS`` followed by at least ``_MIN_REASON_CHARS`` characters of
    reason; an entry with no comment above it has neither.
    """
    problems: list[str] = []
    comment: list[str] = []
    seen_entry = False
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith('#'):
            if seen_entry:
                comment, seen_entry = [], False
            comment.append(stripped.lstrip('#').strip())
        elif _ENTRY.match(line):
            if not seen_entry:
                reason = _tagged_reason(' '.join(comment))
                if reason is None:
                    problems.append(
                        f'{WHITELIST}:{number}: block at {stripped!r} needs a comment with one of '
                        f'{", ".join(TAGS)} and a written reason (>= {_MIN_REASON_CHARS} chars)'
                    )
            seen_entry = True
        elif not stripped:
            comment, seen_entry = [], False
    return problems


def _tagged_reason(comment: str) -> str | None:
    for tag in TAGS:
        _, found, after = comment.partition(tag)
        if found and len(after.strip()) >= _MIN_REASON_CHARS:
            return after.strip()
    return None


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
    wl = args.root / WHITELIST
    problems = whitelist_problems(wl.read_text(encoding='utf-8')) if wl.is_file() else []
    if problems:
        sys.stderr.write('ERROR: untagged vulture whitelist block(s).\n')
        for p in problems:
            sys.stderr.write(f'  {p}\n')
        return 1
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
