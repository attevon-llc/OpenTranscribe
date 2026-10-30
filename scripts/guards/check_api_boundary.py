#!/usr/bin/env python3
"""Layer-boundary ratchet: web-framework and ORM details must stay in ``backend/app/api``.

Three per-file counters, each of which may only go down (baseline key ``counter::path``):

* ``fastapi_import``: ``fastapi`` / ``starlette`` imports outside the HTTP layer.
* ``http_exception_raise``: ``raise HTTPException(...)`` outside the HTTP layer.
* ``orm_call_in_api``: ``.query(``, ``.execute(``, ``select(``, ``text(`` calls inside ``app/api``.

The HTTP layer is ``app/api``, ``app/middleware``, ``app/startup`` (the lifespan's startup jobs,
split out of ``app/main.py``) and ``app/main.py``. Every import and call is
counted, including those inside function bodies (a lazy import hides layering just as well as a
top-level one). A file that does not parse fails the run: skipping it would read as a clean file.

Usage:
    check_api_boundary.py [--root DIR] [--baseline FILE] [--tighten]
    check_api_boundary.py --write-baseline --reason TEXT [--force]
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _baseline

APP_DIR = 'backend/app'
HTTP_LAYER_DIRS = ('backend/app/api/', 'backend/app/middleware/', 'backend/app/startup/')
HTTP_LAYER_FILES = ('backend/app/main.py',)
API_DIR = 'backend/app/api/'
FRAMEWORK_ROOTS = frozenset({'fastapi', 'starlette'})
ORM_METHODS = frozenset({'query', 'execute'})
ORM_FUNCTIONS = frozenset({'select', 'text'})
ORM_MODULES = frozenset({'sa', 'sqlalchemy'})
DEFAULT_BASELINE = Path(__file__).resolve().parent / 'baselines' / 'api_boundary.txt'


def _in_http_layer(rel: str) -> bool:
    return rel.startswith(HTTP_LAYER_DIRS) or rel in HTTP_LAYER_FILES


def _is_http_exception(node: ast.expr) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Name):
        return target.id == 'HTTPException'
    return isinstance(target, ast.Attribute) and target.attr == 'HTTPException'


def _is_orm_call(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in ORM_FUNCTIONS
    if isinstance(func, ast.Attribute):
        if func.attr in ORM_METHODS:
            return True
        return (
            func.attr in ORM_FUNCTIONS
            and isinstance(func.value, ast.Name)
            and func.value.id in ORM_MODULES
        )
    return False


def count_source(source: str, rel: str) -> dict[str, int]:
    """Counters for one file's ``source``; ``rel`` is its repo-relative posix path."""
    tree = ast.parse(source, filename=rel)
    outside = not _in_http_layer(rel)
    in_api = rel.startswith(API_DIR)
    counts = {'fastapi_import': 0, 'http_exception_raise': 0, 'orm_call_in_api': 0}
    for node in ast.walk(tree):
        if outside and isinstance(node, ast.Import):
            counts['fastapi_import'] += sum(
                a.name.split('.')[0] in FRAMEWORK_ROOTS for a in node.names
            )
        elif outside and isinstance(node, ast.ImportFrom):
            if node.level == 0 and (node.module or '').split('.')[0] in FRAMEWORK_ROOTS:
                counts['fastapi_import'] += 1
        elif outside and isinstance(node, ast.Raise):
            if node.exc is not None and _is_http_exception(node.exc):
                counts['http_exception_raise'] += 1
        elif in_api and isinstance(node, ast.Call) and _is_orm_call(node):
            counts['orm_call_in_api'] += 1
    return counts


def scan(root: Path) -> dict[str, int]:
    """Baseline-shaped findings (``counter::path`` -> count) for every file under ``app/``."""
    app = root / APP_DIR
    if not app.is_dir():
        msg = f'{app} is not a directory'
        raise _baseline.ScanError(msg)
    findings: dict[str, int] = {}
    for path in sorted(app.rglob('*.py')):
        rel = path.relative_to(root).as_posix()
        try:
            counts = count_source(path.read_text(encoding='utf-8'), rel)
        except (SyntaxError, ValueError, UnicodeDecodeError) as exc:
            msg = f'cannot parse {rel}: {exc}'
            raise _baseline.ScanError(msg) from exc
        for counter, value in counts.items():
            if value:
                findings[f'{counter}::{rel}'] = value
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--root', type=Path, default=None, help='repository root')
    parser.add_argument('--baseline', type=Path, default=DEFAULT_BASELINE)
    parser.add_argument('--tighten', action='store_true', help='lower values, drop stale entries')
    _baseline.add_baseline_args(parser)
    args = parser.parse_args(argv)

    try:
        root = args.root or _baseline.find_root(Path(__file__).resolve(), (APP_DIR,))
        findings = scan(root.resolve())
        if args.write_baseline:
            written: int = _baseline.run_write_baseline(args, args.baseline, findings)
            return written
        entries = _baseline.load(args.baseline)
    except (_baseline.ScanError, ValueError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2

    if args.tighten:
        if not args.baseline.exists():
            print(f'error: {args.baseline} does not exist', file=sys.stderr)
            return 2
        changed = _baseline.tighten(args.baseline, findings, entries)
        entries = _baseline.load(args.baseline)
        print(f'tightened {changed} baseline entries')

    errors = _baseline.compare(findings, entries, unit='occurrences')
    for line in errors:
        print(line, file=sys.stderr)
    if errors:
        print(f'api boundary: {len(errors)} problem(s)', file=sys.stderr)
        return 1
    print(f'api boundary: ok ({len(findings)} baselined files)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
