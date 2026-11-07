"""Static reachability from a callable to a Celery publish (issue #898).

Answers one question for :mod:`app.core.inert`: *can calling this function, through any chain
of calls into ``app.*`` code, reach a statement that enqueues background work?* The sinks are
the publish calls Celery documents — ``Task.delay``, ``Task.apply_async`` and
``Celery.send_task`` (Celery user guide, "Calling Tasks") — plus FastAPI/Starlette
``BackgroundTasks.add_task``, whose target runs in-process after the response. That one is
not a Celery publish, so the runtime counter cannot see it and this model is its only check.

How a call is followed: the function's own source is parsed with :mod:`ast` and every call is
resolved against the objects Python itself would use — the function's module globals, the
``import`` statements inside its body (this codebase imports lazily), and ``self``/``cls`` via
the enclosing class. Anything that resolves to a function or class defined under ``app.`` is
walked in turn. Callables passed positionally (``run_in_threadpool(fn)``) count as
called.

What it cannot see, so the result is a LOWER bound, never a proof of absence: calls through a
local variable (``svc = Foo(); svc.run()``), ``getattr`` dispatch, and anything a library calls
back into. The runtime publish counter in :mod:`app.core.inert` is the proof; this model is
what makes a crawl's zero meaningful by naming the routes that must stay zero.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import sys
import textwrap
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from types import FunctionType
from types import MethodType
from types import ModuleType
from typing import Any

#: Attribute calls that publish a Celery message.
PUBLISH_ATTRS = frozenset({"delay", "apply_async", "send_task"})

#: Attribute call that schedules in-process background work (Starlette ``BackgroundTasks``).
BACKGROUND_ATTR = "add_task"

SINK_ATTRS = PUBLISH_ATTRS | {BACKGROUND_ATTR}

#: Packages whose code is walked. Library code is never entered: its publishes, if any, are
#: reached through an ``app`` call site that the model already sees.
APP_PACKAGES: tuple[str, ...] = ("app",)


@dataclass(frozen=True)
class Sink:
    """One statement that starts background work."""

    call: str
    location: str


@dataclass(frozen=True)
class Witness:
    """The first call chain found from an entry point to a sink."""

    chain: tuple[str, ...]
    sink: Sink

    def render(self) -> str:
        """``a -> b -> c: x.delay() at path:line``."""
        return f"{' -> '.join(self.chain)}: {self.sink.call} at {self.sink.location}"


@dataclass
class ReachabilityModel:
    """Memoised call-graph walk; one instance per checker run."""

    packages: tuple[str, ...] = APP_PACKAGES
    unresolved_imports: set[str] = field(default_factory=set)
    _memo: dict[Any, Witness | None] = field(default_factory=dict)
    _active: set[Any] = field(default_factory=set)

    def witness(self, target: Any) -> Witness | None:
        """The first chain from ``target`` to a sink, or None if none is visible."""
        fn = self._as_function(target)
        if fn is None:
            return None
        key = fn.__code__
        if key in self._memo:
            return self._memo[key]
        if key in self._active:
            return None
        self._active.add(key)
        try:
            found = self._walk(fn)
        finally:
            self._active.discard(key)
        self._memo[key] = found
        return found

    def _owns(self, obj: Any) -> bool:
        return self._is_ours(getattr(obj, "__module__", None) or "")

    def _as_function(self, target: Any) -> FunctionType | None:
        if isinstance(target, MethodType):
            target = target.__func__
        if isinstance(target, (staticmethod, classmethod)):
            target = target.__func__
        if not callable(target):
            return None
        if not isinstance(target, (FunctionType, type)):
            # A callable instance (a FastAPI class-based dependency): its __call__ runs.
            target = type(target).__call__
        target = inspect.unwrap(target)
        if isinstance(target, FunctionType) and self._owns(target):
            return target
        return None

    def callees_of_class(self, cls: type) -> list[Any]:
        """What constructing or calling ``cls`` runs: its own ``__init__``/``__call__``/``dispatch``."""
        out = []
        for name in ("__init__", "__call__", "dispatch"):
            for klass in cls.__mro__:
                if name in vars(klass):
                    member = vars(klass)[name]
                    if self._owns(klass):
                        out.append(member)
                    break
        return out

    def _walk(self, fn: FunctionType) -> Witness | None:
        try:
            source = textwrap.dedent(inspect.getsource(fn))
            tree = ast.parse(source)
        except (OSError, TypeError, SyntaxError):
            return None
        filename = inspect.getsourcefile(fn) or "?"
        offset = fn.__code__.co_firstlineno - 1
        label = f"{fn.__module__}.{fn.__qualname__}"
        scope = _Scope(fn, self._local_imports(tree, fn))

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in SINK_ATTRS:
                sink = Sink(
                    call=f"{ast.unparse(func)}()",
                    location=f"{_short_path(filename)}:{node.lineno + offset}",
                )
                return Witness(chain=(label,), sink=sink)
            candidates = [scope.resolve(func)]
            candidates += [scope.resolve(arg) for arg in node.args]
            for obj in candidates:
                for callee in self._expand(obj):
                    found = self.witness(callee)
                    if found is not None:
                        return Witness(chain=(label, *found.chain), sink=found.sink)
        return None

    def _expand(self, obj: Any) -> list[Any]:
        if obj is None:
            return []
        if isinstance(obj, type):
            return self.callees_of_class(obj) if self._owns(obj) else []
        return [obj]

    def _local_imports(self, tree: ast.AST, fn: FunctionType) -> dict[str, Any]:
        names: dict[str, Any] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                base = _absolute_module(node, fn.__module__)
                if base is None or not self._is_ours(base):
                    continue
                for alias in node.names:
                    obj = self._import_attr(base, alias.name)
                    if obj is not None:
                        names[alias.asname or alias.name] = obj
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname and self._is_ours(alias.name):
                        module = self._import(alias.name)
                        if module is not None:
                            names[alias.asname] = module
        return names

    def _is_ours(self, module: str) -> bool:
        return any(module == p or module.startswith(p + ".") for p in self.packages)

    def _import(self, module: str) -> ModuleType | None:
        try:
            return importlib.import_module(module)
        except ImportError:  # an unimportable module is a model gap, recorded and reported
            self.unresolved_imports.add(module)
            return None

    def _import_attr(self, module: str, name: str) -> Any:
        mod = self._import(module)
        if mod is None:
            return None
        if hasattr(mod, name):
            return getattr(mod, name)
        return self._import(f"{module}.{name}")


class _Scope:
    """Resolves a call expression inside one function to the object it names."""

    def __init__(self, fn: FunctionType, local_imports: dict[str, Any]) -> None:
        self._fn = fn
        self._locals = local_imports

    def resolve(self, expr: ast.expr) -> Any:
        if isinstance(expr, ast.Name):
            return self._name(expr.id)
        if isinstance(expr, ast.Attribute):
            if isinstance(expr.value, ast.Name) and expr.value.id in ("self", "cls"):
                owner = self._enclosing_class()
                return getattr(owner, expr.attr, None) if owner is not None else None
            base = self.resolve(expr.value)
            if isinstance(base, (ModuleType, type)):
                return inspect.getattr_static(base, expr.attr, None)
        return None

    def _name(self, name: str) -> Any:
        if name in self._locals:
            return self._locals[name]
        return self._fn.__globals__.get(name)

    def _enclosing_class(self) -> type | None:
        parts = self._fn.__qualname__.split(".")
        if len(parts) < 2 or "<locals>" in parts:
            return None
        obj: Any = self._fn.__globals__.get(parts[0])
        for part in parts[1:-1]:
            obj = getattr(obj, part, None)
        return obj if isinstance(obj, type) else None


def _absolute_module(node: ast.ImportFrom, current: str) -> str | None:
    if node.level == 0:
        return node.module
    # A package's own __init__ is level-1 relative to itself, a plain module to its parent.
    is_package = hasattr(sys.modules.get(current), "__path__")
    parts = current.split(".")
    keep = len(parts) - node.level + (1 if is_package else 0)
    if keep <= 0:
        return None
    package = ".".join(parts[:keep])
    return f"{package}.{node.module}" if node.module else package


#: ``backend/`` (or ``/app`` in the image): sink locations are reported relative to it.
_SOURCE_ROOT = str(Path(__file__).resolve().parents[2]) + "/"


def _short_path(filename: str) -> str:
    return filename[len(_SOURCE_ROOT) :] if filename.startswith(_SOURCE_ROOT) else filename
