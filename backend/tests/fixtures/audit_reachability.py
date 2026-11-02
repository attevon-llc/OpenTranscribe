"""Which audit events can a route (or a dependency, or a task) reach? A static answer.

Used by ``unit/test_compliance_audit_route_coverage.py``. The walk starts at a callable,
parses its body, resolves every called name through the function's globals (plus the
imports made inside the body, which this codebase uses heavily to keep modules light),
follows ``task.delay``/``task.apply_async`` into the task body, and collects every
``AuditEventType.<MEMBER>`` named in the source of anything reached. Only ``app.*``
code is followed.

It is an over- and under-approximation at once, and the test that uses it is shaped for
that: a route is only REQUIRED to reach the event it is declared to emit (so a deleted
emitter fails), and the reverse direction (a route that reaches a compliance event must be
declared) tolerates over-reach by letting the declaration say ``NOT_AUDITED`` with a reason.
Function signatures are not walked: ``Depends(get_current_user)`` defaults would otherwise
pull every authentication event into every route.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import re
import textwrap
from collections.abc import Callable
from collections.abc import Iterator
from typing import Any

EVENT_RE = re.compile(r"AuditEventType\.([A-Z_]+)")
_DISPATCH_ATTRS = {"delay", "apply_async", "s", "si"}


def _members() -> frozenset[str]:
    from app.auth.audit import AuditEventType

    return frozenset(AuditEventType.__members__)


_MEMBERS = _members()
_memo: dict[int, tuple[set[str], list[Any]]] = {}


def _resolve(node: ast.AST, names: dict[str, Any]) -> Any:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    obj = names.get(node.id)
    for part in reversed(parts):
        if obj is None:
            return None
        obj = getattr(obj, part, None)
    return obj


def _local_imports(tree: ast.AST, names: dict[str, Any]) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if not node.module.startswith("app"):
                continue
            for alias in node.names:
                try:
                    module = importlib.import_module(node.module)
                    value = getattr(module, alias.name, None)
                    if value is None:
                        value = importlib.import_module(f"{node.module}.{alias.name}")
                except ImportError:
                    continue
                names[alias.asname or alias.name] = value


def _target(fn: Any) -> Callable[..., Any] | None:
    if hasattr(fn, "delay") and hasattr(fn, "run"):  # a Celery task: follow its body
        fn = fn.run
    fn = inspect.unwrap(fn) if callable(fn) else fn
    if not (inspect.isfunction(fn) or inspect.ismethod(fn)):
        return None
    if not (getattr(fn, "__module__", "") or "").startswith("app."):
        return None
    return fn


def _scan(fn: Callable[..., Any]) -> tuple[set[str], list[Any]]:
    key = id(fn)
    if key in _memo:
        return _memo[key]
    try:
        source = textwrap.dedent(inspect.getsource(fn))
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError):
        _memo[key] = (set(), [])
        return _memo[key]
    top = tree.body[0] if tree.body else tree
    body = list(getattr(top, "body", []))
    names = dict(getattr(fn, "__globals__", {}))
    for stmt in body:
        _local_imports(stmt, names)
    events: set[str] = set()
    callees: list[Any] = []
    for stmt in body:
        events |= set(EVENT_RE.findall(ast.unparse(stmt)))
        for node in ast.walk(stmt):
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
                # ``_EVENT_FOR_TARGET[target]``: a module-level mapping picks the event. Only a
                # subscripted mapping counts; a set of event types is a filter, not an emit.
                table = names.get(node.value.id)
                if isinstance(table, dict):
                    events |= {getattr(v, "name", "") for v in table.values()} & _MEMBERS
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in _DISPATCH_ATTRS:
                func = func.value
            for candidate in [func, *node.args, *(k.value for k in node.keywords)]:
                if isinstance(candidate, (ast.Name, ast.Attribute)):
                    obj = _resolve(candidate, names)
                    if obj is not None and callable(obj):
                        callees.append(obj)
    _memo[key] = (events, callees)
    return _memo[key]


def reachable_events(start: Any, *, depth: int = 7) -> set[str]:
    """Every ``AuditEventType`` member named in code reachable from ``start``."""
    seen: set[int] = set()
    found: set[str] = set()
    stack: list[tuple[Any, int]] = [(start, 0)]
    while stack:
        obj, level = stack.pop()
        fn = _target(obj)
        if fn is None or id(fn) in seen:
            continue
        seen.add(id(fn))
        events, callees = _scan(fn)
        found |= events
        if level < depth:
            stack.extend((callee, level + 1) for callee in callees)
    return found


def dependency_calls(dependant: Any) -> Iterator[Any]:
    """Every dependency callable of a FastAPI ``Dependant``, recursively."""
    for sub in dependant.dependencies:
        if sub.call is not None:
            yield sub.call
        yield from dependency_calls(sub)


def route_events(route: Any, *, include_dependencies: bool = False) -> set[str]:
    """Events reachable from a route's endpoint (and, optionally, its dependencies)."""
    found = reachable_events(route.endpoint)
    if include_dependencies:
        for call in dependency_calls(route.dependant):
            found |= reachable_events(call)
    return found
