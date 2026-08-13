#!/usr/bin/env python3
"""Report API routes that no test references — accurately enough to act on.

Why this is a script and not a grep
-----------------------------------
The obvious version — search the test tree for each route's literal path — reported 141
uncovered routes on a tree where two thirds of them were in fact covered. The new suites
build their URLs from a module constant::

    _BASE = "/api/user-settings"
    client.put(f"{_BASE}/download", ...)

so the literal ``/api/user-settings/download`` never appears anywhere, and a literal scan
calls a thoroughly tested route untested. A metric that is wrong in the reassuring
direction is bad; one that is wrong in the alarming direction wastes real work and
discredits the number, which is what happened here.

So this resolves module-level string constants per test file and reconstructs f-strings and
``str.format`` templates whose pieces are all constant, before matching. It still measures
*reference*, not execution — an upper bound on "untested" — and says so in its output
rather than letting the reader forget.

Usage::

    python3 scripts/audit-route-coverage.py                 # summary by router prefix
    python3 scripts/audit-route-coverage.py --list           # every uncovered route
    python3 scripts/audit-route-coverage.py --json           # machine-readable
    python3 scripts/audit-route-coverage.py --prefix admin   # one cluster
"""

from __future__ import annotations

import argparse
import ast
import collections
import json
import os
import pathlib
import re
import sys

_REPO = pathlib.Path(__file__).resolve().parent.parent
_BACKEND = _REPO / 'backend'


def _load_app():
    """Import the FastAPI app with the env it needs, without touching a real deployment."""
    scratch = pathlib.Path(os.environ.get('TMPDIR', '/tmp')) / 'ot-route-audit'
    for sub in ('uploads', 'models', 'temp'):
        (scratch / sub).mkdir(parents=True, exist_ok=True)
    os.environ.update(
        TESTING='true',
        SKIP_CELERY='true',
        UPLOAD_DIR=str(scratch / 'uploads'),
        MODEL_CACHE_DIR=str(scratch / 'models'),
        TEMP_DIR=str(scratch / 'temp'),
    )
    sys.path.insert(0, str(_BACKEND))
    from app.main import app  # noqa: PLC0415  (import must follow the env setup)

    return app


def _string_pool(tree: ast.AST) -> tuple[set[str], dict[str, str]]:
    """Every string a module could contribute to a URL, plus its constant bindings.

    Returns ``(literals, constants)``. ``constants`` holds only ``NAME = "literal"`` at
    module level — enough to resolve the base-constant idiom without pretending to
    interpret the module.
    """
    constants: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value

    literals: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.add(node.value)
        elif isinstance(node, ast.JoinedStr):
            # Rebuild the f-string, substituting resolvable constants and replacing
            # anything else with a wildcard so `f"{_BASE}/x/{uuid}"` still matches
            # `/api/.../x/{param}`.
            parts: list[str] = []
            for piece in node.values:
                if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                    parts.append(piece.value)
                elif isinstance(piece, ast.FormattedValue):
                    inner = piece.value
                    if isinstance(inner, ast.Name) and inner.id in constants:
                        parts.append(constants[inner.id])
                    elif isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                        parts.append(inner.value)
                    else:
                        parts.append('\x00')  # wildcard sentinel
            literals.add(''.join(parts))
    return literals, constants


def _candidate_paths(text: str) -> list[list[str]]:
    """Every path-shaped substring in *text*, split into segments.

    Sentinel-bearing segments (from an unresolved f-string expression) stay as-is and are
    treated as "any one segment" by :func:`_segments_match`.
    """
    out: list[list[str]] = []
    for raw in re.findall(r'/[A-Za-z0-9_\-./{}\x00]*', text):
        path = raw.split('?')[0].rstrip('/') or '/'
        out.append(path.split('/'))
    return out


def _segments_match(route: list[str], candidate: list[str]) -> bool:
    """Structural comparison, segment by segment.

    Deliberately NOT a substring match. A permissive
    ``^.*/api/tasks/[^/]*.*$`` counts ``/api/tasks/{task_id}`` as covered by any test that
    merely names ``/api/tasks/system/fix-file/x`` — which under-reports the gap and is how
    the previous version of this metric produced a number that was too flattering to act on.
    A route path and a URL match only if they have the same shape.
    """
    if len(route) != len(candidate):
        return False
    for r_seg, c_seg in zip(route, candidate, strict=True):
        if r_seg.startswith('{') and r_seg.endswith('}'):
            continue  # route parameter: any single segment
        if '\x00' in c_seg:
            continue  # unresolved f-string expression: any single segment
        if r_seg != c_seg:
            return False
    return True


#: Must-fire / must-stay-clean cases for the matcher. A metric with no self-test is
#: indistinguishable from a broken one: the first version of this script under-reported by
#: two thirds and read as good news, and the second over-matched `/api/tasks/{task_id}`
#: against `/api/tasks/system/fix-file/x`. Both are cases below.
_SELFTEST: list[tuple[str, str, str, bool]] = [
    # (description, route path, test-source text, should_be_covered)
    (
        'plain literal',
        '/api/groups',
        'client.get("/api/groups")',
        True,
    ),
    (
        'base constant resolved out of an f-string -- the whole reason this is not a grep',
        '/api/user-settings/download',
        '_BASE = "/api/user-settings"\nclient.put(f"{_BASE}/download", json={})',
        True,
    ),
    (
        'route parameter matches any single segment',
        '/api/files/{file_uuid}/info',
        'client.get(f"/api/files/{file.uuid}/info")',
        True,
    ),
    (
        'a DEEPER path must NOT cover a shallower route (the over-match regression)',
        '/api/tasks/{task_id}',
        'client.post("/api/tasks/system/fix-file/abc")',
        False,
    ),
    (
        'a SHALLOWER path must not cover a deeper route either',
        '/api/search/models/neural/status',
        'client.get("/api/search/models")',
        False,
    ),
    (
        'a sibling segment is not a match',
        '/api/search/suggestions',
        'client.get("/api/search/count")',
        False,
    ),
    (
        'trailing slash parity -- nginx normalises these, so they are one route',
        '/api/tags/',
        'client.get("/api/tags")',
        True,
    ),
    (
        'unresolved f-string expression stands in for exactly one segment',
        '/api/admin/users/{user_uuid}',
        'client.delete(f"/api/admin/users/{make_uuid()}")',
        True,
    ),
    (
        '...but not for two',
        '/api/admin/users/{user_uuid}/role',
        'client.delete(f"/api/admin/users/{make_uuid()}")',
        False,
    ),
]


def _selftest() -> int:
    """Run the matcher against in-memory cases; exit non-zero on any wrong verdict."""
    failures = 0
    for description, route_path, source, expected in _SELFTEST:
        literals, _ = _string_pool(ast.parse(source))
        candidates = [seg for text in literals for seg in _candidate_paths(text)]
        route_segments = (route_path.rstrip('/') or '/').split('/')
        covered = any(_segments_match(route_segments, cand) for cand in candidates)
        ok = covered == expected
        failures += not ok
        mark = 'PASS' if ok else 'FAIL'
        want = 'covered' if expected else 'NOT covered'
        print(
            f'  [{mark}] {description}  (expected {want}, got '
            f'{"covered" if covered else "NOT covered"})'
        )
    print()
    if failures:
        print(f'{failures} self-test case(s) FAILED — the metric cannot be trusted')
        return 1
    print(f'all {len(_SELFTEST)} self-test cases pass')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--list', action='store_true', help='print every uncovered route')
    ap.add_argument('--json', action='store_true', help='machine-readable output')
    ap.add_argument('--prefix', help='only routes whose path contains this string')
    ap.add_argument(
        '--selftest', action='store_true', help='check the matcher against in-memory cases and exit'
    )
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    app = _load_app()
    routes: list[tuple[str, str]] = []
    for route in app.routes:
        path = getattr(route, 'path', None)
        methods = getattr(route, 'methods', None) or set()
        if not path or not methods:
            continue
        for method in sorted(methods - {'HEAD', 'OPTIONS'}):
            routes.append((method, path))

    pool: set[str] = set()
    test_files = 0
    for file in sorted((_BACKEND / 'tests').rglob('*.py')):
        try:
            tree = ast.parse(file.read_text(errors='ignore'))
        except SyntaxError:
            continue
        test_files += 1
        literals, _ = _string_pool(tree)
        pool.update(literals)

    candidates = [seg for text in pool for seg in _candidate_paths(text)]
    uncovered: list[tuple[str, str]] = []
    for method, path in routes:
        route_segments = (path.rstrip('/') or '/').split('/')
        if not any(_segments_match(route_segments, cand) for cand in candidates):
            uncovered.append((method, path))

    if args.prefix:
        uncovered = [(m, p) for m, p in uncovered if args.prefix in p]

    if args.json:
        print(
            json.dumps(
                {
                    'total_routes': len(routes),
                    'unreferenced': len(uncovered),
                    'routes': [{'method': m, 'path': p} for m, p in uncovered],
                },
                indent=2,
            )
        )
        return 0

    print(f'{len(routes)} routes, {test_files} test files scanned')
    print(f'{len(uncovered)} route(s) with no test reference')
    print("  NOTE: this measures REFERENCE, not execution — an upper bound on 'untested'.")
    print('  A route counted here is definitely not named by any test; a route NOT counted')
    print('  may still only be touched incidentally.\n')

    by_prefix: collections.Counter[str] = collections.Counter(
        p.split('/')[2] if p.count('/') > 2 else p for _, p in uncovered
    )
    for prefix, count in by_prefix.most_common():
        print(f'  {count:3d}  {prefix}')

    if args.list:
        print()
        for method, path in sorted(uncovered, key=lambda x: (x[1], x[0])):
            print(f'  {method:7s} {path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
