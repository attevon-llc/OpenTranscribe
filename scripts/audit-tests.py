#!/usr/bin/env python3
"""Find tests that pass whether the code works or not.

A test that cannot fail is worse than no test: it buys false confidence and hides the
defect it was written to catch. This scans the test tree by AST for six such patterns.

Detectors
    permissive-status
        ``assert response.status_code in (200, 403)`` accepts success AND authorization
        failure. Concentrated in the security suites, where it matters most.
    conditional-only
        Every assertion in the test sits inside an ``if`` with no ``else``, so the test
        passes silently whenever the condition is False.
    no-assertion
        No ``assert``, no ``pytest.raises``/``pytest.fail``, no ``expect()``, no
        ``assert_*`` helper.
    failure-masking
        ``except ...: pytest.skip(...)`` reports a *failure* as a *skip*. A rename or a
        genuine regression then reads as "skipped" forever. Helper functions are scanned
        too — that is where import guards hide.
    mock-heavy
        So many ``patch`` references that the test asserts its own mock wiring rather
        than behaviour.
    fixture-named-test
        A ``@pytest.fixture`` named ``test_*``. It never runs as a test but reads as one,
        and it corrupts any count of tests-without-assertions.

Usage::

    scripts/audit-tests.py backend/tests
    scripts/audit-tests.py backend/tests --json
    scripts/audit-tests.py backend/tests --category permissive-status

Exits 1 when any finding is not in the allowlist, so this can gate a commit. The allowlist
lives at ``backend/tests/audit-allowlist.txt``: one ``<file>::<test>::<category>  # reason``
per line. The category is REQUIRED — an entry keyed only by test would exempt that test from
every detector at once, which is how a `failure-masking` exemption silently granted a
`no-assertion` one too. Adding an entry is a deliberate, reviewable act; widening an
assertion to restore green is not.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

#: A status assertion listing this many alternatives is not asserting anything useful.
_MAX_STATUS_ALTERNATIVES = 1

#: `patch` references above this in one test mean the test is mostly scaffolding.
_MAX_PATCH_REFS = 6

_ALLOWLIST_NAME = 'audit-allowlist.txt'

CATEGORIES = (
    'permissive-status',
    'conditional-only',
    'no-assertion',
    'failure-masking',
    'mock-heavy',
    'fixture-named-test',
)


@dataclass(frozen=True)
class Finding:
    """One suspect test."""

    category: str
    path: str
    line: int
    test: str
    detail: str

    @property
    def key(self) -> str:
        """Allowlist key. Includes the category so one exemption cannot cover all six."""
        return f'{self.path}::{self.test}::{self.category}'


def _is_fixture(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when decorated with ``@pytest.fixture`` (bare or called)."""
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and target.attr == 'fixture':
            return True
        if isinstance(target, ast.Name) and target.id == 'fixture':
            return True
    return False


def _is_test(node: ast.AST) -> bool:
    """A collected test: named ``test_*`` and NOT a fixture that borrowed the prefix."""
    if not (
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith('test_')
    ):
        return False
    return not _is_fixture(node)


def _asserts(fn: ast.AST) -> list[ast.Assert]:
    return [n for n in ast.walk(fn) if isinstance(n, ast.Assert)]


#: Assertion idioms other than a bare ``assert``. ``expect`` is Playwright's web-first
#: assertion and is the ONLY assertion in most E2E tests — omit it and a third of the
#: E2E suite reads as assertion-free, which is a detector bug, not a finding.
#: ``does_not_raise`` is ``tests/helpers.py``'s context manager, which calls ``pytest.fail``
#: with a reason when the block raises — so a test using it does assert something, and the
#: reason string is mandatory there precisely so it cannot become a silent pass.
_ASSERTING_CALLS = frozenset({'expect', 'fail', 'raises', 'does_not_raise'})


def _has_raises_or_helper(fn: ast.AST) -> bool:
    """True when the test asserts via ``pytest.raises``, ``expect()``, or ``assert_*``."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute) and node.attr in _ASSERTING_CALLS:
            return True
        if isinstance(node, ast.Call):
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else getattr(node.func, 'id', '')
            )
            if name.startswith('assert') or name in _ASSERTING_CALLS:
                return True
    return False


def _status_alternatives(fn: ast.AST) -> list[tuple[int, int]]:
    """(lineno, n_alternatives) for each ``status_code in (...)`` comparison."""
    out: list[tuple[int, int]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Compare) or not node.ops:
            continue
        if not isinstance(node.ops[0], ast.In):
            continue
        left = node.left
        if not (isinstance(left, ast.Attribute) and 'status' in left.attr):
            continue
        target = node.comparators[0]
        if isinstance(target, ast.Tuple | ast.List | ast.Set):
            out.append((node.lineno, len(target.elts)))
    return out


def _conditional_only(fn: ast.AST) -> bool:
    """True when every assertion is nested in an ``if`` that has no ``else``."""
    asserts = _asserts(fn)
    if not asserts:
        return False
    unguarded = {id(a) for a in asserts}
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and not node.orelse:
            for inner in node.body:
                for descendant in ast.walk(inner):
                    unguarded.discard(id(descendant))
    return not unguarded


def _masks_failure(fn: ast.AST) -> str | None:
    """Return the caught exception when a handler skips instead of failing."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.ExceptHandler):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            if isinstance(func, ast.Attribute) and func.attr == 'skip':
                caught = ast.unparse(node.type) if node.type else 'bare except'
                return caught
    return None


def _patch_refs(fn: ast.AST) -> int:
    count = 0
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Name)
            and node.id == 'patch'
            or isinstance(node, ast.Attribute)
            and node.attr in ('patch', 'object')
        ):
            count += 1
    return count


def scan_file(path: Path, root: Path) -> list[Finding]:
    """Scan one test module. Helper functions are scanned for failure-masking too."""
    try:
        tree = ast.parse(path.read_text())
    except (SyntaxError, UnicodeDecodeError):
        return []
    rel = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
    found: list[Finding] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        is_test = _is_test(node)
        if node.name.startswith('test_') and _is_fixture(node):
            found.append(
                Finding(
                    'fixture-named-test',
                    rel,
                    node.lineno,
                    node.name,
                    'fixture named test_* — reads as a test, never runs as one',
                )
            )

        # Failure masking matters in helpers too — that is where the import guards live.
        caught = _masks_failure(node)
        if caught:
            found.append(
                Finding(
                    'failure-masking',
                    rel,
                    node.lineno,
                    node.name,
                    f'except {caught} -> pytest.skip',
                )
            )
        if not is_test:
            continue

        for lineno, count in _status_alternatives(node):
            if count > _MAX_STATUS_ALTERNATIVES:
                found.append(
                    Finding(
                        'permissive-status', rel, lineno, node.name, f'accepts {count} statuses'
                    )
                )

        if _conditional_only(node):
            found.append(
                Finding(
                    'conditional-only',
                    rel,
                    node.lineno,
                    node.name,
                    f'{len(_asserts(node))} assert(s), all inside if-without-else',
                )
            )

        if not _asserts(node) and not _has_raises_or_helper(node):
            found.append(
                Finding('no-assertion', rel, node.lineno, node.name, 'no assert/raises/assert_*')
            )

        refs = _patch_refs(node)
        if refs >= _MAX_PATCH_REFS:
            found.append(
                Finding('mock-heavy', rel, node.lineno, node.name, f'{refs} patch references')
            )

    return found


def load_allowlist(root: Path) -> dict[str, str]:
    """Map ``<file>::<test>::<category>`` to its stated reason."""
    path = root / _ALLOWLIST_NAME
    if not path.exists():
        return {}
    allowed: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        key, _, reason = line.partition('#')
        allowed[key.strip()] = reason.strip() or 'no reason given'
    return allowed


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument('root', type=Path, help='test tree to scan (e.g. backend/tests)')
    ap.add_argument('--category', choices=CATEGORIES, help='limit to one detector')
    ap.add_argument('--json', action='store_true', help='machine-readable output')
    ap.add_argument(
        '--include-e2e', action='store_true', help='also scan tests/e2e (excluded by default)'
    )
    ap.add_argument('--list', action='store_true', help='print every finding, not just counts')
    args = ap.parse_args()

    if not args.root.is_dir():
        print(f'error: {args.root} is not a directory', file=sys.stderr)
        return 2

    findings: list[Finding] = []
    for path in sorted(args.root.rglob('*.py')):
        if not args.include_e2e and 'e2e' in path.parts:
            continue
        findings.extend(scan_file(path, args.root))

    if args.category:
        findings = [f for f in findings if f.category == args.category]

    allowed = load_allowlist(args.root)
    unallowed = [f for f in findings if f.key not in allowed]

    if args.json:
        print(
            json.dumps(
                {
                    'total': len(findings),
                    'allowlisted': len(findings) - len(unallowed),
                    'unallowlisted': len(unallowed),
                    'by_category': dict(Counter(f.category for f in unallowed)),
                    'findings': [f.__dict__ for f in unallowed],
                },
                indent=2,
            )
        )
        return 1 if unallowed else 0

    counts = Counter(f.category for f in findings)
    print(
        f'\n\033[1m{args.root}\033[0m — {len(findings)} findings, {len(unallowed)} not allowlisted\n'
    )
    for category in CATEGORIES:
        hits = [f for f in unallowed if f.category == category]
        total = counts.get(category, 0)
        colour = '\033[31m' if hits else '\033[32m'
        print(f'  {colour}{category:20s}\033[0m {len(hits):4d} open  ({total} total)')
        if args.list or len(hits) <= 5:
            for f in hits[: (None if args.list else 5)]:
                print(f'      {f.path}:{f.line} {f.test} — {f.detail}')

    if unallowed:
        print(f'\n\033[31m{len(unallowed)} findings need a fix or an allowlist entry\033[0m')
        print(
            f'  allowlist: {args.root / _ALLOWLIST_NAME}  (one "<file>::<test>  # reason" per line)\n'
        )
        return 1
    print('\n\033[32mno un-allowlisted findings\033[0m\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
