"""Broad-raises, bare-return, self-comparison, sleep-sync and skip-marker detectors."""

from __future__ import annotations

import ast

from audit_tests.common import ASSERTING_CALLS, assert_nodes, asserting_calls, dotted

# ----------------------------------------------------------- over-broad exception assertions

#: Exception types that assert "something went wrong" and nothing more.
_BROAD_EXCEPTIONS = frozenset({'Exception', 'BaseException'})


def broad_raises(fn: ast.AST) -> list[tuple[int, str]]:
    """``(lineno, caught)`` for each ``pytest.raises(Exception)`` with no ``match=``.

    The block is not a claim about the code under test — it is a claim that the block raised.
    Everything inside it counts, so a renamed keyword argument, a fixture that stopped
    supplying an attribute, or a DB error at ``commit()`` all satisfy the assertion and read
    as the refusal the test says it proves.

    ``match=`` is the escape hatch and it is a real one: pinning the message narrows the
    assertion to a specific failure. A trailing ``as exc`` is NOT, even when an assertion on
    ``exc.value`` follows — ``assert getattr(exc.value, "status_code", None) == 403`` is
    satisfied by any exception carrying that attribute, including one raised while building
    the call's arguments.
    """
    out: list[tuple[int, str]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        name = (
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, 'id', '')
        )
        if name != 'raises' or not node.args:
            continue
        first = node.args[0]
        caught = first.elts if isinstance(first, ast.Tuple) else [first]
        if not all(isinstance(c, ast.Name) and c.id in _BROAD_EXCEPTIONS for c in caught):
            continue
        if any(k.arg == 'match' for k in node.keywords):
            continue
        out.append((node.lineno, ast.unparse(first)))
    return out


# ------------------------------------------------------------------------ bare early return

#: Calls that REPORT a bail-out. A ``return`` after one of these is bookkeeping (usually
#: ``return  # unreachable, satisfies mypy``), not a silent green.
_REPORTING_CALLS = ASSERTING_CALLS | frozenset({'skip', 'xfail', 'exit'})


def _inner_function_node_ids(scope: ast.AST) -> set[int]:
    """Ids of every node that lives inside a function defined within ``scope``.

    The generalisation of :func:`_nested_call_ids`. A ``return`` inside a helper closure
    defined in a test is that helper's control flow, not the test bailing out.
    """
    out: set[int] = set()
    for node in ast.walk(scope):
        if node is scope or not isinstance(
            node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda
        ):
            continue
        for inner in ast.walk(node):
            out.add(id(inner))
    return out


def _expression_evidence(node: ast.AST) -> bool:
    """True when an EXPRESSION contains an assertion or a reported bail-out.

    Expression-level only: ``ast.walk`` over a statement would descend into nested ``if`` and
    ``for`` bodies, and evidence inside a branch that may not run is not evidence.
    """
    for inner in ast.walk(node):
        if isinstance(inner, ast.Assert):
            return True
        if isinstance(inner, ast.Call):
            name = (
                inner.func.attr
                if isinstance(inner.func, ast.Attribute)
                else getattr(inner.func, 'id', '')
            )
            if name.startswith('assert') or name in _REPORTING_CALLS:
                return True
    return False


def _unconditional_evidence(statement: ast.stmt) -> bool:
    """True when running ``statement`` necessarily asserts or reports something.

    A branching statement is worth nothing here even if it contains an assertion: that is the
    whole point of ``conditional-only``. The distinction is what makes this detector see the
    live instance — ``test_throughput_benchmark`` opens with ``if not completed_files:
    pytest.skip(...)``, so a "was anything reported earlier in the function?" reading excused
    the *unrelated* bare return 60 lines later and the detector found nothing at all.
    """
    if isinstance(statement, ast.Assert):
        return True
    if isinstance(
        statement,
        ast.If
        | ast.For
        | ast.AsyncFor
        | ast.While
        | ast.FunctionDef
        | ast.AsyncFunctionDef
        | ast.ClassDef
        | ast.Match,
    ):
        return False
    if isinstance(statement, ast.With | ast.AsyncWith):
        if any(_expression_evidence(item.context_expr) for item in statement.items):
            return True
        return any(_unconditional_evidence(s) for s in statement.body)
    if isinstance(statement, ast.Try):
        return any(_unconditional_evidence(s) for s in statement.body)
    return _expression_evidence(statement)


def bare_returns(fn: ast.AST) -> list[int]:
    """Linenos of ``return`` statements reached before the test asserted or reported anything.

    Evidence counts when it is on the return's OWN path: an unconditional statement earlier in
    the return's block, or earlier in any block enclosing it. Evidence in a sibling branch does
    not count, and neither does an assertion after the return — a test that exits early having
    proved nothing is green whatever the rest of the body does.

    Calibrated against every bare return in this tree. Four of the five are legitimate and
    firing on them would have made the detector noise:

    * ``e2e/test_chat.py`` and ``unit/test_auth_config_has_readers.py`` assert in the same
      branch, then return — the rest of the test is genuinely conditional;
    * ``e2e/test_mfa.py`` asserts twice inside the ``except`` handler it returns from;
    * ``e2e/test_gallery_actions.py`` calls ``pytest.skip`` and then returns for mypy's benefit.

    The fifth — ``test_embedding_migration_integration.py`` — prints "No files were prepared
    successfully." and returns, with nothing asserted and nothing reported. A run where the
    setup produced nothing is then indistinguishable from a run that passed.
    """
    out: list[int] = []

    def walk_block(statements: list[ast.stmt], seen: bool) -> None:
        for statement in statements:
            if isinstance(statement, ast.Return) and statement.value is None and not seen:
                out.append(statement.lineno)
            if not isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                for block in _statement_lists(statement):
                    walk_block(block, seen)
            if _unconditional_evidence(statement):
                seen = True

    walk_block(list(getattr(fn, 'body', [])), False)
    return sorted(set(out))


# ------------------------------------------------------------------------- self-comparison


def self_comparison(fn: ast.AST) -> tuple[int, str] | None:
    """``(lineno, expression)`` when the test's SOLE assertion compares something to itself.

    ``assert _key_with_corpus("7") == _key_with_corpus("7")`` is green whether or not the key
    depends on the corpus version at all, which is the one thing the test is named for.

    "Sole" is load-bearing. The same file's ``test_scope_hash_is_order_independent`` asserts
    ``scope_hash(None) == scope_hash(None)`` beside ``scope_hash(["b","a"]) ==
    scope_hash(["a","b"])`` and ``scope_hash(None) != scope_hash(["a"])`` — there it pins
    determinism of a value the other two assertions have already constrained, and firing on it
    would teach people to allowlist.
    """
    asserts = assert_nodes(fn)
    if len(asserts) != 1 or asserting_calls(fn):
        return None
    test = asserts[0].test
    if not isinstance(test, ast.Compare) or len(test.comparators) != 1:
        return None
    if not isinstance(test.ops[0], ast.Eq | ast.Is):
        return None
    left = ast.unparse(test.left)
    if left != ast.unparse(test.comparators[0]):
        return None
    return asserts[0].lineno, left


# --------------------------------------------------------------- sleep as synchronisation


def _is_sleep_call(node: ast.Call) -> bool:
    """``time.sleep(...)`` / ``asyncio.sleep(...)`` / a bare imported ``sleep(...)``.

    Playwright's ``page.wait_for_timeout`` is deliberately NOT here. It is the e2e suite's
    whole waiting idiom (~70 sites), it precedes ``expect()`` assertions that auto-retry, and
    reporting it would produce a 70-entry allowlist nobody reads — the "a detector that fires
    on correct code teaches people to allowlist" failure this file already learned once.

    ``asyncio.sleep(0)`` is exempt for the same reason, and it is not a judgement call: a
    ZERO-duration sleep is the documented way to yield to the event loop once, so it bounds
    nothing and cannot be the "hope it is ready by now" bet this detector exists to catch. It
    is also unavoidable — ``loop.call_soon_threadsafe`` SCHEDULES a callback rather than
    running it, so any test that asserts on the result must let the loop turn first. Flagging
    it would put a permanent allowlist entry on every async test in the repo, which is exactly
    how a detector stops being read.
    """
    parts = dotted(node.func)
    if not parts or parts[-1] != 'sleep':
        return False
    if not (len(parts) == 1 or parts[0] in ('time', 'asyncio')):
        return False
    return not _is_zero_duration(node)


def _is_zero_duration(node: ast.Call) -> bool:
    """``sleep(0)`` / ``sleep(0.0)`` — a scheduling yield, not a wait."""
    if len(node.args) != 1 or node.keywords:
        return False
    arg = node.args[0]
    return (
        isinstance(arg, ast.Constant) and arg.value in (0, 0.0) and not isinstance(arg.value, bool)
    )


def _statement_lists(node: ast.AST) -> list[list[ast.stmt]]:
    """Every statement list under ``node``, innermost blocks included."""
    out: list[list[ast.stmt]] = []
    for field in ('body', 'orelse', 'finalbody'):
        block = getattr(node, field, None)
        if not isinstance(block, list) or not all(isinstance(s, ast.stmt) for s in block):
            continue
        out.append(block)
        for statement in block:
            out.extend(_statement_lists(statement))
    for handler in getattr(node, 'handlers', []) or []:
        out.extend(_statement_lists(handler))
    return out


def _direct_sleep_calls(statements: list[ast.stmt]) -> list[ast.Call]:
    """Sleep calls written as a statement DIRECTLY in this block (not in a nested one)."""
    out: list[ast.Call] = []
    for statement in statements:
        if not isinstance(statement, ast.Expr):
            continue
        value = statement.value
        if isinstance(value, ast.Await):
            value = value.value
        if isinstance(value, ast.Call) and _is_sleep_call(value):
            out.append(value)
    return out


def _poll_interval_sleeps(fn: ast.AST) -> set[int]:
    """Ids of sleeps that are a poll INTERVAL in a bounded loop, so not a synchronisation bet.

    A loop that re-checks a condition and can exit early already asserts readiness; the sleep
    only decides how often it asks, so a wrong duration costs time and never correctness. That
    is eleven of the sixteen blocking sleeps in this tree — auth retry/backoff, status polling,
    ``_wait_for(predicate)`` helpers.

    The exception, and the reason this looks at the sleep's OWN block rather than "is it
    somewhere in a loop": ``if _is_port_open(...): time.sleep(5); break``. That sleep is inside
    the loop but on the branch that *leaves* it — the poll already succeeded, and the 5 s is a
    bare bet that Keycloak finishes booting. A direct ``break``/``return`` in the sleep's own
    block therefore disqualifies it.
    """
    out: set[int] = set()
    nested = _inner_function_node_ids(fn)
    for loop in ast.walk(fn):
        if not isinstance(loop, ast.For | ast.AsyncFor | ast.While) or id(loop) in nested:
            continue
        has_exit = any(
            isinstance(inner, ast.Break | ast.Return)
            for statement in loop.body
            for inner in ast.walk(statement)
        )
        if not has_exit:
            continue
        for block in _statement_lists(loop):
            if any(isinstance(s, ast.Break | ast.Return) for s in block):
                continue
            for call in _direct_sleep_calls(block):
                out.add(id(call))
    return out


def sleep_syncs(fn: ast.AST) -> list[tuple[int, str]]:
    """``(lineno, source)`` for each blocking sleep that is not a poll interval."""
    exempt = _poll_interval_sleeps(fn)
    nested = _inner_function_node_ids(fn)
    return [
        (node.lineno, ast.unparse(node))
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and _is_sleep_call(node)
        and id(node) not in exempt
        and id(node) not in nested
    ]


# ------------------------------------------------------------------------- disabled tests


def skip_markers(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[int, str]]:
    """``(lineno, marker)`` for decorators that permanently disable the test.

    ``skipif`` is deliberately absent: a conditional gate is how this repo selects the
    ``RUN_*`` suites, and it is visible in the run. ``xfail(strict=True)`` is absent for the
    opposite reason — it FAILS when the test starts passing, so it cannot outlive its subject.
    A non-strict ``xfail`` can, and silently.
    """
    out: list[tuple[int, str]] = []
    for dec in fn.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        parts = dotted(target)
        if 'mark' not in parts:
            continue
        marker = parts[-1]
        if marker == 'skip':
            out.append((dec.lineno, 'pytest.mark.skip — never runs, never reports'))
        elif marker == 'xfail':
            strict = isinstance(dec, ast.Call) and any(
                k.arg == 'strict' and isinstance(k.value, ast.Constant) and k.value.value is True
                for k in dec.keywords
            )
            if not strict:
                out.append(
                    (dec.lineno, 'pytest.mark.xfail without strict=True — also passes once fixed')
                )
    return out
