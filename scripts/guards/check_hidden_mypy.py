#!/usr/bin/env python3
"""Ratchet guard: what ``# type: ignore`` comments hide from mypy may not grow.

``warn_unused_ignores`` only reports an ignore that suppresses nothing; one ``[arg-type]``
ignore can hide any number of errors on its line (``rate_limit.py:220`` hid seven). This guard
measures the hidden errors: it copies the tracked ``backend/`` Python tree and the root
``pyproject.toml`` to a temporary directory, strips every ``type: ignore`` directive (read with
``tokenize``, so string literals are never touched), runs the pinned mypy with the repo config over
the copy, and compares the ``(path, error code) -> count`` multiset with
``baselines/hidden_mypy.txt`` (key ``path::code``). The baseline can only shrink;
``allowlists/hidden_mypy.txt`` holds a genuine stub gap (class ``stub-gap`` with an upstream URL).

Fail-closed: a mypy other than the version pinned in ``backend/requirements-dev.txt``, a mypy
crash (exit 2) or output that cannot be parsed exits 2, never a silent pass.

Usage::

    check_hidden_mypy.py [--root DIR] [--baseline FILE] [--allowlist FILE]
    check_hidden_mypy.py --tighten
    check_hidden_mypy.py --write-baseline --reason TEXT [--force]
"""

from __future__ import annotations

import argparse
import io
import re
import subprocess
import sys
import tempfile
import tokenize
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _baseline

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE = REPO_ROOT / 'scripts' / 'guards' / 'baselines' / 'hidden_mypy.txt'
ALLOWLIST = REPO_ROOT / 'scripts' / 'guards' / 'allowlists' / 'hidden_mypy.txt'
TARGET = 'backend'
_DIRECTIVE = re.compile(r'\s*#?\s*type:\s*ignore(?:\[[^\]]*\])?[ \t]*')
_ERROR = re.compile(
    r'^(?P<path>[^:\n]+\.pyi?):(?P<line>\d+):(?:\d+:)? error: .*?(?:\s+\[(?P<code>[\w-]+)\])?$'
)
_PIN = re.compile(r'^mypy==(?P<version>[\w.]+)\s*(?:#.*)?$', re.MULTILINE)


def strip_type_ignores(source: str) -> str:
    """``source`` with every ``type: ignore`` directive removed from its comments.

    Line numbers are preserved. Other text in the same comment (``# noqa: X  # type: ignore[y]``,
    a trailing reason) is kept; a comment left empty is dropped with its leading blanks.
    """
    lines = source.splitlines(keepends=True)
    edits: list[tuple[int, int, str]] = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT and 'ignore' in tok.string:
            new = _DIRECTIVE.sub(' ', tok.string).strip()
            if new != tok.string:
                edits.append((tok.start[0] - 1, tok.start[1], new))
    for row, col, new in edits:
        line = lines[row]
        ending = line[len(line.rstrip('\r\n')) :]
        head = line[:col]
        if new in ('', '#'):
            lines[row] = head.rstrip() + ending
        else:
            lines[row] = head + (new if new.startswith('#') else '# ' + new) + ending
    return ''.join(lines)


def parse_mypy_output(output: str) -> dict[str, int]:
    """Map ``path::code`` -> error count from mypy's ``--show-error-codes`` output."""
    counts: dict[str, int] = {}
    for line in output.splitlines():
        match = _ERROR.match(line)
        if match is None:
            continue
        key = f'{match["path"]}::{match["code"] or "no-code"}'
        counts[key] = counts.get(key, 0) + 1
    return counts


def tracked_python(root: Path) -> list[str]:
    try:
        proc = subprocess.run(  # nosec B603 B607  fixed argv
            ['git', '-C', str(root), 'ls-files', '-z', '--', f'{TARGET}/*.py', f'{TARGET}/*.pyi'],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        msg = f'git ls-files failed in {root}: {exc}'
        raise _baseline.ScanError(msg) from exc
    return [p for p in proc.stdout.decode('utf-8').split('\0') if p]


def check_mypy_version(root: Path) -> None:
    """Raise ``ScanError`` unless the mypy in use is the one pinned in ``requirements-dev.txt``."""
    try:
        pinned = _PIN.search((root / 'backend' / 'requirements-dev.txt').read_text('utf-8'))
    except OSError as exc:
        msg = f'cannot read backend/requirements-dev.txt: {exc}'
        raise _baseline.ScanError(msg) from exc
    if pinned is None:
        msg = 'backend/requirements-dev.txt has no exact mypy== pin'
        raise _baseline.ScanError(msg)
    proc = subprocess.run(  # nosec B603  our own interpreter
        [sys.executable, '-m', 'mypy', '--version'], capture_output=True, text=True, check=False
    )
    found = proc.stdout.split()[1] if proc.returncode == 0 and proc.stdout.split() else ''
    if found != pinned['version']:
        msg = f'mypy {pinned["version"]} required, got {found or proc.stderr.strip()!r}'
        raise _baseline.ScanError(msg)


def hidden_errors(root: Path) -> dict[str, int]:
    """Run mypy over a stripped copy of the tracked tree; ``path::code`` -> error count."""
    check_mypy_version(root)
    files = tracked_python(root)
    if not files:
        msg = f'no tracked Python files under {TARGET}/ in {root}'
        raise _baseline.ScanError(msg)
    with tempfile.TemporaryDirectory(prefix='ot-hidden-mypy.') as tmp:
        copy = Path(tmp) / 'tree'
        for rel in [*files, 'pyproject.toml']:
            dest = copy / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            text = (root / rel).read_text(encoding='utf-8')
            try:
                dest.write_text(
                    strip_type_ignores(text) if rel != 'pyproject.toml' else text, encoding='utf-8'
                )
            except (tokenize.TokenError, SyntaxError) as exc:
                msg = f'cannot tokenise {rel}: {exc}'
                raise _baseline.ScanError(msg) from exc
        proc = subprocess.run(  # nosec B603  our own interpreter
            [
                sys.executable,
                '-m',
                'mypy',
                '--config-file',
                'pyproject.toml',
                '--cache-dir',
                str(Path(tmp) / 'cache'),
                '--no-color-output',
                '--no-error-summary',
                '--show-error-codes',
                '--no-pretty',
                TARGET,
            ],
            cwd=copy,
            capture_output=True,
            text=True,
            check=False,
        )
    if proc.returncode not in (0, 1):
        msg = f'mypy crashed (exit {proc.returncode}): {(proc.stderr or proc.stdout).strip()[:500]}'
        raise _baseline.ScanError(msg)
    found = parse_mypy_output(proc.stdout)
    if proc.returncode == 1 and not found:
        msg = f'mypy exited 1 but no error line could be parsed: {proc.stdout[:500]!r}'
        raise _baseline.ScanError(msg)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--root', type=Path, default=REPO_ROOT)
    parser.add_argument('--baseline', type=Path, default=None)
    parser.add_argument('--allowlist', type=Path, default=None)
    parser.add_argument('--tighten', action='store_true', help='lower values / drop stale entries')
    _baseline.add_baseline_args(parser)
    args = parser.parse_args(argv)
    baseline_path = args.baseline or args.root / 'scripts/guards/baselines/hidden_mypy.txt'
    allow_path = args.allowlist or args.root / 'scripts/guards/allowlists/hidden_mypy.txt'
    try:
        current = hidden_errors(args.root)
        if args.write_baseline:
            code: int = _baseline.run_write_baseline(args, baseline_path, current)
            return code
        baseline = _baseline.load(baseline_path)
        allowlist = _baseline.load_allowlist(allow_path)
    except (_baseline.ScanError, ValueError, OSError) as exc:
        sys.stderr.write(f'error: {exc}\n')
        return 2
    if args.tighten:
        changed = (
            _baseline.tighten(baseline_path, current, baseline) if baseline_path.exists() else 0
        )
        sys.stderr.write(f'tightened {changed} line(s) in {baseline_path.name}\n')
        baseline = _baseline.load(baseline_path)
    errors = _baseline.compare(current, baseline, unit='hidden mypy error(s)', allowlist=allowlist)
    if errors:
        sys.stderr.write(
            'hidden-mypy ratchet failed. A type: ignore hides these errors; fix the typing '
            '(stub gaps go in allowlists/hidden_mypy.txt with an upstream URL):\n'
        )
        for line in errors:
            sys.stderr.write(f'  {line}\n')
        return 1
    sys.stdout.write(
        f'hidden mypy: {sum(current.values())} hidden error(s) in {len(current)} keys, '
        'within the baseline\n'
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
