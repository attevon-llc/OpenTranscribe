"""Detectors for tests whose assertions cannot fail (loops, constants, mocks, masking)."""

from __future__ import annotations

import ast
from collections import Counter

from audit_tests.common import (
    MOCK_ASSERT_METHODS,
    MOCK_INTROSPECTION,
    SWALLOW_CALLS,
    assert_nodes,
    asserting_calls,
)


def single_assignments(statements: list[ast.stmt]) -> dict[str, ast.expr]:
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


def loop_only(fn: ast.AST, module_consts: dict[str, ast.expr] | None = None) -> str | None:
    """Return the dynamic iterable when every assertion is inside a ``for`` over one."""
    asserts = assert_nodes(fn)
    if not asserts:
        return None
    consts = dict(module_consts or {})
    consts.update(single_assignments(list(getattr(fn, 'body', []))))
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


def unfalsifiable(fn: ast.AST) -> list[tuple[int, str]]:
    """(lineno, source) for each assertion whose every operand is a constant."""
    out: list[tuple[int, str]] = []
    for assertion in assert_nodes(fn):
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


def weak_only(fn: ast.AST) -> str | None:
    """Return the weak expressions when the test has nothing stronger."""
    asserts = assert_nodes(fn)
    if not asserts or asserting_calls(fn):
        return None
    weak = [a for a in asserts if _is_weak_test(a.test)]
    if len(weak) != len(asserts):
        return None
    return ', '.join(sorted({ast.unparse(a.test) for a in weak}))[:120]


def _reads_mock_internals(node: ast.AST) -> bool:
    return any(
        isinstance(inner, ast.Attribute) and inner.attr in MOCK_INTROSPECTION
        for inner in ast.walk(node)
    )


def mock_only(fn: ast.AST) -> int | None:
    """Return the assertion count when every assertion is mock bookkeeping."""
    bookkeeping = 0
    real = 0
    for assertion in assert_nodes(fn):
        if _reads_mock_internals(assertion.test):
            bookkeeping += 1
        else:
            real += 1
    for call in asserting_calls(fn):
        if isinstance(call.func, ast.Attribute) and call.func.attr in MOCK_ASSERT_METHODS:
            bookkeeping += 1
        else:
            real += 1
    if bookkeeping and not real:
        return bookkeeping
    return None


def masks_failure(fn: ast.AST) -> str | None:
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


def swallows_error(fn: ast.AST) -> str | None:
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
            if name in SWALLOW_CALLS:
                continue
        return False
    return bool(body)
