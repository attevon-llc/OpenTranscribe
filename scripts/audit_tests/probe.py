"""Detector for readiness probes aimed at a hardcoded endpoint."""

from __future__ import annotations

import ast
import re

from audit_tests.patching import words
from audit_tests.vacuous import single_assignments

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
    return bool(words(name) & _PROBE_WORDS) or 'is_up' in name.lower()


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


def readiness_probe_targets(
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
    consts.update(single_assignments(body))
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
