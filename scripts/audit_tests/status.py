"""Detectors for status-code assertions and conditionally-reached assertions."""

from __future__ import annotations

import ast

from audit_tests.common import assert_nodes

#: A status assertion listing this many alternatives is not asserting anything useful.
MAX_STATUS_ALTERNATIVES = 1


def _is_status_attr(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and 'status' in node.attr


def status_alternatives(fn: ast.AST) -> list[tuple[int, int]]:
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


def negated_status(fn: ast.AST) -> list[tuple[int, str]]:
    """(lineno, detail) for every ``assert response.status_code != X`` / ``not in (...)``.

    Restricted to expressions inside an ``assert``: ``if response.status_code != 200:`` is
    control flow, not a claim about the response.
    """
    out: list[tuple[int, str]] = []
    for assertion in assert_nodes(fn):
        for node in ast.walk(assertion):
            if not isinstance(node, ast.Compare) or not _is_status_attr(node.left):
                continue
            for op, comparator in zip(node.ops, node.comparators, strict=True):
                if isinstance(op, ast.NotEq):
                    out.append((node.lineno, f'!= {ast.unparse(comparator)}'))
                elif isinstance(op, ast.NotIn):
                    out.append((node.lineno, f'not in {ast.unparse(comparator)}'))
    return out


def status_guarded_asserts(fn: ast.AST) -> list[tuple[int, str, int]]:
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


def conditional_only(fn: ast.AST) -> bool:
    """True when every assertion is nested in an ``if`` that has no ``else``."""
    asserts = assert_nodes(fn)
    if not asserts:
        return False
    unguarded = {id(a) for a in asserts}
    for node in ast.walk(fn):
        if isinstance(node, ast.If) and not node.orelse:
            for inner in node.body:
                for descendant in ast.walk(inner):
                    unguarded.discard(id(descendant))
    return not unguarded


def conditional_skip(fn: ast.AST) -> str | None:
    """Return the guard when every assertion sits in an ``if`` whose ``else`` only skips.

    Sibling of ``conditional_only`` for the shape that detector deliberately
    excludes: an ``else`` branch exists, so the test is not *vacuous* — it is
    *skipped*. That reads as honest reporting but hides a permanent skip when the
    guard can never be true. ``test_tasks.py::test_get_task`` guarded on a task
    row that only Celery dispatch creates, which the autouse fixture patches out,
    so the only test of ``GET /tasks/{task_id}`` skipped every run for 11 months
    while the endpoint returned a hardcoded progress value (issue #431).
    """
    asserts = assert_nodes(fn)
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
