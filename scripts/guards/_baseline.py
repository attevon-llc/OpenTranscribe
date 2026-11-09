"""Shared reader/comparer for the ratchet baselines in ``scripts/guards/baselines/``.

A baseline file holds the violations that existed when a guard landed. Every
line is ``key<TAB>value<TAB>reason`` (``value`` is an integer: a line count, a
complexity score, an occurrence count). The guard fails when:

* a violation is not in the baseline (new debt),
* a violation's value grew past its recorded value (the debt got worse),
* a recorded value is higher than reality (loosened baseline: lower it), or
* a baseline entry no longer has a violation (stale: delete the line).

So a baseline can only shrink. ``--tighten`` on a guard rewrites it for the last
two cases; it never adds a line and never raises a value.

The one way a baseline is created is ``--write-baseline --reason TEXT`` (see
:func:`write_initial`): it records every current finding with a real written reason and
refuses to overwrite a non-empty baseline unless ``--force`` is given.
"""

from __future__ import annotations

import importlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse


class ScanError(Exception):
    """Unusable input (missing directory, unparseable file). Guards exit 2: never a silent pass."""


def find_root(start: Path, markers: tuple[str, ...]) -> Path:
    """Nearest ancestor of ``start`` (inclusive) containing one of ``markers``, else ``ScanError``.

    Guards copied to a different depth must not guess their root from ``__file__``: a wrong root
    scans an empty tree and passes.
    """
    for candidate in (start, *start.parents):
        if any((candidate / m).exists() for m in markers):
            return candidate
    msg = f'no {" or ".join(markers)} found at or above {start}; pass --root explicitly'
    raise ScanError(msg)


@dataclass(frozen=True)
class Entry:
    value: int
    reason: str


def load(path: Path) -> dict[str, Entry]:
    """Parse ``path``; a missing file is an empty baseline. Raises ``ValueError`` on a bad line."""
    if not path.exists():
        return {}
    entries: dict[str, Entry] = {}
    for lineno, raw in enumerate(path.read_text(encoding='utf-8').splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith('#'):
            continue
        parts = raw.split('\t')
        if len(parts) != 3 or not parts[0] or not parts[2].strip():
            msg = f'{path}:{lineno}: expected "key<TAB>value<TAB>reason" with a reason: {raw!r}'
            raise ValueError(msg)
        try:
            value = int(parts[1])
        except ValueError:
            msg = f'{path}:{lineno}: value must be an integer: {parts[1]!r}'
            raise ValueError(msg) from None
        if parts[0] in entries:
            msg = f'{path}:{lineno}: duplicate key {parts[0]!r}'
            raise ValueError(msg)
        entries[parts[0]] = Entry(value, parts[2].strip())
    return entries


#: Class tags an allowlist reason must start with. An allowlist holds only what is legitimate
#: forever; debt lives in the baseline and only shrinks.
ALLOWLIST_CLASSES = (
    'applied-history',
    'stub-gap',
    'extension-seam',
    'test-observation',
    'bootstrap-before-settings',
    'single-file-installer',
    'intentional-parallel',
)
_URL = re.compile(r'https?://\S+')


def load_allowlist(path: Path, classes: tuple[str, ...] = ALLOWLIST_CLASSES) -> dict[str, Entry]:
    """Parse an allowlist (same TSV as a baseline); a missing file is empty.

    Every reason must be a real written reason (``audit_tests.allowlist.is_real_reason``) that
    starts with one of ``classes``; ``stub-gap`` must also carry the upstream issue URL.
    Raises ``ValueError`` otherwise.
    """
    entries = load(path)
    for key, entry in entries.items():
        if not _is_real_reason(entry.reason):
            msg = f'{path}: allowlist entry {key!r} has no real reason: {entry.reason!r}'
            raise ValueError(msg)
        tag = next(
            (
                c
                for c in classes
                if entry.reason == c or entry.reason.startswith((c + ' ', c + ':'))
            ),
            None,
        )
        if tag is None:
            msg = f'{path}: allowlist entry {key!r} reason must start with one of {classes}'
            raise ValueError(msg)
        if tag == 'stub-gap' and _URL.search(entry.reason) is None:
            msg = f'{path}: stub-gap entry {key!r} must cite an upstream issue URL'
            raise ValueError(msg)
    return entries


def compare(
    current: dict[str, int],
    baseline: dict[str, Entry],
    *,
    unit: str,
    allowlist: dict[str, Entry] | None = None,
) -> list[str]:
    """Error lines for ``current`` (key -> value of every violation) against ``baseline``.

    A key in ``allowlist`` is accepted as legitimate up to its recorded value (more is an error)
    and must not also be in the baseline. An allowlist entry with no violation is stale and
    fails: nothing else would ever remove it, because ``--tighten`` never edits allowlists.
    """
    allowed = allowlist or {}
    errors: list[str] = []
    for key in sorted(current):
        value = current[key]
        if key in allowed:
            if key in baseline:
                errors.append(f'{key}: in both the baseline and the allowlist (keep one)')
            elif value > allowed[key].value:
                errors.append(f'{key}: {value} {unit} grew past the allowlist {allowed[key].value}')
            elif value < allowed[key].value:
                errors.append(
                    f'{key}: {value} {unit} is below the allowlist {allowed[key].value}; '
                    'edit the allowlist by hand (--tighten never touches it)'
                )
            continue
        if key not in baseline:
            errors.append(f'{key}: {value} {unit} (new violation, not in the baseline)')
        elif value > baseline[key].value:
            errors.append(f'{key}: {value} {unit} grew past the baseline {baseline[key].value}')
        elif value < baseline[key].value:
            errors.append(
                f'{key}: {value} {unit} is below the baseline {baseline[key].value}; '
                'lower it (run the guard with --tighten)'
            )
    errors.extend(
        f'{key}: stale baseline entry, no violation left (delete the line or run --tighten)'
        for key in sorted(baseline)
        if key not in current
    )
    errors.extend(
        f'{key}: stale allowlist entry, no violation left (delete the line by hand)'
        for key in sorted(allowed)
        if key not in current
    )
    return errors


def tighten(path: Path, current: dict[str, int], baseline: dict[str, Entry]) -> int:
    """Rewrite ``path`` with lowered values and without stale entries. Returns lines changed."""
    changed = 0
    lines: list[str] = []
    for raw in path.read_text(encoding='utf-8').splitlines():
        parts = raw.split('\t')
        line = raw
        if len(parts) == 3 and parts[0] in baseline:
            key = parts[0]
            if key not in current:
                changed += 1
                continue
            if current[key] < baseline[key].value:
                parts[1] = str(current[key])
                line = '\t'.join(parts)
                changed += 1
        lines.append(line)
    path.write_text('\n'.join(lines) + '\n' if lines else '', encoding='utf-8')
    return changed


def _is_real_reason(reason: str) -> bool:
    """Apply ``scripts/audit_tests``'s placeholder-reason rule, so every allowlist agrees."""
    scripts_dir = str(Path(__file__).resolve().parents[1])
    sys.path.insert(0, scripts_dir)
    try:
        module = importlib.import_module('audit_tests.allowlist')
    finally:
        sys.path.remove(scripts_dir)
    return bool(module.is_real_reason(reason.strip()))


def write_initial(path: Path, findings: dict[str, int], reason: str, *, force: bool = False) -> int:
    """Create the baseline at ``path`` from ``findings``. Returns the number of lines written.

    Raises ``ScanError`` for an empty or placeholder ``reason``, for a key or reason that would
    corrupt the TSV format, and for a non-empty existing baseline unless ``force`` is set.
    Output is sorted, so the same findings always produce the same bytes.
    """
    if not _is_real_reason(reason):
        msg = f'refusing to write a baseline without a real --reason (got {reason!r})'
        raise ScanError(msg)
    reason = reason.strip()
    if any(c in reason for c in '\t\r\n'):
        msg = 'the reason must be a single line without tabs'
        raise ScanError(msg)
    for key in findings:
        if not key or any(c in key for c in '\t\r\n'):
            msg = f'unusable baseline key {key!r}'
            raise ScanError(msg)
    if not force and path.exists() and path.read_text(encoding='utf-8').strip():
        msg = f'{path} already has content; pass --force to overwrite it (tighten only shrinks)'
        raise ScanError(msg)
    lines = [f'{key}\t{findings[key]}\t{reason}' for key in sorted(findings)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('\n'.join(lines) + '\n' if lines else '', encoding='utf-8')
    return len(lines)


def add_baseline_args(parser: argparse.ArgumentParser) -> None:
    """Add ``--write-baseline``, ``--reason TEXT`` and ``--force`` to a guard's parser."""
    parser.add_argument(
        '--write-baseline',
        action='store_true',
        help='record every current finding in the baseline (requires --reason)',
    )
    parser.add_argument('--reason', default='', help='why these findings are accepted debt')
    parser.add_argument(
        '--force',
        action='store_true',
        help='overwrite a non-empty baseline (with --write-baseline)',
    )


def run_write_baseline(args: argparse.Namespace, path: Path, findings: dict[str, int]) -> int:
    """Handle ``--write-baseline`` for a guard. Returns the process exit code (0, or 2 on refusal)."""
    try:
        count = write_initial(path, findings, args.reason, force=args.force)
    except ScanError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    print(f'wrote {count} baseline entries to {path}')
    return 0
