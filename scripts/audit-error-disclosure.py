#!/usr/bin/env python3
"""Keep stored failure text off the wire, and retry policy off stored prose (issues #786, #959).

A pipeline failure stores a fixed user-facing sentence in ``media_file.last_error_message`` /
``task.error_message`` and a retry code in ``media_file.error_category``. Rows written before
#959, and the download path, can still hold raw exception text (paths, command lines,
tracebacks), so every read edge that puts either column on the wire must go through
``ErrorCategorizationService``. #786 fixed five such edges by hand; #959 found two more it had
missed. This script makes "every edge is sanitized" an enforced property, not a count of
hand-fixed sites.

Detectors
    raw-error-read
        A wire-layer function (``app/api/``, ``formatting_service.py``, the transcription
        notification module) that reads ``.last_error_message``, ``getattr(x,
        "last_error_message")`` or a task's ``.error_message`` and never calls a sanitizer
        (``user_message_for``, ``error_fields_for``, ``get_error_info``). Column references on a
        model class (``MediaFile.last_error_message`` in a query) are not reads of a value and
        are ignored; so are writes.
    prose-retry-rederivation
        ``categorize_error(...)`` fed ``.last_error_message`` anywhere under ``app/``. Retry
        policy reads the category the failure site stored (``stored_category``); re-deriving
        it from stored prose is what made rewording a message change retry behaviour.

Usage::

    scripts/audit-error-disclosure.py backend/app
    scripts/audit-error-disclosure.py --selftest

Exits 1 on any finding. There is deliberately no allowlist: a read edge is either sanitized
or it is a disclosure.
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass
from pathlib import Path

CATEGORIES = ('raw-error-read', 'prose-retry-rederivation')

STORED_COLUMN = 'last_error_message'
TASK_COLUMN = 'error_message'
SANITIZERS = frozenset({'user_message_for', 'error_fields_for', 'get_error_info'})

# Paths (relative to the scanned app root) whose functions build client-facing payloads.
WIRE_PREFIXES = (
    'api/',
    'services/formatting_service.py',
    'tasks/transcription/notifications.py',
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    scope: str
    category: str
    detail: str


def _is_wire(relpath: str) -> bool:
    return any(relpath == p or relpath.startswith(p) for p in WIRE_PREFIXES)


def _receiver_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_model_column(node: ast.Attribute) -> bool:
    """``MediaFile.last_error_message`` / ``TaskModel.error_message`` — a column, not a value."""
    name = _receiver_name(node.value)
    return bool(name) and name[0].isupper()


def _raw_reads(func: ast.AST) -> list[tuple[int, str]]:
    reads: list[tuple[int, str]] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            if _is_model_column(node):
                continue
            if node.attr == STORED_COLUMN:
                reads.append((node.lineno, f'reads .{STORED_COLUMN}'))
            elif node.attr == TASK_COLUMN and 'task' in (_receiver_name(node.value) or '').lower():
                reads.append((node.lineno, f'reads {_receiver_name(node.value)}.{TASK_COLUMN}'))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == 'getattr'
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == STORED_COLUMN
        ):
            reads.append((node.lineno, f'getattr(..., "{STORED_COLUMN}")'))
    return reads


def _calls(func: ast.AST, names: frozenset[str]) -> bool:
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            called = _receiver_name(node.func)
            if called in names:
                return True
    return False


def _mentions_stored_column(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr == STORED_COLUMN:
            return True
        if isinstance(sub, ast.Constant) and sub.value == STORED_COLUMN:
            return True
    return False


def _outermost_functions(tree: ast.AST) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Top-level functions and methods; a nested helper is judged with its enclosing function."""
    found: list[ast.FunctionDef | ast.AsyncFunctionDef] = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append(child)
            elif isinstance(child, ast.ClassDef):
                visit(child)

    visit(tree)
    return found


def scan_source(source: str, relpath: str) -> list[Finding]:
    tree = ast.parse(source)
    findings: list[Finding] = []

    if _is_wire(relpath):
        for func in _outermost_functions(tree):
            reads = _raw_reads(func)
            if reads and not _calls(func, SANITIZERS):
                for line, detail in reads:
                    findings.append(
                        Finding(
                            relpath,
                            line,
                            func.name,
                            'raw-error-read',
                            f'{detail} with no sanitizer call in the function',
                        )
                    )

    scope_by_line: dict[int, str] = {}
    for func in _outermost_functions(tree):
        for node in ast.walk(func):
            if hasattr(node, 'lineno'):
                scope_by_line.setdefault(node.lineno, func.name)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and _receiver_name(node.func) == 'categorize_error'
            and any(_mentions_stored_column(arg) for arg in node.args)
        ):
            findings.append(
                Finding(
                    relpath,
                    node.lineno,
                    scope_by_line.get(node.lineno, '<module>'),
                    'prose-retry-rederivation',
                    f'retry category re-derived from stored .{STORED_COLUMN}; '
                    'use stored_category(media_file.error_category)',
                )
            )
    return findings


def scan_file(path: Path, root: Path) -> list[Finding]:
    relpath = path.relative_to(root).as_posix()
    return scan_source(path.read_text(encoding='utf-8'), relpath)


# ------------------------------------------------------------------------------ self-test

SELFTEST_CASES: tuple[tuple[str, str, str], ...] = (
    (
        'raw-error-read',
        'api/endpoints/x.py',
        'def f(db_file):\n    return {"error": db_file.last_error_message}\n',
    ),
    (
        'raw-error-read',
        'api/endpoints/x.py',
        'def f(task):\n    return {"error_message": task.error_message}\n',
    ),
    (
        'raw-error-read',
        'services/formatting_service.py',
        'class S:\n    def f(self, m):\n        return getattr(m, "last_error_message", None)\n',
    ),
    (
        'prose-retry-rederivation',
        'services/task_recovery_service.py',
        'def f(m):\n    return categorize_error(m.last_error_message or "")\n',
    ),
)

SELFTEST_CLEAN: tuple[tuple[str, str], ...] = (
    (
        'api/endpoints/x.py',
        'def f(task):\n'
        '    return {"error_message": ErrorCategorizationService.user_message_for(\n'
        '        task.error_message)}\n',
    ),
    (
        'api/endpoints/x.py',
        'def f(db):\n    return db.query(MediaFile.last_error_message, TaskModel.error_message)\n',
    ),
    ('api/endpoints/x.py', 'def f(m):\n    m.last_error_message = "fixed"\n'),
    # A watch-source row's error_message is another table, not a task's.
    ('api/endpoints/x.py', 'def f(r):\n    return {"error_message": r.error_message}\n'),
    # Outside the wire layer, reading the column server-side is fine.
    ('services/task_detection_service.py', 'def f(m):\n    return m.last_error_message\n'),
    (
        'tasks/youtube_processing.py',
        'def f(e):\n    return categorize_error(str(e))\n',
    ),
)


def run_selftest(verbose: bool = True) -> list[str]:
    """Return failure descriptions — empty means every detector is alive."""
    failures: list[str] = []
    for category, relpath, source in SELFTEST_CASES:
        got = {f.category for f in scan_source(source, relpath)}
        ok = category in got
        if not ok:
            failures.append(f'{category} did not fire on {relpath} (got {sorted(got)})')
        if verbose:
            print(f'  {"ok  " if ok else "FAIL"} fires {category} ({relpath})')
    for i, (relpath, source) in enumerate(SELFTEST_CLEAN, start=1):
        found = scan_source(source, relpath)
        if found:
            failures.append(f'clean case {i} produced {[f.category for f in found]}')
        if verbose:
            print(f'  {"FAIL" if found else "ok  "} clean case {i} produces no finding')
    return failures


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument('root', type=Path, nargs='?', help='app tree to scan (backend/app)')
    ap.add_argument('--selftest', action='store_true', help='audit the auditor')
    args = ap.parse_args()

    if args.selftest:
        failures = run_selftest()
        if failures:
            print(f'\n{len(failures)} self-test failure(s) — a detector is broken:')
            for line in failures:
                print(f'  {line}')
            return 1
        print(f'\nall {len(SELFTEST_CASES) + len(SELFTEST_CLEAN)} self-test cases pass')
        return 0

    if args.root is None:
        ap.error('root is required unless --selftest is given')
    if not args.root.is_dir():
        print(f'error: {args.root} is not a directory', file=sys.stderr)
        return 2

    # A broken detector reports zero findings, so never let the tree scan speak without it.
    selftest_failures = run_selftest(verbose=False)
    findings: list[Finding] = []
    for path in sorted(args.root.rglob('*.py')):
        findings.extend(scan_file(path, args.root))

    for f in findings:
        print(f'{args.root}/{f.path}:{f.line} [{f.category}] {f.scope} — {f.detail}')
    if selftest_failures:
        print('SELF-TEST BROKEN — the scan above is not trustworthy:')
        for line in selftest_failures:
            print(f'  {line}')
    if findings or selftest_failures:
        print(
            f'\n{len(findings)} finding(s). Route the read through '
            'ErrorCategorizationService.user_message_for / error_fields_for, and read retry '
            'policy from stored_category(media_file.error_category).'
        )
        return 1
    print('no error-disclosure findings')
    return 0


if __name__ == '__main__':
    sys.exit(main())
