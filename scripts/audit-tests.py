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
    external-service-mock
        A test whose **id claims integration with an external service while that service is
        substituted**. The convention (issue #431, decided from the #403 RAG work): "ran
        against the real engine" and "ran against a stand-in" must be **different test ids** —
        a stand-in is never quietly substituted under the same id. Twelve tests for an
        OpenSearch ``delete_by_query`` body once ran entirely against an in-memory stand-in
        under ids that read as real-engine coverage; the suite was green on an assumption about
        the engine, and neither JUnit history nor the timing analyser could see it. A claim is
        only counted where the test DECLARES one — a marker/env gate, a realness word in the
        test's own name, or the module path. The service's own name is deliberately NOT a
        claim: it names the subject, not the engine.
    readiness-probe-target
        A health/readiness probe whose **target is hardcoded instead of derived from the stack
        under test**. ``wait_for_bench_backend_health`` polled ``opentranscribe-backend`` — the
        DEV stack — so the bench stack's readiness wait returned "healthy" whenever dev was up,
        regardless of whether the stack under test had started at all. That green-lights a
        benchmark against a stack that may not exist. Fires only when EVERY argument of the
        probe call is fixed at parse time; one derived argument means the target follows the
        stack under test.

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
import re
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
    'external-service-mock',
    'readiness-probe-target',
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


# -------------------------------------------------------------- external-service substitution

#: External services whose behaviour a test can only really prove against the running thing.
#: ``patch`` = substrings that identify the service in a patch/monkeypatch target;
#: ``name`` = words that identify it in an identifier (test name, fixture name, filename).
#: The two differ on purpose: ``boto3`` names S3 in a patch target and never in a test id,
#: while ``http`` names a transport in a fixture name (``fake_http``) and matches far too much
#: as a patch substring.
_EXTERNAL_SERVICES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    'opensearch': (
        frozenset({'opensearch', 'elasticsearch'}),
        frozenset({'opensearch', 'elasticsearch'}),
    ),
    's3': (frozenset({'minio', 'boto3', 'botocore', 's3'}), frozenset({'minio', 's3'})),
    'redis': (frozenset({'redis'}), frozenset({'redis'})),
    'llm': (
        frozenset({'llm', 'openai', 'anthropic', 'ollama', 'bedrock'}),
        frozenset({'llm', 'openai', 'anthropic', 'ollama', 'bedrock'}),
    ),
    'smtp': (frozenset({'smtp', 'smtplib'}), frozenset({'smtp'})),
    'http': (
        frozenset({'requests', 'httpx', 'aiohttp', 'urllib', 'urlopen'}),
        frozenset({'http', 'httpx', 'requests'}),
    ),
}

#: Words that mark an identifier as naming a *substitute*. A fixture called ``fake_opensearch``
#: says what it is; the failure mode is a fixture like that wired into a test id that says the
#: opposite.
_DOUBLE_WORDS = frozenset(
    {
        'mock',
        'mocks',
        'mocked',
        'fake',
        'fakes',
        'faked',
        'stub',
        'stubs',
        'stubbed',
        'dummy',
        'patched',
        'inmemory',
        'standin',
        'spy',
    }
)

#: Words in a test id that claim the REAL service was exercised.
_REAL_CLAIM_WORDS = frozenset({'integration', 'integrations', 'real', 'live', 'e2e', 'actual'})

#: Multi-word claims that survive word-splitting badly (``end_to_end`` -> end, to, end).
_REAL_CLAIM_PHRASES = ('end_to_end', 'endtoend', 'against_real', 'realworld')

#: Words that make an id honest about running against a substitute. ``unit`` counts: a test
#: under ``tests/unit/`` has already declared what it is, and its id says so.
_HONEST_WORDS = _DOUBLE_WORDS | frozenset({'unit', 'offline', 'simulated', 'contract'})

#: Honest phrases, same reason as ``_REAL_CLAIM_PHRASES``.
_HONEST_PHRASES = ('in_memory', 'stand_in', 'no_stack', 'without_', 'no_service')


#: Word splitter that also breaks camelCase, so ``LLMService`` -> ``llm``, ``service``.
#: Matching service tokens as SUBSTRINGS instead cost a false positive immediately:
#: ``llm`` lives inside ``mfa_enro-llm-ent_module``, which made an MFA test read as a
#: mislabelled LLM integration test.
_WORD_RE = re.compile(r'[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+')


def _words(text: str) -> set[str]:
    """Lowercase word set of an identifier or path (``api/test_s3_live.py`` -> api, test, …)."""
    return {m.group(0).lower() for m in _WORD_RE.finditer(text)}


def _service_of(text: str, *, use_name_tokens: bool) -> str | None:
    """The external service an identifier or patch target names, if any."""
    text_words = _words(text)
    for service, (patch_tokens, name_tokens) in _EXTERNAL_SERVICES.items():
        tokens = name_tokens if use_name_tokens else patch_tokens
        if tokens & text_words:
            return service
    return None


def _params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Every parameter name — for a test or fixture, the fixtures it requests."""
    return [a.arg for a in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]]


def _is_configuration_patch(target: str) -> bool:
    """Is this patch redirecting CONFIGURATION rather than substituting a service?

    ``monkeypatch.setattr(settings, "OPENSEARCH_CHUNKS_INDEX", scratch)`` changes which index
    the code under test writes to. The client is untouched: every assertion still executes on
    the real engine. Pointing a real client at a throwaway index is the *correct* shape for an
    integration test — it is how you get real engine semantics without touching the live index.

    This exists because the detector fired on all 8 tests in
    ``tests/integration/test_rename_propagation_chunks.py``, which do exactly that against a
    real OpenSearch 3.4. Reported by the #403 work, and their framing is the reason it was
    worth fixing rather than allowlisting: **a detector that fires on correct code teaches
    people to allowlist, and an allowlist habit is how the 41-finding backlog formed.**

    The discrimination is a service patch replaces the client or something that returns one
    (``get_opensearch_client``, ``OpenSearch``, ``.search``, ``.index``), whereas a
    configuration patch sets a scalar on a settings object.
    """
    first = target.split(',')[0].strip()
    # `settings` / `Settings` / `app.core.config.settings` as the patch target object.
    if re.search(r'(^|\.)settings$', first, re.IGNORECASE):
        return True
    # The dotted string form: patch("app.core.config.settings.OPENSEARCH_CHUNKS_INDEX").
    return bool(re.search(r'config\.settings\.[A-Z_]+', first))


def _direct_patch_services(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Services substituted by ``patch``/``monkeypatch`` calls written INSIDE this function."""
    services: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and (_is_patch_call(node) or _is_monkeypatch_call(node)):
            target = ', '.join(ast.unparse(a) for a in node.args[:2])
            if _is_configuration_patch(target):
                continue
            service = _service_of(target, use_name_tokens=False)
            if service is not None:
                services.add(service)
    return services


def _fixture_services(tree: ast.Module) -> dict[str, set[str]]:
    """``fixture name -> services it substitutes``, resolved transitively within the module.

    **This is the part that makes the detector see the real defect.** The #400 suite installs
    its in-memory OpenSearch through a fixture called ``fake_index``::

        @pytest.fixture
        def fake_index(monkeypatch):
            client = _FakeIndex()
            monkeypatch.setattr(svc, 'opensearch_client', client)

    Every test then takes ``fake_index`` and contains no ``patch`` call at all. Reading only
    the test body sees nothing, and reading only the *parameter name* sees nothing either —
    ``fake_index`` says "fake" but never says which service, and naming the service in the
    fixture is a convention no test can be relied on to follow. Resolving the fixture's own
    body is what turns "a name that looks harmless" into "monkeypatches OpenSearch".

    Verified against the real file: before this, the genuine #400 suite stayed clean even when
    deliberately mislabelled into ``tests/integration/`` — the detector fired on its synthetic
    fixture and on nothing else, which is the failure mode this auditor exists to catch.
    """
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
    services = {fn.name: _direct_patch_services(fn) for fn in functions}
    requests_of = {fn.name: _params(fn) for fn in functions}

    # A fixture that requests another fixture inherits its substitutions. Iterate to a
    # fixpoint rather than recursing: fixture graphs in conftest-heavy trees contain cycles
    # through overridden names, and a recursive walk would not terminate.
    for _ in range(len(functions) + 1):
        changed = False
        for name, requested in requests_of.items():
            inherited = {s for r in requested for s in services.get(r, ())}
            if inherited - services[name]:
                services[name] |= inherited
                changed = True
        if not changed:
            break
    return {name: found for name, found in services.items() if found}


def _substitution_targets(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, fixture_services: dict[str, set[str]]
) -> list[tuple[str, str]]:
    """``(service, evidence)`` for every external-service client this test substitutes.

    Three shapes, in descending order of how visible they are in the test itself:

    * a ``patch``/``monkeypatch`` call in the test body;
    * a requested **fixture** that patches the service — resolved through
      :func:`_fixture_services`, and the shape that hid the #400 near-miss entirely;
    * a parameter whose name says "double" *and* names the service
      (``fake_opensearch``, ``mock_redis_unavailable``). Kept as the fallback for fixtures
      defined in a ``conftest.py`` this scan cannot see.
    """
    out: list[tuple[str, str]] = []
    for service in sorted(_direct_patch_services(fn)):
        out.append((service, f'patches {service}'))

    for param in _params(fn):
        resolved = fixture_services.get(param)
        if resolved:
            for service in sorted(resolved):
                out.append((service, f'fixture {param} patches {service}'))
            continue
        if _words(param) & _DOUBLE_WORDS:
            service = _service_of(param, use_name_tokens=True)
            if service is not None:
                out.append((service, f'fixture {param}'))
    return out


#: Markers that DECLARE a test needs the real thing. A marker is part of test selection, so
#: `-m integration` picking up a fully mocked test is the mislabel in its purest form.
_REAL_CLAIM_MARKERS = frozenset({'integration', 'e2e'})

#: Env gates that declare the same thing. ``skipif(SKIP_OPENSEARCH, …)`` means "this test only
#: runs when the real OpenSearch is reachable" — the conftest sets it by TCP probe.
_SERVICE_SKIP_GATES = {
    'skip_opensearch': 'opensearch',
    'skip_s3': 's3',
    'skip_minio': 's3',
    'skip_redis': 'redis',
}


def _marker_claims(decorators: list[ast.expr]) -> set[str]:
    """Service names (or ``*`` for any) claimed by a decorator set.

    Recognises ``@pytest.mark.integration``, ``@pytest.mark.needs_opensearch``,
    ``@pytest.mark.<service>``, and ``@pytest.mark.skipif(SKIP_S3, …)``.
    """
    claims: set[str] = set()
    for dec in decorators:
        target = dec.func if isinstance(dec, ast.Call) else dec
        parts = _dotted(target)
        if 'mark' not in parts:
            continue
        marker = parts[-1]
        marker_words = _words(marker)
        if marker in _REAL_CLAIM_MARKERS:
            claims.add('*')
        if marker in ('skipif', 'skipIf') and isinstance(dec, ast.Call):
            gate_text = ' '.join(ast.unparse(a) for a in dec.args).lower()
            for gate, service in _SERVICE_SKIP_GATES.items():
                if gate in gate_text:
                    claims.add(service)
        for service, (_, name_tokens) in _EXTERNAL_SERVICES.items():
            needs = marker.startswith(('needs_', 'requires_'))
            if (name_tokens & marker_words) and (needs or marker in name_tokens):
                claims.add(service)
    return claims


def _pytestmark_values(scope: ast.Module | ast.ClassDef) -> list[ast.expr]:
    """The marker expressions of a ``pytestmark = …`` assignment, module- or class-level."""
    out: list[ast.expr] = []
    for statement in scope.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == 'pytestmark' for t in statement.targets):
            continue
        value = statement.value
        out.extend(value.elts if isinstance(value, ast.List | ast.Tuple) else [value])
    return out


def _external_service_mock(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    rel: str,
    module_claims: set[str],
    fixture_services: dict[str, set[str]],
) -> tuple[str, str] | None:
    """Return ``(services, evidence)`` when a test id claims the real service but mocks it.

    The decided convention (issue #431, from the #403 RAG work): **"ran against the real
    engine" and "ran against a stand-in" must be different test ids.** A suite of twelve tests
    for a ``delete_by_query`` body once ran entirely against an in-memory OpenSearch stand-in
    while carrying ids that read as real-engine coverage — a green suite resting on an
    assumption about the engine, invisible to JUnit history and to the timing analyser.

    A claim of realness is only counted where the test *declares* one, in descending order of
    how hard it is to argue with:

    * a **marker or env gate** — ``@pytest.mark.integration``, ``@pytest.mark.needs_redis``,
      ``skipif(SKIP_OPENSEARCH, …)``, or a module-level ``pytestmark``. These drive selection,
      so this is the strongest form and no naming excuses it;
    * a **realness word in the test's own name** (``test_real_delete_by_query``,
      ``…_against_live_opensearch``). No directory makes that id honest;
    * a **realness word in the module path** (``tests/integration/``, ``tests/e2e/``) — a
      weaker, positional claim, excused when the location or the name declares the substitute.

    The service's own name in the test id is deliberately NOT a claim. It names the *subject*,
    not the engine: on this tree that tier fired on 20 honestly-named unit tests
    (``test_blacklist_token_redis_unavailable_fail_secure``, whose fixture is literally called
    ``mock_redis_unavailable``), which is exactly the false-positive class that makes a gate
    get ignored.
    """
    substitutions = _substitution_targets(fn, fixture_services)
    if not substitutions:
        return None
    substituted = {service for service, _ in substitutions}

    name = fn.name.lower()
    path = rel.lower()
    name_words = _words(name)
    path_words = _words(path)

    claims = module_claims | _marker_claims(fn.decorator_list)
    declared = substituted if '*' in claims else substituted & claims

    honest_name = bool(name_words & _HONEST_WORDS) or any(p in name for p in _HONEST_PHRASES)
    honest_path = bool(path_words & _HONEST_WORDS) or any(p in path for p in _HONEST_PHRASES)
    named = bool(name_words & _REAL_CLAIM_WORDS) or any(p in name for p in _REAL_CLAIM_PHRASES)
    positional = bool(path_words & _REAL_CLAIM_WORDS) or any(p in path for p in _REAL_CLAIM_PHRASES)

    # A realness word in a test name claims the real SERVICE only when the name says which
    # service. `test_a_real_session_still_authorizes_enrollment` uses "real" to qualify the
    # session object, not Redis, and was this detector's first false positive.
    named_services = {s for s in substituted if _EXTERNAL_SERVICES[s][1] & name_words}

    if declared:
        claimed, why = declared, 'marker/env gate'
    elif named and named_services and not honest_name:
        claimed, why = named_services, 'test name'
    elif positional and not (honest_name or honest_path):
        claimed, why = substituted, 'module path'
    else:
        return None

    evidence = '; '.join(sorted({e for _, e in substitutions}))[:100]
    return f'{", ".join(sorted(claimed))} ({why})', evidence


# ------------------------------------------------------------------- readiness probe targets

#: Words that make a function, or a called helper, a readiness/health probe. Matched as WORDS,
#: not substrings: ``ready`` inside ``already`` would otherwise make every
#: ``test_already_*`` test a probe, and there are a dozen of those in this tree.
_PROBE_WORDS = frozenset(
    {
        'ready',
        'readiness',
        'reachable',
        'health',
        'healthy',
        'wait',
        'poll',
        'polling',
        'probe',
    }
)

#: Calls that actually reach out to an endpoint.
_PROBE_CALLS = frozenset(
    {
        'create_connection',
        'connect',
        'connect_ex',
        'get',
        'head',
        'post',
        'request',
        'urlopen',
        'run',
        'check_output',
        'check_call',
        'ping',
        'inspect',
    }
)

#: Substrings that make a string literal a concrete endpoint rather than a relative path.
#: The container-name prefixes are here because that is the shape the real bug took:
#: ``wait_for_bench_backend_health`` polled ``opentranscribe-backend`` — the DEV stack — so
#: the bench stack's readiness wait returned "healthy" whenever dev was up, regardless of
#: whether the stack under test had started at all.
_ENDPOINT_SUBSTRINGS = (
    'localhost',
    '127.0.0.1',
    '0.0.0.0',
    '[::1]',
    'opentranscribe-',
    'otbench-',
    'otfresh-',
)

_HOST_PORT_RE = re.compile(r'[a-z0-9][a-z0-9.\-]*:\d{2,5}(?!\d)')


def _fully_literal(
    node: ast.AST, consts: dict[str, ast.expr], _seen: frozenset[str] = frozenset()
) -> bool:
    """True when the expression's value is fixed at parse time, resolving single assignments.

    ``BASE_URL = "http://localhost:5174/api"`` then ``f"{BASE_URL}/auth/login"`` is as
    hardcoded as the literal; ``os.environ.get("MINIO_PORT", "5178")`` is not.
    """
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Name):
        if node.id in _seen or node.id not in consts:
            return False
        return _fully_literal(consts[node.id], consts, _seen | {node.id})
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return all(_fully_literal(e, consts, _seen) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            k is not None and _fully_literal(k, consts, _seen) and _fully_literal(v, consts, _seen)
            for k, v in zip(node.keys, node.values, strict=True)
        )
    if isinstance(node, ast.JoinedStr):
        return all(
            _fully_literal(v.value if isinstance(v, ast.FormattedValue) else v, consts, _seen)
            for v in node.values
        )
    if isinstance(node, ast.BinOp):
        return _fully_literal(node.left, consts, _seen) and _fully_literal(
            node.right, consts, _seen
        )
    return False


def _literal_strings(node: ast.AST, consts: dict[str, ast.expr]) -> list[str]:
    """Every string constant reachable from the expression, resolving single assignments."""
    out: list[str] = []
    stack: list[tuple[ast.AST, frozenset[str]]] = [(node, frozenset())]
    while stack:
        current, seen = stack.pop()
        if isinstance(current, ast.Constant):
            if isinstance(current.value, str):
                out.append(current.value)
            continue
        if isinstance(current, ast.Name):
            if current.id not in seen and current.id in consts:
                stack.append((consts[current.id], seen | {current.id}))
            continue
        for child in ast.iter_child_nodes(current):
            stack.append((child, seen))
    return out


def _is_endpoint_literal(text: str) -> bool:
    lowered = text.lower()
    if any(sub in lowered for sub in _ENDPOINT_SUBSTRINGS):
        return True
    return bool(_HOST_PORT_RE.search(lowered))


def _is_probe_name(name: str) -> bool:
    return bool(_words(name) & _PROBE_WORDS) or 'is_up' in name.lower()


def _skip_gated_calls(body: list[ast.stmt]) -> set[int]:
    """Ids of calls inside a reachability gate — ``try/except -> skip`` or ``if …: skip``.

    A gate that decides "the stack is not there, skip" IS a readiness probe, whatever it is
    called. ``test_selective_reprocess.py``'s ``auth_token`` is the shape: a POST wrapped in
    ``except requests.ConnectionError: pytest.skip("Dev environment not running")``.
    """
    gated: set[int] = set()
    for statement in body:
        for node in ast.walk(statement):
            skips: list[ast.AST] = []
            probed: list[ast.AST] = []
            if isinstance(node, ast.Try):
                skips = list(node.handlers)
                probed = list(node.body)
            elif isinstance(node, ast.If):
                skips = [*node.body, *node.orelse]
                probed = [node.test]
            if not skips or not probed:
                continue
            calls_skip = any(
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == 'skip'
                for stmt in skips
                for inner in ast.walk(stmt)
            )
            if not calls_skip:
                continue
            for stmt in probed:
                for inner in ast.walk(stmt):
                    if isinstance(inner, ast.Call):
                        gated.add(id(inner))
    return gated


def _nested_call_ids(scope: ast.AST) -> set[int]:
    """Ids of every call that lives inside a function defined within ``scope``.

    Without this the same probe is reported twice. ``scan_source`` scans each function via
    ``ast.walk(tree)`` — which yields methods and nested functions too — and then scans module
    scope separately; a probe inside a test **method** was therefore reported once under the
    method's name and again under ``<module>``, needing two allowlist entries for one defect.
    """
    out: set[int] = set()
    for node in ast.walk(scope):
        if node is scope or not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call):
                out.add(id(inner))
    return out


def _readiness_probe_targets(
    fn: ast.FunctionDef | ast.AsyncFunctionDef | ast.Module,
    name: str,
    module_consts: dict[str, ast.expr],
) -> list[tuple[int, str]]:
    """``(lineno, target)`` for each probe whose endpoint is hardcoded, not derived.

    Fires only when EVERY argument of the probe call is fixed at parse time. One derived
    argument means the target follows the stack under test: ``_reachable("127.0.0.1", port)``
    against a subprocess on an allocated port is correct and must stay clean, while
    ``_reachable("127.0.0.1", 5199)`` asks about whichever stack happens to own the dev
    stack's published port.
    """
    body = list(getattr(fn, 'body', []))
    consts = dict(module_consts)
    consts.update(_single_assignments(body))
    whole_fn_is_probe = _is_probe_name(name)
    gated = _skip_gated_calls(body)
    # Calls belonging to an inner function are that function's findings, not this scope's.
    nested = _nested_call_ids(fn)

    out: list[tuple[int, str]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call) or id(node) in nested:
            continue
        callee = (
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, 'id', '')
        )
        # A call to a probe HELPER (`_service_reachable(...)`) is a probe wherever it appears.
        # A bare `.get(...)` only becomes one inside a probe function or a reachability gate.
        named_probe = _is_probe_name(callee)
        contextual_probe = callee in _PROBE_CALLS and (whole_fn_is_probe or id(node) in gated)
        if not (named_probe or contextual_probe):
            continue
        arguments = [*node.args, *[k.value for k in node.keywords]]
        if not arguments or any(isinstance(a, ast.Starred) for a in arguments):
            continue
        if not all(_fully_literal(a, consts) for a in arguments):
            continue
        endpoints = [
            s for a in arguments for s in _literal_strings(a, consts) if _is_endpoint_literal(s)
        ]
        if endpoints:
            out.append((node.lineno, f'{callee}(…{endpoints[0]}…)'))
    return out


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
    # `pytest.mark.integration` on the module or the class reaches every test inside it, so a
    # decorator-only read of the claim would miss whole suites.
    module_claims = _marker_claims(_pytestmark_values(tree))
    # Which fixture in this module substitutes which service — see `_fixture_services`.
    fixture_services = _fixture_services(tree)
    class_claims: dict[int, set[str]] = {}
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        claims = _marker_claims([*cls.decorator_list, *_pytestmark_values(cls)])
        if not claims:
            continue
        for inner in ast.walk(cls):
            if isinstance(inner, ast.FunctionDef | ast.AsyncFunctionDef):
                class_claims.setdefault(id(inner), set()).update(claims)

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

        # Probes live in helpers and fixtures more often than in tests, so this one is not
        # gated on `is_test`.
        for lineno, target in _readiness_probe_targets(node, node.name, module_consts):
            found.append(
                Finding(
                    'readiness-probe-target',
                    rel,
                    lineno,
                    node.name,
                    f'{target} — hardcoded endpoint, not the stack under test',
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

        mislabelled = _external_service_mock(
            node, rel, module_claims | class_claims.get(id(node), set()), fixture_services
        )
        if mislabelled is not None:
            services, evidence = mislabelled
            found.append(
                Finding(
                    'external-service-mock',
                    rel,
                    node.lineno,
                    node.name,
                    f'id claims real {services}, but {evidence}',
                )
            )

    # A conftest's service detection runs at import time, outside any function — and that is
    # where the probe that decides which stack the whole suite talks to actually lives. The
    # whole tree is passed: `_readiness_probe_targets` drops calls owned by an inner function,
    # which is what keeps a probe inside a test METHOD from being reported here as well.
    for lineno, target in _readiness_probe_targets(tree, '<module>', module_consts):
        found.append(
            Finding(
                'readiness-probe-target',
                rel,
                lineno,
                '<module>',
                f'{target} — hardcoded endpoint, not the stack under test',
            )
        )

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
    (
        # The #400 near-miss, relabelled the way the convention forbids: an in-memory
        # stand-in wearing an id that claims the engine.
        'external-service-mock',
        'def test_delete_by_query_against_real_opensearch(fake_opensearch):\n'
        '    index_chunks(file_id=1, chunks=[])\n'
        '    assert fake_opensearch.deleted == [{"range": {"chunk_index": {"gte": 3}}}]\n',
    ),
    (
        # A marker is the strongest claim there is: `-m integration` SELECTS this test.
        'external-service-mock',
        '@pytest.mark.integration\n'
        'def test_stale_tail_is_pruned(monkeypatch):\n'
        '    monkeypatch.setattr("app.services.search.opensearch_client", _Fake())\n'
        '    assert prune(1) == 3\n',
    ),
    (
        # ...and it reaches every test in the module through `pytestmark`.
        'external-service-mock',
        'pytestmark = pytest.mark.integration\n'
        'def test_upload_writes_the_object():\n'
        '    with patch("boto3.client") as client:\n'
        '        store("k", b"v")\n'
        '    assert client.called\n',
    ),
    (
        # `skipif(SKIP_OPENSEARCH, ...)` declares "only runs against the real engine".
        'external-service-mock',
        '@pytest.mark.skipif(SKIP_OPENSEARCH, reason="needs the engine")\n'
        'def test_hybrid_search_ranks_by_rrf(monkeypatch):\n'
        '    monkeypatch.setattr("app.services.search.opensearch_service.client", _Fake())\n'
        '    assert rank(["a"]) == ["a"]\n',
    ),
    (
        # The bench bug: a readiness wait pinned to the DEV stack's container name, so it
        # reported healthy whenever dev was up and the stack under test never mattered.
        'readiness-probe-target',
        'def wait_for_bench_backend_health(timeout=180):\n'
        '    out = subprocess.check_output(["docker", "inspect", "opentranscribe-backend"])\n'
        '    return b"healthy" in out\n',
    ),
    (
        # Same defect through a module constant: resolving single assignments is what makes
        # `f"{BASE_URL}/health"` as hardcoded as the literal it came from.
        'readiness-probe-target',
        'BASE_URL = "http://localhost:5174/api"\n'
        'def test_stack_is_up():\n'
        '    try:\n'
        '        r = requests.get(f"{BASE_URL}/health", timeout=5)\n'
        '    except requests.ConnectionError:\n'
        '        pytest.skip("stack not running")\n'
        '    assert r.status_code == 200\n',
    ),
)

#: Sources that must produce NO finding — the false-positive half of the calibration.
SELFTEST_CLEAN: tuple[str, ...] = (
    # Redirecting a REAL client at a throwaway index is the correct shape for an
    # integration test: `settings.OPENSEARCH_CHUNKS_INDEX` is the index NAME, not the
    # client, and every assertion still executes on the real engine. The detector fired on
    # all 8 tests in tests/integration/test_rename_propagation_chunks.py for this
    # (reported by #403). A detector that fires on correct code teaches people to
    # allowlist, and an allowlist habit is how the 41-finding backlog formed.
    '@pytest.mark.integration\n'
    'def test_rename_propagates_to_chunks(chunk_index, monkeypatch):\n'
    '    monkeypatch.setattr(settings, "OPENSEARCH_CHUNKS_INDEX", chunk_index)\n'
    '    assert propagate(1) == 3\n',
    # The dotted-string form of the same thing.
    '@pytest.mark.integration\n'
    'def test_reindex_uses_the_scratch_index():\n'
    '    with patch("app.core.config.settings.OPENSEARCH_CHUNKS_INDEX", "scratch"):\n'
    '        assert reindex(1) == 3\n',
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
    # A stand-in under an id that claims nothing is the CORRECT half of the convention —
    # this is the #400 suite as actually written, and it must stay clean.
    'def test_stale_tail_is_dropped(fake_opensearch):\n'
    '    index_chunks(1, [])\n'
    '    assert fake_opensearch.deleted == ["chunk_index >= 3"]\n',
    # "real" qualifying something that is not the service. The first false positive this
    # detector produced: "a real session", with Redis faked, and Redis nowhere in the name.
    'def test_a_real_session_still_authorizes_enrollment(fake_redis):\n'
    '    assert authorize(session="s1") == "ok"\n',
    # The service NAMING itself is not a claim — it names the subject. Twenty tests of this
    # exact shape fired before that tier was removed.
    'def test_blacklist_token_redis_unavailable_fails_secure(mock_redis_unavailable):\n'
    '    assert blacklist("t") is False\n',
    # One derived argument means the probe follows the stack under test: a subprocess on an
    # allocated port is legitimately on 127.0.0.1.
    'def _wait_until_serving(port):\n    return _reachable("127.0.0.1", port)\n',
    # The conftest shape after 7c989d51: probe the endpoint the suite will actually use.
    'PORT = int(os.environ.get("MINIO_PORT", "5178"))\n'
    'def _service_reachable():\n'
    '    return socket.create_connection(("localhost", PORT), timeout=1)\n',
    # A relative path against a fixture-provided client carries no endpoint at all, so a
    # readiness TEST is not a readiness probe with a hardcoded target.
    'def test_readiness_endpoint_reports_degraded(client):\n'
    '    r = client.get("/api/readiness")\n'
    '    assert r.json()["status"] == "degraded"\n',
)

#: The #400 suite's ACTUAL fixture, reduced. It is the regression guard for the defect that
#: made ``external-service-mock`` useless in practice: the stand-in is installed by a fixture
#: called ``fake_index``, which never names OpenSearch, so neither a patch-only scan of the
#: test body nor a look at the parameter name saw anything. Only resolving the fixture's own
#: body does. Before `_fixture_services` existed, the real file stayed clean even when
#: deliberately mislabelled — the detector fired on its own synthetic fixture and nothing else.
_FIXTURE_RESOLUTION_SOURCE = (
    '@pytest.fixture\n'
    'def fake_index(monkeypatch):\n'
    '    client = _FakeIndex()\n'
    "    monkeypatch.setattr(svc, 'opensearch_client', client)\n"
    '    return client\n'
    '\n'
    'def test_shrinking_rechunk_leaves_no_stale_tail(fake_index):\n'
    '    assert index_chunks(1, []) == 3\n'
)

#: Must-fire cases whose verdict depends on the MODULE PATH, which the fixtures above cannot
#: express (they are all scanned as ``fixture.py``). ``(category, path, source)``.
SELFTEST_PATH_CASES: tuple[tuple[str, str, str], ...] = (
    (
        # `tests/integration/` is a positional claim: the directory says real stack.
        'external-service-mock',
        'integration/test_chunk_reindex.py',
        'def test_stale_tail_is_pruned(fake_opensearch):\n'
        '    index_chunks(1, [])\n'
        '    assert fake_opensearch.deleted == ["chunk_index >= 3"]\n',
    ),
    (
        # The real shape, mislabelled by LOCATION. Verified against the actual #400 file.
        'external-service-mock',
        'integration/test_search_chunk_pruning.py',
        _FIXTURE_RESOLUTION_SOURCE,
    ),
    (
        # ...and the same body mislabelled by MARKER, which is the stronger claim: a
        # `-m integration` run selects it as real-engine coverage.
        'external-service-mock',
        'unit/test_search_chunk_pruning.py',
        'pytestmark = pytest.mark.integration\n' + _FIXTURE_RESOLUTION_SOURCE,
    ),
)

#: ``(path, source)`` pairs that must stay clean. The first is the second half of the
#: convention made executable: the SAME test body, moved to a directory that declares what it
#: is, is correctly labelled — which is the whole point of "different ids".
SELFTEST_PATH_CLEAN: tuple[tuple[str, str], ...] = (
    (
        'unit/test_chunk_reindex.py',
        'def test_stale_tail_is_pruned(fake_opensearch):\n'
        '    index_chunks(1, [])\n'
        '    assert fake_opensearch.deleted == ["chunk_index >= 3"]\n',
    ),
    # The #400 suite exactly as it really ships: a stand-in, honestly located, claiming
    # nothing. Fixture resolution must not turn "uses a fake" into a finding on its own —
    # 388 tests in this tree substitute a service and 264 of them are only visible through
    # this resolution, so a claim-side slip would report all of them.
    ('unit/test_search_chunk_pruning.py', _FIXTURE_RESOLUTION_SOURCE),
)

#: ``(category, path, source)`` where the category must fire **exactly once**.
#:
#: The fires/clean pair cannot express "reported twice". One defect reported under two names
#: costs two allowlist entries and reads as two problems, and `readiness-probe-target` did
#: exactly that: `scan_source` scans every function reached by ``ast.walk`` — methods and
#: nested functions included — and then scans module scope separately, so a probe inside a test
#: METHOD was reported once as the method and again as ``<module>``. Both a must-fire and a
#: must-stay-clean case pass happily while that is broken.
SELFTEST_ONCE: tuple[tuple[str, str, str], ...] = (
    (
        'readiness-probe-target',
        'test_stack.py',
        'class TestStack:\n'
        '    def test_the_api_is_up(self):\n'
        '        try:\n'
        '            r = requests.get("http://localhost:5174/api/health", timeout=1)\n'
        '        except requests.ConnectionError:\n'
        '            pytest.skip("dev stack not running")\n'
        '        assert r.json()["status"] == "ok"\n',
    ),
)


def run_selftest(verbose: bool = True) -> list[str]:
    """Return a list of failure descriptions — empty means every detector is alive."""
    failures: list[str] = []

    def check_fires(category: str, source: str, rel: str, label: str) -> None:
        got = {f.category for f in scan_source(source, rel)}
        if category not in got:
            failures.append(f'{label} did not fire (got {sorted(got) or "nothing"})')
        if verbose:
            mark = '\033[31m✗' if category not in got else '\033[32m✓'
            print(f'  {mark}\033[0m fires {label}')

    def check_clean(source: str, rel: str, label: str) -> None:
        found = scan_source(source, rel)
        if found:
            detail = ', '.join(f'{f.category}: {f.detail}' for f in found)
            failures.append(f'{label} produced {detail}')
        if verbose:
            mark = '\033[31m✗' if found else '\033[32m✓'
            print(f'  {mark}\033[0m {label} produces no finding')

    for category, source in SELFTEST_CASES:
        check_fires(category, source, 'fixture.py', category)
    # Path-dependent cases: `external-service-mock`'s weakest claim tier is POSITIONAL
    # (`tests/integration/`, `tests/e2e/`), which a fixture scanned as `fixture.py` cannot
    # express. Without these the tier is unreachable from the self-test and could rot silently.
    for category, rel, source in SELFTEST_PATH_CASES:
        check_fires(category, source, rel, f'{category} @ {rel}')
    for category, rel, source in SELFTEST_ONCE:
        hits = [f for f in scan_source(source, rel) if f.category == category]
        label = f'{category} @ {rel} fires exactly once'
        if len(hits) != 1:
            where = ', '.join(f'{f.test}:{f.line}' for f in hits) or 'nothing'
            failures.append(f'{label}: got {len(hits)} ({where})')
        if verbose:
            mark = '\033[31m✗' if len(hits) != 1 else '\033[32m✓'
            print(f'  {mark}\033[0m {label}')
    for i, source in enumerate(SELFTEST_CLEAN, start=1):
        check_clean(source, 'fixture.py', f'clean case {i}')
    for rel, source in SELFTEST_PATH_CLEAN:
        check_clean(source, rel, f'clean case @ {rel}')
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
    total = (
        len(SELFTEST_CASES)
        + len(SELFTEST_PATH_CASES)
        + len(SELFTEST_ONCE)
        + len(SELFTEST_CLEAN)
        + len(SELFTEST_PATH_CLEAN)
    )
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
