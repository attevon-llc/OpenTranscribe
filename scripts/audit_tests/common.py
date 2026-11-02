"""AST helpers shared by every detector."""

from __future__ import annotations

import ast


def is_fixture(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when decorated with ``@pytest.fixture`` (bare or called)."""
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and target.attr == 'fixture':
            return True
        if isinstance(target, ast.Name) and target.id == 'fixture':
            return True
    return False


def is_test_function(node: ast.AST) -> bool:
    """A collected test: named ``test_*`` and NOT a fixture that borrowed the prefix."""
    if not (
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith('test_')
    ):
        return False
    return not is_fixture(node)


def assert_nodes(fn: ast.AST) -> list[ast.Assert]:
    return [n for n in ast.walk(fn) if isinstance(n, ast.Assert)]


#: Assertion idioms other than a bare ``assert``. ``expect`` is Playwright's web-first
#: assertion and is the ONLY assertion in most E2E tests — omit it and a third of the
#: E2E suite reads as assertion-free, which is a detector bug, not a finding.
#: ``does_not_raise`` is ``tests/helpers.py``'s context manager, which calls ``pytest.fail``
#: with a reason when the block raises — so a test using it does assert something, and the
#: reason string is mandatory there precisely so it cannot become a silent pass.
ASSERTING_CALLS = frozenset({'expect', 'fail', 'raises', 'does_not_raise'})

#: ``unittest.mock`` assertion methods. These live ONLY on Mock objects, so matching the
#: method name is exact — no need to guess whether the receiver is a mock.
MOCK_ASSERT_METHODS = frozenset(
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
MOCK_INTROSPECTION = frozenset(
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
SWALLOW_CALLS = frozenset({'debug', 'info', 'warning', 'warn', 'error', 'exception', 'print'})

#: ``monkeypatch`` mutators, which the old ``patch``-only count could never see.
MONKEYPATCH_METHODS = frozenset(
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


def dotted(node: ast.AST) -> list[str]:
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


def asserting_calls(fn: ast.AST) -> list[ast.Call]:
    """Every call that acts as an assertion (``assert_*``, ``expect``, ``pytest.raises``)."""
    out: list[ast.Call] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        name = (
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, 'id', '')
        )
        if name.startswith('assert') or name in ASSERTING_CALLS:
            out.append(node)
    return out


def has_raises_or_helper(fn: ast.AST) -> bool:
    """True when the test asserts via ``pytest.raises``, ``expect()``, or ``assert_*``."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute) and node.attr in ASSERTING_CALLS:
            return True
    return bool(asserting_calls(fn))
