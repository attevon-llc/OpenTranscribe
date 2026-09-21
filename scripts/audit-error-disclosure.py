#!/usr/bin/env python3
"""Find raw exception text flowing into a persisted/user-facing error field.

GH #959 item 5. Issue #786 established a no-raw-echo contract for
``ErrorCategorizationService`` (every ``user_message``/suggestion it returns is a fixed
sentence), and #959 closed the two write-time residuals — ``preprocess.py``'s
``_mark_pipeline_error`` and the two ``task_recovery_service.py`` retry-classification
sites that re-derived a category from stored prose. Without a gate, a SEVENTH un-sanitized
call site can appear at any time: nothing stopped the five #959 named — this script is
that stop.

What it flags
    An assignment (attribute or keyword argument) to one of the WATCHED_FIELDS whose
    right-hand side is:

    - an f-string (``JoinedStr``) containing a ``{e}``/``{exc}``/``{error}``/
      ``{exception}``-shaped interpolation (any of EXCEPTION_VAR_NAMES), or
    - a bare ``str(e)`` call (or a bare reference to such a variable) with no
      ``ErrorCategorizationService`` / ``sanitize_for_storage`` / ``get_error_info`` /
      ``categorize_error`` call anywhere in the same expression.

This is a heuristic, not a type checker — it looks at the shape of the expression, not
whether a helper further up the call chain already sanitized the value. That is why it is
allowlist-gated rather than a hard ban: a legitimate false positive (e.g. a curated,
non-raw exception message that happens to be built from a variable named ``e``) is
expected occasionally, and should be resolved by fixing the shape (name the variable
`message`/`sanitized`) rather than reaching for the allowlist as a default.

Usage
    python3 scripts/audit-error-disclosure.py [path ...]   # defaults to backend/app
    python3 scripts/audit-error-disclosure.py --list        # print all findings, ignore allowlist

Allowlist
    scripts/error-disclosure-allowlist.txt, one ``<file>::<lineno>::<reason>`` per line.
    A written reason is mandatory. An allowlist entry for a line the scanner no longer
    flags fails the run (count-aware: the file may only shrink to match reality).
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

WATCHED_FIELDS = {
    'last_error_message',
    'error_message',
    'user_message',
}

EXCEPTION_VAR_NAMES = {
    'e',
    'exc',
    'err',
    'error',
    'exception',
    'status_err',
    'update_err',
    'cleanup_err',
}

SANITIZER_MARKERS = (
    'sanitize_for_storage',
    'get_error_info',
    'categorize_error',
    'build_error_response_fields',
    'ErrorCategorizationService',
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ALLOWLIST_PATH = Path(__file__).resolve().parent / 'error-disclosure-allowlist.txt'
_DEFAULT_SCAN_ROOT = _REPO_ROOT / 'backend' / 'app'


@dataclass(frozen=True)
class Finding:
    file: str
    lineno: int
    field: str
    snippet: str

    @property
    def key(self) -> str:
        return f'{self.file}::{self.lineno}'


def _expr_source(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - defensive, ast.unparse is stable on py3.9+
        return '<unparsable>'


def _mentions_sanitizer(node: ast.AST) -> bool:
    src = _expr_source(node)
    return any(marker in src for marker in SANITIZER_MARKERS)


def _is_exception_name(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id in EXCEPTION_VAR_NAMES


def _joinedstr_leaks_exception(node: ast.JoinedStr) -> bool:
    for value in node.values:
        if not isinstance(value, ast.FormattedValue):
            continue
        inner = value.value
        if _is_exception_name(inner):
            return True
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name)
            and inner.func.id == 'str'
            and inner.args
            and _is_exception_name(inner.args[0])
        ):
            return True
    return False


def _value_is_raw(node: ast.AST) -> bool:
    """True if ``node`` looks like raw exception text with no sanitizer in sight."""
    if _mentions_sanitizer(node):
        return False
    if isinstance(node, ast.JoinedStr):
        return _joinedstr_leaks_exception(node)
    if _is_exception_name(node):
        return True
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == 'str'
        and bool(node.args)
        and _is_exception_name(node.args[0])
    )


def _scan_file(path: Path) -> list[Finding]:
    try:
        source = path.read_text(encoding='utf-8')
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return []

    try:
        rel = str(path.relative_to(_REPO_ROOT))
    except ValueError:
        # A caller (e.g. a test fixture under a tmp dir) scanning a file outside the repo —
        # report the path as given rather than crashing.
        rel = str(path)
    findings: list[Finding] = []

    for node in ast.walk(tree):
        # `media_file.last_error_message = <expr>` / `task.error_message = <expr>`
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr in WATCHED_FIELDS
                    and _value_is_raw(node.value)
                ):
                    findings.append(
                        Finding(rel, node.lineno, target.attr, _expr_source(node.value))
                    )
        # `update_task_status(..., error_message=<expr>)` and similar keyword calls
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in WATCHED_FIELDS and _value_is_raw(kw.value):
                    findings.append(Finding(rel, node.lineno, kw.arg, _expr_source(kw.value)))

    return findings


def scan(paths: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for root in paths:
        if root.is_file():
            findings.extend(_scan_file(root))
            continue
        for py_file in sorted(root.rglob('*.py')):
            if '/tests/' in str(py_file) or py_file.name.startswith('test_'):
                continue
            findings.extend(_scan_file(py_file))
    return findings


def load_allowlist(path: Path = _ALLOWLIST_PATH) -> dict[str, str]:
    if not path.exists():
        return {}
    entries: dict[str, str] = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split('::', 2)
        if len(parts) != 3:
            continue
        file_, lineno, reason = parts
        if not reason.strip():
            continue
        entries[f'{file_}::{lineno}'] = reason.strip()
    return entries


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument('paths', nargs='*', default=[str(_DEFAULT_SCAN_ROOT)])
    ap.add_argument(
        '--list', action='store_true', help='print all findings, ignoring the allowlist'
    )
    args = ap.parse_args()

    findings = scan([Path(p) for p in args.paths])
    allowlist = load_allowlist()

    if args.list:
        for f in findings:
            print(f'{f.key}  [{f.field}]  {f.snippet}')
        print(f'\n{len(findings)} finding(s)')
        return 0

    unallowed = [f for f in findings if f.key not in allowlist]
    stale = sorted(set(allowlist) - {f.key for f in findings})

    if unallowed:
        print('\033[31mUn-allowlisted raw-error-disclosure findings:\033[0m')
        for f in unallowed:
            print(f'  {f.key}  [{f.field}]  {f.snippet}')
        print(
            f'\nFix the finding (sanitize via ErrorCategorizationService before persisting), '
            f'or add a reasoned entry to {_ALLOWLIST_PATH.relative_to(_REPO_ROOT)} '
            f'in the form <file>::<lineno>::<reason>.'
        )

    if stale:
        print('\033[31mStale allowlist entries (no longer a finding — delete the line):\033[0m')
        for key in stale:
            print(f'  {key}')

    if unallowed or stale:
        return 1

    print(f'\033[32mno un-allowlisted findings ({len(findings)} allowlisted)\033[0m')
    return 0


if __name__ == '__main__':
    sys.exit(main())
