#!/usr/bin/env python3
"""Find tests that pass whether the code works or not.

A test that cannot fail is worse than no test: it buys false confidence and hides the
defect it was written to catch. This scans the test tree by AST for the patterns below.

Detectors
    permissive-status
        ``assert response.status_code in (200, 403)`` accepts success AND authorization
        failure. Concentrated in the security suites, where it matters most.
    negated-status
        ``assert response.status_code != 403``. Reads like an authorization assertion and
        is not one: a 401, a 404 and — the reason this matters — a **500** all pass. Fifteen
        of these were in this tree and none was visible to ``permissive-status``, which only
        looks at ``in (...)``.
    status-guarded-assert
        The real assertion sits inside ``if response.status_code == 200:`` with no ``else``.
        ``conditional-only`` cannot see it, because the test ALSO has an unguarded (and
        usually negated) status assert — so the shape survived the fix applied to its
        siblings. A wrong status silently skips the only check that matters.
    conditional-only
        Every assertion in the test sits inside an ``if`` with no ``else``, so the test
        passes silently whenever the condition is False.
    conditional-skip
        Every assertion sits inside an ``if`` whose ``else`` only calls ``pytest.skip``.
        Not vacuous like conditional-only — it *reports* a skip — but a guard that can
        never be true makes it a permanent skip that reads as a passing suite.
    loop-only
        Every assertion sits inside a ``for`` over a **dynamic** iterable. An empty
        iterable executes the body zero times, so the test is a vacuous pass:
        ``for span in detector.detect_pii(...): assert 0 <= span.start`` is green when
        nothing at all was detected. A loop over a literal collection is exempt — its
        length is fixed at parse time.
    no-assertion
        No ``assert``, no ``pytest.raises``/``pytest.fail``, no ``expect()``, no
        ``assert_*`` helper.
    unfalsifiable
        Both sides of the assertion are compile-time constants — ``assert True``,
        ``assert 1 == 1``. Cannot fail, ever.
    weak-only
        The test's ONLY assertions are bare-truthy (``assert still_on_login``) or
        ``is not None``. ``still_on_login = "/login" in page.url or
        page.locator("#email").is_visible()`` is an ``or`` chain over two things that are
        both true on a page that never navigated — including one that is true on a *crashed*
        page — so the assertion is a formality. Anything stronger in the test clears it.
    mock-only
        Every assertion is mock bookkeeping (``assert_called_once_with``, ``.called``,
        ``.call_args``) with nothing asserted about a return value or real state. Proves the
        test called the mock, not that the code works.
    failure-masking
        ``except ...: pytest.skip(...)`` reports a *failure* as a *skip*. A rename or a
        genuine regression then reads as "skipped" forever. Helper functions are scanned
        too — that is where import guards hide.
    error-swallowed
        ``except ...: pass`` / bare ``return`` / log-and-continue inside a **test**. The
        untested twin of failure-masking: it does not even report a skip, so the assertions
        after the ``try`` simply never run and the test is green.
    mock-heavy
        So many ``patch``/``monkeypatch`` calls in one test that the test asserts its own
        mock wiring rather than behaviour.
    fixture-named-test
        A ``@pytest.fixture`` named ``test_*``. It never runs as a test but reads as one,
        and it corrupts any count of tests-without-assertions.

Usage::

    scripts/audit-tests.py backend/tests
    scripts/audit-tests.py backend/tests --json
    scripts/audit-tests.py backend/tests --category permissive-status
    scripts/audit-tests.py --selftest        # audit the auditor (no tree needed)

``tests/e2e`` is scanned by DEFAULT (``--no-e2e`` opts out). It used to be excluded, which
hid 21 findings in the suite that drives a real browser — including the conditional-only
assertions around the media-download hang. A detector that does not look at the riskiest
tests in the tree is not a gate.

Exits 1 when any finding is not in the allowlist, so this can gate a commit. The allowlist
lives at ``backend/tests/audit-allowlist.txt``: one ``<file>::<test>::<category>  # reason``
per line. The category is REQUIRED — an entry keyed only by test would exempt that test from
every detector at once, which is how a `failure-masking` exemption silently granted a
`no-assertion` one too. Adding an entry is a deliberate, reviewable act; widening an
assertion to restore green is not.

**Run ``--selftest`` after touching any detector.** Its frontend sibling's self-test caught
two detectors matching *nothing*, which reports 0 findings and is indistinguishable from a
clean suite — the exact failure mode this script exists to prevent. Every detector needs a
case that must fire and the tree of clean cases must stay silent.
``backend/tests/unit/test_audit_tests_selftest.py`` runs the same cases under pytest, so the
suite fails if a detector goes blind even when nobody remembers the flag.
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

#: `patch`/`monkeypatch` CALLS above this in one test mean the test is mostly scaffolding.
#: Counted per call, not per AST node: the old implementation matched both `Name('patch')`
#: and `Attribute(attr='object')`, so every `patch.object(...)` scored 2 and the effective
#: threshold was 3 — while `monkeypatch.setattr` scored 0 and was invisible entirely.
_MAX_PATCH_REFS = 6

_ALLOWLIST_NAME = 'audit-allowlist.txt'

CATEGORIES = (
    'permissive-status',
    'negated-status',
    'status-guarded-assert',
    'conditional-only',
    'conditional-skip',
    'loop-only',
    'no-assertion',
    'unfalsifiable',
    'weak-only',
    'mock-only',
    'failure-masking',
    'error-swallowed',
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

#: ``unittest.mock`` assertion methods. These live ONLY on Mock objects, so matching the
#: method name is exact — no need to guess whether the receiver is a mock.
_MOCK_ASSERT_METHODS = frozenset(
    {
        'assert_called',
        'assert_called_once',
        'assert_called_with',
        'assert_called_once_with',
        'assert_not_called',
        'assert_any_call',
        'assert_has_calls',
        'assert_awaited',
        'assert_awaited_once',
        'assert_awaited_with',
        'assert_awaited_once_with',
        'assert_not_awaited',
        'assert_any_await',
        'assert_has_awaits',
    }
)

#: Mock introspection attributes. An assertion whose expression reads one of these is
#: asserting about the call log, not about anything the production code produced.
_MOCK_INTROSPECTION = frozenset(
    {
        'called',
        'call_count',
        'call_args',
        'call_args_list',
        'mock_calls',
        'await_count',
        'await_args',
        'await_args_list',
    }
)

#: Calls that make an ``except`` handler a swallow rather than a report.
_SWALLOW_CALLS = frozenset({'debug', 'info', 'warning', 'warn', 'error', 'exception', 'print'})

#: ``monkeypatch`` mutators, which the old ``patch``-only count could never see.
_MONKEYPATCH_METHODS = frozenset(
    {
        'setattr',
        'setitem',
        'delattr',
        'delitem',
        'setenv',
        'delenv',
        'syspath_prepend',
        'chdir',
    }
)


def _dotted(node: ast.AST) -> list[str]:
    """``mock.patch.object`` -> ``['mock', 'patch', 'object']``. Non-name roots -> ``[]``."""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return []
    parts.append(cur.id)
    return list(reversed(parts))


def _asserting_calls(fn: ast.AST) -> list[ast.Call]:
    """Every call that acts as an assertion (``assert_*``, ``expect``, ``pytest.raises``)."""
    out: list[ast.Call] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        name = (
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, 'id', '')
        )
        if name.startswith('assert') or name in _ASSERTING_CALLS:
            out.append(node)
    return out


def _has_raises_or_helper(fn: ast.AST) -> bool:
    """True when the test asserts via ``pytest.raises``, ``expect()``, or ``assert_*``."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute) and node.attr in _ASSERTING_CALLS:
            return True
    return bool(_asserting_calls(fn))


def _is_status_attr(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and 'status' in node.attr


def _status_alternatives(fn: ast.AST) -> list[tuple[int, int]]:
    """(lineno, n_alternatives) for each ``status_code in (...)`` comparison."""
    out: list[tuple[int, int]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Compare) or not node.ops:
            continue
        if not isinstance(node.ops[0], ast.In):
            continue
        if not _is_status_attr(node.left):
            continue
        target = node.comparators[0]
        if isinstance(target, ast.Tuple | ast.List | ast.Set):
            out.append((node.lineno, len(target.elts)))
    return out


def _negated_status(fn: ast.AST) -> list[tuple[int, str]]:
    """(lineno, detail) for every ``assert response.status_code != X`` / ``not in (...)``.

    Restricted to expressions inside an ``assert``: ``if response.status_code != 200:`` is
    control flow, not a claim about the response.
    """
    out: list[tuple[int, str]] = []
    for assertion in _asserts(fn):
        for node in ast.walk(assertion):
            if not isinstance(node, ast.Compare) or not _is_status_attr(node.left):
                continue
            for op, comparator in zip(node.ops, node.comparators, strict=True):
                if isinstance(op, ast.NotEq):
                    out.append((node.lineno, f'!= {ast.unparse(comparator)}'))
                elif isinstance(op, ast.NotIn):
                    out.append((node.lineno, f'not in {ast.unparse(comparator)}'))
    return out


def _status_guarded_asserts(fn: ast.AST) -> list[tuple[int, str, int]]:
    """(lineno, guard, n_asserts) for each ``if <status...>:`` without ``else`` holding asserts."""
    out: list[tuple[int, str, int]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.If) or node.orelse:
            continue
        if not any(_is_status_attr(inner) for inner in ast.walk(node.test)):
            continue
        inner_asserts = [
            d for stmt in node.body for d in ast.walk(stmt) if isinstance(d, ast.Assert)
        ]
        if inner_asserts:
            out.append((node.lineno, ast.unparse(node.test), len(inner_asserts)))
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


def _conditional_skip(fn: ast.AST) -> str | None:
    """Return the guard when every assertion sits in an ``if`` whose ``else`` only skips.

    Sibling of ``_conditional_only`` for the shape that detector deliberately
    excludes: an ``else`` branch exists, so the test is not *vacuous* — it is
    *skipped*. That reads as honest reporting but hides a permanent skip when the
    guard can never be true. ``test_tasks.py::test_get_task`` guarded on a task
    row that only Celery dispatch creates, which the autouse fixture patches out,
    so the only test of ``GET /tasks/{task_id}`` skipped every run for 11 months
    while the endpoint returned a hardcoded progress value (issue #431).
    """
    asserts = _asserts(fn)
    if not asserts:
        return None
    for node in ast.walk(fn):
        if not isinstance(node, ast.If) or not node.orelse:
            continue
        body_ids = {id(d) for stmt in node.body for d in ast.walk(stmt)}
        if not all(id(a) in body_ids for a in asserts):
            continue
        # The else branch must do nothing but skip.
        else_ids = {id(d) for stmt in node.orelse for d in ast.walk(stmt)}
        skips = [
            inner
            for stmt in node.orelse
            for inner in ast.walk(stmt)
            if isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == 'skip'
        ]
        if skips and not any(id(a) in else_ids for a in asserts):
            return ast.unparse(node.test)
    return None


def _single_assignments(statements: list[ast.stmt]) -> dict[str, ast.expr]:
    """``name -> value`` for names bound EXACTLY once by a plain ``name = <expr>``.

    Without this, ``endpoints = ["/a", "/b"]`` followed by ``for e in endpoints:`` looked
    dynamic and ``loop-only`` reported 22 false positives — every table-driven test in the
    tree. A name bound more than once, unpacked, augmented, or used as a loop variable is
    NOT resolved: it can hold anything by the time the loop runs.
    """
    counts: Counter[str] = Counter()
    values: dict[str, ast.expr] = {}

    def poison(target: ast.AST) -> None:
        for inner in ast.walk(target):
            if isinstance(inner, ast.Name):
                counts[inner.id] += 2

    for node in statements:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign):
                for target in sub.targets:
                    if isinstance(target, ast.Name):
                        counts[target.id] += 1
                        values[target.id] = sub.value
                    else:
                        poison(target)
            elif isinstance(sub, ast.AnnAssign) and sub.value is not None:
                if isinstance(sub.target, ast.Name):
                    counts[sub.target.id] += 1
                    values[sub.target.id] = sub.value
            elif isinstance(sub, ast.AugAssign | ast.For | ast.AsyncFor):
                poison(sub.target)
            elif isinstance(sub, ast.withitem) and sub.optional_vars is not None:
                poison(sub.optional_vars)
    return {name: value for name, value in values.items() if counts[name] == 1}


def _static_nonempty(
    node: ast.AST, consts: dict[str, ast.expr] | None = None, _seen: frozenset[str] = frozenset()
) -> bool:
    """True when the iterable's length is fixed at parse time AND greater than zero.

    Only these can never execute a loop body zero times. Anything computed at runtime —
    a call, a subscript, a comprehension, an unresolvable name — can be empty, and an empty
    iterable turns every assertion inside the loop into a vacuous pass.
    """
    consts = consts or {}
    if isinstance(node, ast.Name):
        if node.id in _seen or node.id not in consts:
            return False
        return _static_nonempty(consts[node.id], consts, _seen | {node.id})
    if isinstance(node, ast.List | ast.Tuple | ast.Set):
        return bool(node.elts) and all(not isinstance(e, ast.Starred) for e in node.elts)
    if isinstance(node, ast.Dict):
        return bool(node.keys) and all(k is not None for k in node.keys)
    if isinstance(node, ast.Constant):
        return bool(node.value)
    if isinstance(node, ast.Attribute) and node.attr in ('items', 'keys', 'values'):
        return False
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        # `TABLE.items()` / `.keys()` / `.values()` inherit the mapping's static length.
        if func.attr in ('items', 'keys', 'values'):
            return _static_nonempty(func.value, consts, _seen)
        name = func.attr
    else:
        name = getattr(func, 'id', '')
    if name == 'range':
        bounds = [a.value for a in node.args if isinstance(a, ast.Constant)]
        if len(bounds) != len(node.args) or not bounds:
            return False
        if not all(isinstance(b, int) for b in bounds):
            return False
        return bounds[0] > 0 if len(bounds) == 1 else bounds[1] > bounds[0]
    if name in ('enumerate', 'reversed', 'sorted', 'list', 'tuple', 'set', 'dict'):
        return bool(node.args) and _static_nonempty(node.args[0], consts, _seen)
    if name == 'zip':
        return bool(node.args) and all(_static_nonempty(a, consts, _seen) for a in node.args)
    return False


def _loop_only(fn: ast.AST, module_consts: dict[str, ast.expr] | None = None) -> str | None:
    """Return the dynamic iterable when every assertion is inside a ``for`` over one."""
    asserts = _asserts(fn)
    if not asserts:
        return None
    consts = dict(module_consts or {})
    consts.update(_single_assignments(list(getattr(fn, 'body', []))))
    covered: set[int] = set()
    first_dynamic: str | None = None
    for node in ast.walk(fn):
        if not isinstance(node, ast.For | ast.AsyncFor) or _static_nonempty(node.iter, consts):
            continue
        body_ids = {id(d) for stmt in node.body for d in ast.walk(stmt)}
        if not any(id(a) in body_ids for a in asserts):
            continue
        if first_dynamic is None:
            first_dynamic = ast.unparse(node.iter)
        covered |= body_ids
    if first_dynamic is None:
        return None
    return first_dynamic if all(id(a) in covered for a in asserts) else None


def _is_constant(node: ast.AST) -> bool:
    """True for an expression whose value is fixed at parse time."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp):
        return _is_constant(node.operand)
    if isinstance(node, ast.BinOp):
        return _is_constant(node.left) and _is_constant(node.right)
    if isinstance(node, ast.List | ast.Tuple | ast.Set):
        return all(_is_constant(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            k is not None and _is_constant(k) and _is_constant(v)
            for k, v in zip(node.keys, node.values, strict=True)
        )
    return False


def _unfalsifiable(fn: ast.AST) -> list[tuple[int, str]]:
    """(lineno, source) for each assertion whose every operand is a constant."""
    out: list[tuple[int, str]] = []
    for assertion in _asserts(fn):
        test = assertion.test
        if isinstance(test, ast.Compare):
            operands = [test.left, *test.comparators]
            if all(_is_constant(o) for o in operands):
                out.append((assertion.lineno, ast.unparse(test)))
        elif _is_constant(test):
            out.append((assertion.lineno, ast.unparse(test)))
    return out


def _is_weak_test(test: ast.AST) -> bool:
    """True for a bare-truthy or ``is not None`` assertion expression.

    Two deliberate exclusions, both calibrated against this tree:

    * A CALL is not weak. ``assert is_valid(x)`` delegates to a predicate that can genuinely
      return False.
    * ``assert not offenders, "<list>"`` is not weak either — it is this repo's standard
      AST-guard shape and it fails the moment the violation set is non-empty. Treating the
      negated form as weak (as the frontend does for ``toBeFalsy``) reported 40 of these as
      findings. The risk with a guard is that it can never *find* anything, which is a
      different defect and is covered by each guard's own "guard the guard" tests.

    What is left is the shape the E2E auth suite is built on: ``still_on_login = "/login" in
    page.url or page.locator("#email").is_visible()`` then ``assert still_on_login`` — true
    on a page that never navigated, and true on a page that crashed.
    """
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        comparator = test.comparators[0]
        is_none = isinstance(comparator, ast.Constant) and comparator.value is None
        return is_none and isinstance(test.ops[0], ast.IsNot | ast.NotEq)
    return isinstance(test, ast.Name | ast.Attribute | ast.Subscript)


def _weak_only(fn: ast.AST) -> str | None:
    """Return the weak expressions when the test has nothing stronger."""
    asserts = _asserts(fn)
    if not asserts or _asserting_calls(fn):
        return None
    weak = [a for a in asserts if _is_weak_test(a.test)]
    if len(weak) != len(asserts):
        return None
    return ', '.join(sorted({ast.unparse(a.test) for a in weak}))[:120]


def _reads_mock_internals(node: ast.AST) -> bool:
    return any(
        isinstance(inner, ast.Attribute) and inner.attr in _MOCK_INTROSPECTION
        for inner in ast.walk(node)
    )


def _mock_only(fn: ast.AST) -> int | None:
    """Return the assertion count when every assertion is mock bookkeeping."""
    bookkeeping = 0
    real = 0
    for assertion in _asserts(fn):
        if _reads_mock_internals(assertion.test):
            bookkeeping += 1
        else:
            real += 1
    for call in _asserting_calls(fn):
        if isinstance(call.func, ast.Attribute) and call.func.attr in _MOCK_ASSERT_METHODS:
            bookkeeping += 1
        else:
            real += 1
    if bookkeeping and not real:
        return bookkeeping
    return None


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


def _swallows_error(fn: ast.AST) -> str | None:
    """Return the caught exception when a handler discards it entirely.

    Scoped to test functions on purpose: a fixture teardown that best-effort deletes a
    remote object is a legitimate ``except: pass``, whereas in a test body it means the
    assertions after the ``try`` never ran and nobody was told.
    """
    for node in ast.walk(fn):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if not _handler_only_swallows(node.body):
            continue
        caught = ast.unparse(node.type) if node.type else 'bare except'
        return caught
    return None


def _handler_only_swallows(body: list[ast.stmt]) -> bool:
    """True when the handler body does nothing but discard, log, or bail out."""
    for stmt in body:
        if isinstance(stmt, ast.Pass | ast.Continue):
            continue
        if isinstance(stmt, ast.Return) and stmt.value is None:
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            func = stmt.value.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', '')
            if name in _SWALLOW_CALLS:
                continue
        return False
    return bool(body)


def _is_patch_call(node: ast.Call) -> bool:
    """``patch()``, ``patch.object()``, ``mock.patch.dict()``, ``mocker.patch()``."""
    return 'patch' in _dotted(node.func)


def _is_monkeypatch_call(node: ast.Call) -> bool:
    """``monkeypatch.setattr(...)`` and friends — a patch by another name."""
    parts = _dotted(node.func)
    return bool(parts) and parts[0] == 'monkeypatch' and parts[-1] in _MONKEYPATCH_METHODS


def _patch_refs(fn: ast.AST) -> int:
    """Count patch CALLS (decorators included — ``ast.walk`` visits ``decorator_list``)."""
    count = 0
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and (_is_patch_call(node) or _is_monkeypatch_call(node)):
            count += 1
    return count


def scan_source(source: str, rel: str) -> list[Finding]:
    """Scan test source text. Helper functions are scanned for failure-masking too.

    Split out from ``scan_file`` so ``--selftest`` can drive every detector from in-memory
    fixtures. Fixtures on disk would be collected by pytest and linted as real tests.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found: list[Finding] = []
    # Module-level tables (`EXPORT_FORMATS = [...]`) are as static as an inline literal.
    module_consts = _single_assignments(
        [s for s in tree.body if isinstance(s, ast.Assign | ast.AnnAssign)]
    )

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

        for lineno, detail in _negated_status(node):
            found.append(
                Finding(
                    'negated-status',
                    rel,
                    lineno,
                    node.name,
                    f'status_code {detail} — a 500 passes this',
                )
            )

        for lineno, guard, n_asserts in _status_guarded_asserts(node):
            found.append(
                Finding(
                    'status-guarded-assert',
                    rel,
                    lineno,
                    node.name,
                    f'{n_asserts} assert(s) only run when `{guard}`',
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

        guard = _conditional_skip(node)
        if guard is not None:
            found.append(
                Finding(
                    'conditional-skip',
                    rel,
                    node.lineno,
                    node.name,
                    f'all asserts under `if {guard}` whose else only skips',
                )
            )

        iterable = _loop_only(node, module_consts)
        if iterable is not None:
            found.append(
                Finding(
                    'loop-only',
                    rel,
                    node.lineno,
                    node.name,
                    f'all asserts inside `for ... in {iterable}` — empty iterable passes',
                )
            )

        if not _asserts(node) and not _has_raises_or_helper(node):
            found.append(
                Finding('no-assertion', rel, node.lineno, node.name, 'no assert/raises/assert_*')
            )

        for lineno, detail in _unfalsifiable(node):
            found.append(
                Finding(
                    'unfalsifiable', rel, lineno, node.name, f'`{detail}` — every operand constant'
                )
            )

        weak = _weak_only(node)
        if weak is not None:
            found.append(
                Finding(
                    'weak-only', rel, node.lineno, node.name, f'only bare-truthy asserts: {weak}'
                )
            )

        n_bookkeeping = _mock_only(node)
        if n_bookkeeping is not None:
            found.append(
                Finding(
                    'mock-only',
                    rel,
                    node.lineno,
                    node.name,
                    f'{n_bookkeeping} assertion(s), all mock bookkeeping',
                )
            )

        swallowed = _swallows_error(node)
        if swallowed:
            found.append(
                Finding(
                    'error-swallowed',
                    rel,
                    node.lineno,
                    node.name,
                    f'except {swallowed} -> pass/return/log only',
                )
            )

        refs = _patch_refs(node)
        if refs >= _MAX_PATCH_REFS:
            found.append(Finding('mock-heavy', rel, node.lineno, node.name, f'{refs} patch calls'))

    return found


def scan_file(path: Path, root: Path) -> list[Finding]:
    """Scan one test module."""
    try:
        source = path.read_text()
    except UnicodeDecodeError:
        return []
    rel = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
    return scan_source(source, rel)


#: Reason prefix marking an entry as DEFERRED WORK rather than an accepted pattern. Counted
#: and printed separately on every run so a backlog cannot masquerade as a clean suite —
#: the failure mode that let `expected-schemas.tsv` rot for four months.
_BACKLOG_PREFIX = 'BACKLOG'


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


# ------------------------------------------------------------------------------- self-test

#: ``(category, source)`` — each source MUST produce the named category. A detector that
#: silently stops matching reports 0 findings, which is indistinguishable from a clean
#: suite. That is the same sin this script exists to catch, so it audits itself.
SELFTEST_CASES: tuple[tuple[str, str], ...] = (
    (
        'permissive-status',
        'def test_a(client):\n    r = client.get("/x")\n    assert r.status_code in (200, 403)\n',
    ),
    (
        'negated-status',
        'def test_a(client):\n    r = client.get("/x")\n    assert r.status_code != 403, r.text\n',
    ),
    (
        'status-guarded-assert',
        'def test_a(client):\n'
        '    r = client.get("/x")\n'
        '    assert r.status_code == 200\n'
        '    if r.status_code == 200:\n'
        '        assert len(r.json()) == 3\n',
    ),
    (
        'conditional-only',
        'def test_a(feature):\n    if feature:\n        assert feature.value == 1\n',
    ),
    (
        'conditional-skip',
        'def test_a(feature):\n'
        '    if feature:\n'
        '        assert feature.value == 1\n'
        '    else:\n'
        '        pytest.skip("no feature")\n',
    ),
    (
        'loop-only',
        'def test_a(detector):\n'
        '    spans = detector.detect(text)\n'
        '    for s in spans:\n'
        '        assert 0 <= s.start < s.end\n',
    ),
    (
        # A dynamic INNER loop makes the body vacuous even when the outer iterable
        # is a literal — the shape nine test_search_quality tests are written in.
        'loop-only',
        'def test_a(client):\n'
        '    for q in ["china", "fraud"]:\n'
        '        data = client.get(f"/search?q={q}").json()\n'
        '        for r in data["results"]:\n'
        '            assert r["score"] > 0\n',
    ),
    (
        # A name REBOUND from a runtime value is not a static table, even though its first
        # binding is a literal. Resolving single assignments must not resolve this one.
        'loop-only',
        'def test_a(client):\n'
        '    rows = [1]\n'
        '    rows = client.get("/x").json()\n'
        '    for r in rows:\n'
        '        assert r["ok"] == 1\n',
    ),
    ('no-assertion', 'def test_a():\n    compute_the_thing()\n'),
    ('unfalsifiable', 'def test_a():\n    assert 1 == 1\n'),
    ('unfalsifiable', 'def test_a():\n    assert True\n'),
    (
        'weak-only',
        'def test_a(page):\n'
        '    still_on_login = "/login" in page.url or page.locator("#email").is_visible()\n'
        '    assert still_on_login, "should not log in"\n',
    ),
    ('weak-only', 'def test_a(svc):\n    assert svc.result() is not None\n'),
    (
        'mock-only',
        'def test_a(mock_cleanup):\n'
        '    on_pipeline_error("u", "t")\n'
        '    mock_cleanup.assert_called_once_with("u")\n',
    ),
    (
        'mock-only',
        'def test_a(mock_update):\n'
        '    on_pipeline_error("u", "t")\n'
        '    assert mock_update.call_args[0][1] == 42\n',
    ),
    (
        'failure-masking',
        'def test_a():\n'
        '    try:\n'
        '        import fancy\n'
        '        assert fancy.version == 2\n'
        '    except ImportError:\n'
        '        pytest.skip("missing dep")\n',
    ),
    (
        'error-swallowed',
        'def test_a(svc):\n'
        '    try:\n'
        '        assert svc.masked() == "[PHONE]"\n'
        '    except Exception:\n'
        '        pass\n',
    ),
    (
        'error-swallowed',
        'def test_a(svc):\n'
        '    try:\n'
        '        assert svc.masked() == "[PHONE]"\n'
        '    except Exception as e:\n'
        '        logger.warning(e)\n',
    ),
    (
        # Six patch.object calls used to score 12 and three used to score 6; both read as
        # "6 references". Counting calls makes the threshold mean what it says.
        'mock-heavy',
        'def test_a(monkeypatch):\n'
        '    with patch.object(A, "x"), patch.object(B, "y"), patch("m.c"):\n'
        '        monkeypatch.setattr(D, "e", 1)\n'
        '        monkeypatch.setenv("F", "1")\n'
        '        monkeypatch.setitem(G, "h", 2)\n'
        '        assert run() == 3\n',
    ),
    ('fixture-named-test', '@pytest.fixture\ndef test_thing():\n    return 1\n'),
)

#: Sources that must produce NO finding — the false-positive half of the calibration.
SELFTEST_CLEAN: tuple[str, ...] = (
    # An exact status assertion with the real check unguarded: the shape the fixes produce.
    'def test_a(client):\n'
    '    r = client.get("/x")\n'
    '    assert r.status_code == 200, r.text\n'
    '    assert r.json()["my_permission"] == "owner"\n',
    # A non-emptiness assertion outside the loop makes the loop body reachable-or-fail.
    'def test_a(detector):\n'
    '    spans = detector.detect(text)\n'
    '    assert spans, "detector found nothing"\n'
    '    for s in spans:\n'
    '        assert 0 <= s.start < s.end\n',
    # A literal iterable cannot be empty.
    'def test_a():\n    for n in (1, 2, 3):\n        assert n > 0\n',
    # `range(3)` cannot be empty either.
    'def test_a():\n    for n in range(3):\n        assert n >= 0\n',
    # A local bound ONCE to a literal table is as static as the literal (22 table-driven
    # tests were false positives before single-assignment resolution).
    'def test_a(client):\n'
    '    endpoints = ["/api/a", "/api/b"]\n'
    '    for e in endpoints:\n'
    '        assert client.get(e).status_code == 401\n',
    # Same for a module-level constant, and for `.items()` over a literal mapping.
    'SCHEMAS = {"a": 1, "b": 2}\n'
    'def test_a():\n'
    '    for name, value in SCHEMAS.items():\n'
    '        assert value > 0, name\n',
    # pytest.raises IS an assertion (no-assertion must not fire).
    'def test_a():\n    with pytest.raises(ValueError):\n        parse("nope")\n',
    # Playwright's expect() is the only assertion in most E2E tests.
    'def test_a(page):\n    expect(page.locator("#x")).to_be_visible()\n',
    # A mock call assertion ALONGSIDE a real one is not mock-only.
    'def test_a(mock_send):\n'
    '    result = run()\n'
    '    mock_send.assert_called_once_with(7)\n'
    '    assert result == {"batch_id": "b1"}\n',
    # A predicate call is real evidence, not bare truthy.
    'def test_a():\n    assert is_valid_uuid(value)\n',
    # `assert not <violations>` is the AST-guard idiom and fails on any violation.
    'def test_a():\n'
    '    offenders = scan_the_tree()\n'
    '    assert not offenders, f"unmarked DDL in {offenders}"\n',
    # A guarded assertion plus an unguarded one is not conditional-only.
    'def test_a(flag):\n    assert base() == 1\n    if flag:\n        assert flag.value == 2\n',
    # An except handler that asserts on the exception is neither masking nor swallowing.
    'def test_a():\n'
    '    try:\n'
    '        parse("nope")\n'
    '    except ValueError as e:\n'
    '        assert "nope" in str(e)\n',
    # Two patches is normal test setup, not scaffolding.
    'def test_a():\n    with patch("m.a"), patch.object(B, "c"):\n        assert run() == 1\n',
    # A real fixture that is not named test_*.
    '@pytest.fixture\ndef thing():\n    return 1\n',
    # `if r.status_code != 200:` is control flow, not a claim — negated-status must not fire.
    'def test_a(client):\n'
    '    r = client.get("/x")\n'
    '    if r.status_code != 200:\n'
    '        pytest.fail(r.text)\n'
    '    assert r.json()["ok"] == 1\n',
)


def run_selftest(verbose: bool = True) -> list[str]:
    """Return a list of failure descriptions — empty means every detector is alive."""
    failures: list[str] = []
    for category, source in SELFTEST_CASES:
        got = {f.category for f in scan_source(source, 'fixture.py')}
        if category not in got:
            failures.append(f'{category} did not fire (got {sorted(got) or "nothing"})')
        if verbose:
            mark = '\033[31m✗' if category not in got else '\033[32m✓'
            print(f'  {mark}\033[0m fires {category}')
    for i, source in enumerate(SELFTEST_CLEAN, start=1):
        found = scan_source(source, 'fixture.py')
        if found:
            detail = ', '.join(f'{f.category}: {f.detail}' for f in found)
            failures.append(f'clean case {i} produced {detail}')
        if verbose:
            mark = '\033[31m✗' if found else '\033[32m✓'
            print(f'  {mark}\033[0m clean case {i} produces no finding')
    return failures


def _selftest_main() -> int:
    print('\n\033[1maudit-tests self-test\033[0m\n')
    failures = run_selftest()
    if failures:
        print(f'\n\033[31m{len(failures)} self-test failure(s) — a detector is broken\033[0m')
        for line in failures:
            print(f'  {line}')
        print()
        return 1
    total = len(SELFTEST_CASES) + len(SELFTEST_CLEAN)
    print(f'\n\033[32mall {total} self-test cases pass\033[0m\n')
    return 0


# ----------------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument('root', type=Path, nargs='?', help='test tree to scan (e.g. backend/tests)')
    ap.add_argument('--category', choices=CATEGORIES, help='limit to one detector')
    ap.add_argument('--json', action='store_true', help='machine-readable output')
    ap.add_argument('--no-e2e', action='store_true', help='skip tests/e2e (scanned by default)')
    ap.add_argument('--list', action='store_true', help='print every finding, not just counts')
    ap.add_argument(
        '--selftest', action='store_true', help='audit the auditor against in-memory fixtures'
    )
    args = ap.parse_args()

    if args.selftest:
        return _selftest_main()
    if args.root is None:
        ap.error('root is required unless --selftest is given')
    if not args.root.is_dir():
        print(f'error: {args.root} is not a directory', file=sys.stderr)
        return 2

    # A broken detector reports zero findings, so never let the tree scan speak without it.
    selftest_failures = run_selftest(verbose=False)

    findings: list[Finding] = []
    for path in sorted(args.root.rglob('*.py')):
        if args.no_e2e and 'e2e' in path.parts:
            continue
        findings.extend(scan_file(path, args.root))

    if args.category:
        findings = [f for f in findings if f.category == args.category]

    allowed = load_allowlist(args.root)
    unallowed = [f for f in findings if f.key not in allowed]
    backlog = [f for f in findings if allowed.get(f.key, '').startswith(_BACKLOG_PREFIX)]
    accepted = len(findings) - len(unallowed) - len(backlog)

    # An allowlist entry whose finding is gone is an entry nobody will ever delete. Only
    # meaningful on a FULL scan — a filtered one has not looked at the other categories.
    full_scan = not args.category and not args.no_e2e
    stale = sorted(set(allowed) - {f.key for f in findings}) if full_scan else []

    if args.json:
        print(
            json.dumps(
                {
                    'total': len(findings),
                    'accepted': accepted,
                    'backlog': len(backlog),
                    'unallowlisted': len(unallowed),
                    'stale_allowlist_entries': stale,
                    'selftest_failures': selftest_failures,
                    'by_category': dict(Counter(f.category for f in unallowed)),
                    'backlog_by_category': dict(Counter(f.category for f in backlog)),
                    'findings': [f.__dict__ for f in unallowed],
                },
                indent=2,
            )
        )
        return 1 if (unallowed or stale or selftest_failures) else 0

    counts = Counter(f.category for f in findings)
    backlog_counts = Counter(f.category for f in backlog)
    print(
        f'\n\033[1m{args.root}\033[0m — {len(findings)} findings: '
        f'{len(unallowed)} open, {len(backlog)} backlog, {accepted} accepted\n'
    )
    for category in CATEGORIES:
        hits = [f for f in unallowed if f.category == category]
        total = counts.get(category, 0)
        colour = '\033[31m' if hits else '\033[32m'
        deferred = backlog_counts.get(category, 0)
        suffix = f', {deferred} backlog' if deferred else ''
        print(f'  {colour}{category:22s}\033[0m {len(hits):4d} open  ({total} total{suffix})')
        if args.list or len(hits) <= 5:
            for f in hits[: (None if args.list else 5)]:
                print(f'      {f.path}:{f.line} {f.test} — {f.detail}')
        elif hits:
            print(f'      … {len(hits)} findings (--list)')

    if selftest_failures:
        print(f'\n\033[31mSELF-TEST BROKEN — {len(selftest_failures)} detector(s) dead:\033[0m')
        for line in selftest_failures:
            print(f'  {line}')
        print('  The counts above are not trustworthy. Run --selftest.\n')
    if stale:
        print(f'\n\033[31m{len(stale)} allowlist entry(ies) no longer match any finding:\033[0m')
        for key in stale[:20]:
            print(f'  {key}')
        if len(stale) > 20:
            print(f'  … {len(stale) - 20} more')
        print(f'  Delete them from {args.root / _ALLOWLIST_NAME} — a stale exemption is a')
        print('  blanket nobody reviews, and it hides the next real finding in that test.\n')
    if backlog:
        print(
            f'\033[1;33m{len(backlog)} finding(s) are DEFERRED WORK, not accepted patterns.\033[0m'
        )
        print(
            f'  They carry a `{_BACKLOG_PREFIX}` reason in {args.root / _ALLOWLIST_NAME}. '
            'This gate is green\n  because nothing NEW landed — not because the tree is clean.'
        )
    if unallowed:
        print(f'\n\033[31m{len(unallowed)} findings need a fix or an allowlist entry\033[0m')
        print(
            f'  allowlist: {args.root / _ALLOWLIST_NAME}'
            '  (one "<file>::<test>::<category>  # reason" per line)\n'
        )
    if unallowed or stale or selftest_failures:
        return 1
    print('\n\033[32mno un-allowlisted findings\033[0m\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
