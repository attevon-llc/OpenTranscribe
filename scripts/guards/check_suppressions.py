#!/usr/bin/env python3
"""Ratchet guard: lint suppressions must name a rule, and ``noqa`` must say why.

A bare suppression silences every present and future finding on its line. Counted per
``kind::path`` (value: number of offending suppressions) and recorded in
``scripts/guards/baselines/suppressions.txt``; the baseline can only shrink.

Offending forms, by kind:

* ``noqa``: ``# noqa`` (or ``# ruff: noqa``) with no rule code, or with codes but no trailing
  written reason (``# noqa: BLE001 - reason``).
* ``type-ignore``: ``# type: ignore`` without ``[error-code]``.
* ``nosec``: ``# nosec`` without a bandit test id (``# nosec B603``).
* ``eslint-disable``: any form (file, line, next-line) without a rule name.
* ``ts-ignore``: every ``@ts-ignore`` (use ``@ts-expect-error`` with a reason instead).
* ``svelte-ignore``: ``svelte-ignore`` without a rule.

Scope: tracked files (``git ls-files``) under ``backend/``, ``scripts/`` (``.py``, read with
``tokenize`` so string literals are never matched) and ``frontend/src/`` (``.ts .js .svelte``,
matched on comment syntax). Known limit: a string literal in a ts/svelte file that merely looks
like a comment (``'// @ts-ignore'``) is matched too; only Python is tokenised. A file that
cannot be read or tokenised fails the run (exit 2).

Usage::

    check_suppressions.py [--root DIR] [--baseline FILE]
    check_suppressions.py --tighten
    check_suppressions.py --write-baseline --reason TEXT [--force]
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import re
import subprocess
import sys
import tokenize
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PY_PREFIXES = ('backend/', 'scripts/')
_WEB_PREFIX = 'frontend/src/'
_WEB_SUFFIXES = ('.ts', '.js', '.svelte')


def _load_baseline_lib():
    path = _HERE / '_baseline.py'
    spec = importlib.util.spec_from_file_location('_guard_baseline_suppressions', path)
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

# --- Python comments -------------------------------------------------------------------------

_NOQA = re.compile(r'(?:^|#)\s*(?:ruff:\s*)?noqa\b(?P<tail>[^\n]*)', re.IGNORECASE)
_NOQA_CODES = re.compile(
    r'\s*:\s*(?P<codes>[A-Z]+[0-9]+(?:[\s,]+[A-Z]+[0-9]+)*)(?P<rest>.*)', re.DOTALL
)
# Other directives that may follow a noqa on the same line; they are not a written reason.
_OTHER_DIRECTIVES = re.compile(
    r'type:\s*ignore(?:\[[^\]]*\])?|nosec\b[^#]*|pragma:[^#]*|pylint:[^#]*|fmt:\s*\w+',
    re.IGNORECASE,
)
_TYPE_IGNORE = re.compile(r'(?:^|#)\s*type:\s*ignore\b(?P<tail>[^\n]*)')
_TYPE_IGNORE_CODE = re.compile(r'\[\s*[\w-]+(?:\s*,\s*[\w-]+)*\s*\]')
_NOSEC = re.compile(r'(?:^|#)\s*nosec\b(?P<tail>[^\n]*)', re.IGNORECASE)
_NOSEC_ID = re.compile(r'\s*:?\s*B\d{3}\b')


def _has_reason(text: str) -> bool:
    text = _OTHER_DIRECTIVES.sub(' ', text)
    return re.search(r'[A-Za-z]{3,}', text) is not None


def classify_python_comment(comment: str) -> list[str]:
    """Kinds of offending suppression in one ``#`` comment token."""
    kinds: list[str] = []
    noqa = _NOQA.search(comment)
    if noqa:
        codes = _NOQA_CODES.match(noqa.group('tail'))
        if codes is None or not _has_reason(codes.group('rest')):
            kinds.append('noqa')
    ignore = _TYPE_IGNORE.search(comment)
    if ignore and not _TYPE_IGNORE_CODE.match(ignore.group('tail').lstrip()):
        kinds.append('type-ignore')
    nosec = _NOSEC.search(comment)
    if nosec and not _NOSEC_ID.match(nosec.group('tail')):
        kinds.append('nosec')
    return kinds


def count_python(source: str, rel: str) -> dict[str, int]:
    """Offending suppressions per kind in Python ``source``; strings are never matched."""
    counts: dict[str, int] = {}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                for kind in classify_python_comment(tok.string):
                    counts[kind] = counts.get(kind, 0) + 1
    except (tokenize.TokenError, SyntaxError) as exc:
        msg = f'cannot tokenise {rel}: {exc}'
        raise bl.ScanError(msg) from exc
    return counts


# --- TypeScript / JavaScript / Svelte comments -------------------------------------------------

_OPEN = r'(?://|/\*|<!--)'
_ESLINT = re.compile(rf'{_OPEN}\s*eslint-disable(?P<form>-next-line|-line)?\b(?P<tail>[^\n]*)')
_TS_IGNORE = re.compile(r'(?://|/\*)\s*@ts-ignore\b')
_SVELTE_IGNORE = re.compile(rf'{_OPEN}\s*svelte-ignore\b(?P<tail>[^\n]*)')
_RULE_NAME = re.compile(r'\s+[@\w][\w/@.-]*')


def count_web(source: str) -> dict[str, int]:
    """Offending suppressions per kind in TS/JS/Svelte ``source``, matched on comment syntax."""
    counts: dict[str, int] = {}

    def bump(kind: str) -> None:
        counts[kind] = counts.get(kind, 0) + 1

    for match in _ESLINT.finditer(source):
        if _RULE_NAME.match(match.group('tail')) is None:
            bump('eslint-disable')
    for _ in _TS_IGNORE.finditer(source):
        bump('ts-ignore')
    for match in _SVELTE_IGNORE.finditer(source):
        if _RULE_NAME.match(match.group('tail')) is None:
            bump('svelte-ignore')
    return counts


# --- tree walk ---------------------------------------------------------------------------------


def list_files(root: Path) -> list[str]:
    """Tracked repo-relative paths (``git ls-files``); a failing git is a ``ScanError``."""
    try:
        proc = subprocess.run(
            ['git', '-C', str(root), 'ls-files', '-z'],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        msg = f'git ls-files failed in {root}: {exc}'
        raise bl.ScanError(msg) from exc
    return [p for p in proc.stdout.decode('utf-8').split('\0') if p]


def _in_scope(rel: str) -> str | None:
    if rel.endswith('.py') and rel.startswith(_PY_PREFIXES):
        return 'py'
    if rel.startswith(_WEB_PREFIX) and rel.endswith(_WEB_SUFFIXES):
        return 'web'
    return None


def scan(root: Path, files: list[str] | None = None) -> dict[str, int]:
    """Map ``kind::path`` -> offending count for every in-scope tracked file."""
    if files is None:
        files = list_files(root)
    findings: dict[str, int] = {}
    for rel in sorted(files):
        scope = _in_scope(rel)
        if scope is None:
            continue
        try:
            source = (root / rel).read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError) as exc:
            msg = f'cannot read {rel}: {exc}'
            raise bl.ScanError(msg) from exc
        counts = count_python(source, rel) if scope == 'py' else count_web(source)
        for kind, n in counts.items():
            findings[f'{kind}::{rel}'] = n
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument(
        '--root', type=Path, default=None, help='repository root (default: found from this file)'
    )
    parser.add_argument('--baseline', type=Path, default=None, help='baseline file')
    parser.add_argument('--tighten', action='store_true', help='lower values / drop stale entries')
    bl.add_baseline_args(parser)
    args = parser.parse_args(argv)
    try:
        root = args.root or bl.find_root(_HERE, ('backend/app',))
        baseline_path = args.baseline or (
            root / 'scripts' / 'guards' / 'baselines' / 'suppressions.txt'
        )
        findings = scan(root)
        if args.write_baseline:
            written: int = bl.run_write_baseline(args, baseline_path, findings)
            return written
        baseline = bl.load(baseline_path)
    except (bl.ScanError, ValueError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    if args.tighten:
        changed = bl.tighten(baseline_path, findings, baseline) if baseline_path.exists() else 0
        print(f'tightened {changed} baseline lines in {baseline_path}')
        baseline = bl.load(baseline_path)
    errors = bl.compare(findings, baseline, unit='suppressions without a rule code or reason')
    if errors:
        print('suppressions must carry a rule code (noqa also a reason):', file=sys.stderr)
        for line in errors:
            print(f'  {line}', file=sys.stderr)
        return 1
    print(
        f'suppressions: {sum(findings.values())} offending in {len(findings)} entries, within the baseline'
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
