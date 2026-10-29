#!/usr/bin/env python3
"""Report packages whose installed version differs from the exact pin in the requirements files.

Reads ``name==version`` pins from ``backend/requirements.txt``, ``requirements-dev.txt`` and
``requirements-nodeps.txt`` (or the files given with ``--requirements``) and compares each with
the version installed in the interpreter running this script, so run it with the venv's python.

Exit codes: 0 no drift, 1 drift found, 2 a requirements file is unreadable.
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import re
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / 'backend'
DEFAULT_FILES = ('requirements.txt', 'requirements-dev.txt', 'requirements-nodeps.txt')

_PIN = re.compile(r'^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*==\s*([^\s;#]+)\s*(;.*)?$')


def normalise(name: str) -> str:
    """PEP 503 name normalisation."""
    return re.sub(r'[-_.]+', '-', name).lower()


def parse_pins(path: Path) -> tuple[list[tuple[str, str]], list[str]]:
    """Return ``(pins, notes)`` for one requirements file; raises OSError if unreadable."""
    pins: list[tuple[str, str]] = []
    notes: list[str] = []
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.split(' #', 1)[0].strip()
        if not line or line.startswith(('#', '-')):
            continue
        match = _PIN.match(line)
        if match is None:
            continue
        name, version, marker = match.groups()
        if marker:
            notes.append(f'note: {path.name}: skipped {name} (environment marker not evaluated)')
            continue
        pins.append((name, version))
    return pins, notes


def satisfies(have: str, pinned: str) -> bool:
    """PEP 440 ``==``: a pin without a local label matches any local label (2.11.0 ~ 2.11.0+cu128)."""
    if '+' not in pinned:
        have = have.split('+', 1)[0]
    return have == pinned


def installed_version(name: str) -> str | None:
    try:
        return md.version(normalise(name))
    except md.PackageNotFoundError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        '--requirements',
        nargs='+',
        type=Path,
        help='requirements files to check instead of the backend defaults',
    )
    args = parser.parse_args(argv)
    files = args.requirements or [BACKEND / f for f in DEFAULT_FILES]

    drift = 0
    for path in files:
        try:
            pins, notes = parse_pins(path)
        except (OSError, UnicodeDecodeError) as exc:
            print(f'error: cannot read {path}: {exc}', file=sys.stderr)
            return 2
        for note in notes:
            print(note)
        for name, pinned in pins:
            have = installed_version(name)
            if have is None:
                print(f'{normalise(name)} not installed != {pinned}  ({path.name})')
                drift += 1
            elif not satisfies(have, pinned):
                print(f'{normalise(name)} {have} != {pinned}  ({path.name})')
                drift += 1
    if drift:
        print(f'{drift} package(s) drifted from the pinned versions')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
