"""Exception text must never be echoed verbatim into an HTTP response (#859, #891).

``str(e)`` (or anything derived from it) landing in an ``HTTPException`` detail, an
``ErrorHandler.*`` builder, or a ``raise <OpenTranscribeError subclass>(...)`` message
exposes internal details — file paths, DB connection strings, broker URLs with
embedded credentials, SQL fragments — to any caller who triggers the error. #859
originally flagged the API/endpoint layer; #891 is the identical defect one layer
down in the SERVICE layer, which #859's own remediation explicitly excluded.

Four independent, AST-based scanners live here, all allowlist-gated with a mandatory
written reason, matching this repo's ``test_ws_event_quarantine_discipline.py`` /
``test_ddl_marker_discipline.py`` pattern.

**Scanner 1 (raise-path) scans the WHOLE ``app/`` tree, not just ``app/api/``.**
This is the critical detail #891 is about: a raise made in ``app/services/``
propagates up and is rendered by ``main.py``'s ``OpenTranscribeError`` handler
regardless of which layer raised it (that handler renders ``exc.message`` verbatim
as the response ``detail`` — it is a RENDERER, not a sanitizer). Scoping the scan to
``app/api/`` only would silently miss the service layer forever, which is exactly
what let ``services/search/model_switch.py::dispatch_reindex_for_every_owner`` leak
a Redis broker URL (with an embedded password) into a 503 response for months.
``test_the_scan_root_includes_the_service_layer`` pins this: it fails loudly if
anyone ever "simplifies" the scan root back down to ``app/api/``.

**Scanner 2 (return-path) scans ``app/api/`` ONLY.** A value merely *returned* from a
service function isn't necessarily response-bound — many service functions return
status tuples/dicts used for internal logic, not just API responses — so only the
endpoint layer, which IS necessarily response-bound (a FastAPI handler's return value
IS the response body), is scanned for this construct.

**Scanner 3 (service return-path) extends the SAME construct to ``app/services/``**
(#914 STEP 8) — the residual scanner 2's docstring used to describe as "not covered
here," measured then at 52 findings / 44 function keys / 34 files (#914's own "~60"
was an overcount). Nearly all of those were real, fixable exception-echo sites and
were fixed at the source (narrowed to ``type(exc).__name__``, upgraded the
accompanying log call to ``logger.exception``) rather than allowlisted — see the
#914 fix commits. What is allowlisted below is deliberately narrow: a genuine dial
result an admin/user explicitly requested (§2b), or a value proven by hand-tracing
every consumer to never reach an HTTP response body (§2c). Measured post-fix: 25
findings / 22 function keys, every one accounted for in ``_SERVICE_RETURN_ALLOWLIST``.
A service function's return value is not *automatically* response-bound the way an
endpoint's is, so this scanner is necessarily narrower in what it can assert about a
finding — hence the allowlist reasons here lean on traced consumers rather than the
"this IS the response" shortcut scanner 2's dial-result entries use.

Taint tracking (shared by all three scanners): the exception variable bound in
``except ... as e``, plus every local variable assigned from it — **including
through a function call** (``safe = clean(str(e))`` must still taint ``safe``).
There is deliberately NO trusted-sanitizer-name escape hatch: the investigation
found a function literally named ``create_user_friendly_error``
(``services/media_download_service.py``) that is NOT actually a sanitizer — it only
strips four literal prefixes on the non-auth-error branch. A name-based escape
hatch would hide exactly this class of false confidence, so the scanner never looks
at a callee's name, only at whether an expression's AST subtree references a
tainted variable.

**Scanner 4 (CONTAINER return-path) closes the blind spot the first three share.**
Scanners 1-3 track taint through *assignment* and check it at ``visit_Return``, so they
see ``return {"error": str(e)}`` and are blind to ``failures.append({"error": str(e)})``
followed by ``return failures`` several lines later. That is not hypothetical: it is how
``api/endpoints/tasks.py::recover_all_stuck_tasks`` returned the raw Celery re-dispatch
exception — which on the broker path names the transport URL and its embedded password —
past a gate written to stop exactly that. Scanner 4 tracks a tainted value into a
CONTAINER (``.append``/``.extend``/``.add``/``.update``/``.insert``/``.setdefault``, a
subscript or attribute assignment, or ``+=``) whose container is later returned, and
reports **the mutation's** line — the leak, not the return. Two consequences, both
deliberate:

* It collects returns and checks them when the function CLOSES rather than at
  ``visit_Return``, because the mutation is frequently BELOW the return in source order
  (``gdpr_erasure_service.py::erase_user`` returns at :457 and mutates at :498).
* The finding count is per WRITE, not per return statement. Keying on the return would
  collapse every leak in one function into a single finding, so a new leak added beside
  an already-allowlisted one could never exceed its allowlisted count — which is exactly
  what ``test_a_container_return_site_may_not_exceed_its_allowlisted_count`` exists to
  catch. ``bulk_file_action`` is the live example: two tainted writes into one returned
  list, one legitimate and one a real leak, behind a single ``return results``.

Two scoping decisions, both measured rather than assumed:

* **No transitive propagation.** An earlier draft also treated ``y = f(container)`` as
  container-tainted. That produced three false-positive keys in ``app/tasks/`` where an
  ordinary ``file_id = int(media_file.id)`` inherited taint through an unrelated chain
  (``rediarize_task``, ``process_youtube_url_task``, ``dispatch_batch_transcription``).
  Direct mutation only: measured **zero** false positives across the whole tree.
* **Rooted at ``app/api/`` + ``app/services/``, not ``app/tasks/``.** A Celery task's
  return value goes to the result backend, and this application reads it back in exactly
  two places — ``app/utils/task_utils.py:461`` and ``:670`` — both of which read
  ``.state`` and never ``.result``. So a task return is not a response body here.
  ``test_a_celery_result_is_never_read_into_a_response`` pins that premise mechanically,
  so the scope stays honest if someone ever wires a task result into an endpoint.

**The one structural exception is ``type(<anything>).__name__``.** This is not a
callee-name check (the paragraph above still holds): the scanner recognises one AST
shape — an ``Attribute`` named ``__name__`` whose value is a ``Call`` to a bare
``type`` — that is structurally guaranteed to reduce to a class name and never the
exception's message text, regardless of what expression sits inside ``type(...)``.
This exists because the scanner used to fire on the repo's own approved remedy for
this class of finding (``f"... ({type(e).__name__})"``, precedented at
``llm_context_window.py:249`` and ``fs_events/detection.py:200``) — a gate that
flags its own prescribed fix is a gate people learn to route around. It is narrow
on purpose: ``f"failed ({type(e).__name__})"`` alone does not taint, but
``f"failed ({type(e).__name__}): {e}"`` still does, because the bare ``e`` sitting
outside the ``type(...).__name__`` shape is walked normally.

**Scanner 4 adds a second such shape, ``len(<anything>)``**, for the same reason and
under the same rule (a structural shape, never a callee name). Reporting a per-item
failure COUNT — ``return {"failed": len(failures)}`` — is the textbook safe way to
consume a container the handler filled with exception text, and a gate that flagged it
would fire on its own prescribed alternative. It is applied to scanner 4 only, via
``_references_tainted``'s ``prune`` parameter, so scanners 1-3's measured finding counts
and their allowlists are untouched.
"""

from __future__ import annotations

import ast
import functools
from collections.abc import Callable
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]
_APP_ROOT = _TESTS_ROOT.parent / "app"
_API_ROOT = _APP_ROOT / "api"
_SERVICES_ROOT = _APP_ROOT / "services"

#: The names ErrorHandler exposes. Any of these called as ``ErrorHandler.<name>(...)``
#: is in scope; the scanner does not hardcode this list for matching (it matches any
#: attribute access on an object literally named ``ErrorHandler``), it exists only for
#: documentation.
_ERROR_HANDLER_OBJECT_NAME = "ErrorHandler"


def _iter_py_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def _rel_to_app(path: Path) -> str:
    return path.relative_to(_APP_ROOT).as_posix()


def _is_class_name_only(node: ast.AST) -> bool:
    """True for the ``type(<anything>).__name__`` shape — carries no message text.

    Not a name-based sanitizer escape hatch (the module docstring forbids those):
    this matches one specific AST shape that is structurally guaranteed to reduce
    to a class name, never the exception's message text, no matter what expression
    sits inside the ``type(...)`` call. See the module docstring for why this
    exists (the scanner used to fire on the repo's own approved remedy).
    """
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "__name__"
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "type"
    )


def _is_length_only(node: ast.AST) -> bool:
    """True for a bare ``len(<anything>)`` call — reduces to an int, never text.

    The scanner-4 counterpart of ``_is_class_name_only``, and justified the same way:
    one AST shape that is structurally guaranteed to carry no message text no matter
    what sits inside it. It exists because reporting a per-item failure COUNT
    (``return {"failed": len(failures)}``) is the textbook safe consumption of a
    container the handler filled with exception text — flagging it would make the
    gate fire on the very thing it wants people to do instead. Narrow on purpose:
    ``len(failures)`` alone does not taint, but ``{"n": len(failures), "detail":
    failures}`` still does, because the bare reference outside the ``len(...)``
    subtree is walked normally.
    """
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len"


def _references_tainted(
    expr: ast.AST | None,
    tainted: frozenset[str],
    prune: Callable[[ast.AST], bool] = _is_class_name_only,
) -> bool:
    """True if any ``Name`` node inside *expr* is a member of *tainted*.

    Deliberately structural and name-blind: no allowance is made for a callee
    that "looks like" a sanitizer. See the module docstring. The single exception
    is *prune*: when a subtree matches it, its children (including whatever
    exception variable sits inside) are never visited, because that shape cannot
    carry message text regardless of contents. It defaults to
    ``_is_class_name_only``; scanner 4 passes a predicate that also prunes
    ``len(...)``. A *name*-based escape hatch remains forbidden — every prune here
    is a structural shape, never a callee identity.
    """
    if expr is None or not tainted:
        return False
    stack: list[ast.AST] = [expr]
    while stack:
        node = stack.pop()
        if prune(node):
            continue
        if isinstance(node, ast.Name) and node.id in tainted:
            return True
        stack.extend(ast.iter_child_nodes(node))
    return False


def _is_class_name_or_length_only(node: ast.AST) -> bool:
    """Scanner 4's prune: both structurally text-free shapes."""
    return _is_class_name_only(node) or _is_length_only(node)


def _taint_from_except_handler(
    node: ast.ExceptHandler, inherited: frozenset[str]
) -> frozenset[str]:
    """Fixed-point taint set visible inside one ``except`` handler's body.

    Seeds with the handler's own bound name (``except ... as e``) plus whatever was
    already tainted in an enclosing scope (a nested try/except inside an except
    suite keeps the outer name visible), then repeatedly walks every assignment in
    the handler's body until no new names are added. Handles ``Assign``,
    ``AnnAssign`` and the walrus operator; tuple-target assignments are not
    unpacked (not needed by any real call site found in this codebase).
    """
    tainted: set[str] = set(inherited)
    if node.name:
        tainted.add(node.name)

    changed = True
    while changed:
        changed = False
        for n in ast.walk(node):
            value: ast.AST | None = None
            target: ast.AST | None = None
            if isinstance(n, ast.Assign) and len(n.targets) == 1:
                target, value = n.targets[0], n.value
            elif (isinstance(n, ast.AnnAssign) and n.value is not None) or isinstance(
                n, ast.NamedExpr
            ):
                target, value = n.target, n.value
            else:
                continue
            if (
                isinstance(target, ast.Name)
                and target.id not in tainted
                and _references_tainted(value, frozenset(tainted))
            ):
                tainted.add(target.id)
                changed = True
    return frozenset(tainted)


def _call_target_name(call: ast.Call) -> str | None:
    """The bare name/attribute a Call targets (``HTTPException``, ``internal_error``, ...)."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _is_error_handler_call(call: ast.Call) -> bool:
    """True for ``ErrorHandler.<any_builder>(...)`` — matched on the OBJECT name."""
    return (
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == _ERROR_HANDLER_OBJECT_NAME
    )


def _httpexception_tainted(call: ast.Call, tainted: frozenset[str]) -> bool:
    """``HTTPException(...)``'s ``detail=`` keyword or 2nd positional arg."""
    for kw in call.keywords:
        if kw.arg == "detail" and _references_tainted(kw.value, tainted):
            return True
    return len(call.args) >= 2 and _references_tainted(call.args[1], tainted)


def _any_arg_tainted(call: ast.Call, tainted: frozenset[str]) -> bool:
    """Any positional or keyword argument references a tainted name."""
    return any(_references_tainted(a, tainted) for a in call.args) or any(
        _references_tainted(kw.value, tainted) for kw in call.keywords
    )


def _is_response_bound_finding(
    call: ast.Call, tainted: frozenset[str], error_subclasses: frozenset[str]
) -> bool:
    """True when *call* is a response-bound construct carrying tainted text."""
    if not tainted or not isinstance(call, ast.Call):
        return False
    target = _call_target_name(call)
    if target == "HTTPException":
        return _httpexception_tainted(call, tainted)
    if _is_error_handler_call(call):
        return _any_arg_tainted(call, tainted)
    if target in error_subclasses:
        return _any_arg_tainted(call, tainted)
    return False


class _TaintWalker(ast.NodeVisitor):
    """Walks a module tracking (enclosing function, in-scope taint) for Raise/Return."""

    def __init__(self, error_subclasses: frozenset[str]) -> None:
        self._error_subclasses = error_subclasses
        self._func_stack: list[str] = ["<module>"]
        self._taint_stack: list[frozenset[str]] = [frozenset()]
        # Per-function-scope taint: names assigned FROM a tainted value INSIDE an
        # except handler, but read AFTER the handler closes (e.g. `r = {"error":
        # str(e)}` inside the handler, `return r` after it). `_taint_stack` alone
        # goes empty the moment the handler suite ends, so a return several lines
        # below the handler saw no taint at all — a real, scanner-invisible finding
        # (see the module docstring / plan for the #914 residuals this closes).
        self._fn_taint: list[set[str]] = [set()]
        self.raise_findings: list[tuple[str, int]] = []
        self.return_findings: list[tuple[str, int]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._func_stack.append(node.name)
        self._fn_taint.append(set())
        self.generic_visit(node)
        self._func_stack.pop()
        self._fn_taint.pop()

    # noqa reason: dispatched by name from ast.NodeVisitor.generic_visit
    # ("visit_" + node.__class__.__name__); ast.AsyncFunctionDef is itself
    # mixedCase in the stdlib ast module, so a snake_case alias is never called.
    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]  # noqa: N815

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:  # noqa: N802
        tainted = _taint_from_except_handler(node, self._taint_stack[-1])
        # Carry every name tainted inside this handler into the enclosing
        # function's persistent taint set too — EXCEPT the handler's own bound
        # name (`except ... as e`), which is out of scope the moment the handler
        # closes anyway (Python 3 deletes it), so keeping it would manufacture a
        # finding on an `e` that can no longer be referenced.
        self._fn_taint[-1] |= tainted - ({node.name} if node.name else set())
        self._taint_stack.append(tainted)
        self.generic_visit(node)
        self._taint_stack.pop()

    def visit_Raise(self, node: ast.Raise) -> None:  # noqa: N802
        tainted = self._taint_stack[-1]
        if isinstance(node.exc, ast.Call) and _is_response_bound_finding(
            node.exc, tainted, self._error_subclasses
        ):
            self.raise_findings.append((self._func_stack[-1], node.lineno))
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:  # noqa: N802
        tainted = frozenset(self._taint_stack[-1] | self._fn_taint[-1])
        if node.value is not None and _references_tainted(node.value, tainted):
            self.return_findings.append((self._func_stack[-1], node.lineno))
        self.generic_visit(node)


#: Method names that write a value INTO the object they are called on. A tainted value
#: passed to one of these makes the receiver container-tainted (scanner 4). Chosen from
#: the mutating APIs of the builtin containers this codebase actually returns — list,
#: dict, set, deque — not from a general "any method call could mutate" premise, which
#: would flag every ``logger.error(str(e))`` in the tree.
_CONTAINER_MUTATORS = frozenset(
    {"append", "appendleft", "add", "extend", "insert", "update", "setdefault"}
)


def _container_base_name(node: ast.AST) -> str | None:
    """The root ``Name`` of an attribute/subscript chain, or None.

    ``summary["errors"].append(...)`` and ``result.detail["x"] = ...`` both resolve to
    the local the caller will later return (``summary`` / ``result``).
    """
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


class _ContainerTaintWalker(ast.NodeVisitor):
    """Scanner 4: a tainted value written into a container that is later returned.

    Mirrors ``_TaintWalker``'s scope handling (function stack, handler-local taint,
    function-persistent taint) but records CONTAINER names rather than checking the
    return expression directly, and defers the check to function exit so a mutation
    below the return statement is still seen.
    """

    def __init__(self) -> None:
        self._func_stack: list[str] = ["<module>"]
        self._taint_stack: list[frozenset[str]] = [frozenset()]
        self._fn_taint: list[set[str]] = [set()]
        #: per function scope: ``{container_name: [every tainting mutation's lineno]}``
        self._containers: list[dict[str, list[int]]] = [{}]
        #: per function scope: every ``return <expr>`` seen, checked at function exit
        self._returns: list[list[tuple[int, ast.expr]]] = [[]]
        #: ``(function, MUTATION lineno)`` — deliberately the write, not the return.
        #: Keying on the return would collapse every leak in one function to a single
        #: finding, so a second leak added beside an allowlisted one could never
        #: exceed its count. It also points the failure message at the line to fix.
        self.container_findings: list[tuple[str, int]] = []

    def _live_taint(self) -> frozenset[str]:
        return frozenset(self._taint_stack[-1] | self._fn_taint[-1])

    def _note_container(self, target: ast.AST, lineno: int) -> None:
        name = _container_base_name(target)
        if name is not None:
            self._containers[-1].setdefault(name, []).append(lineno)

    def _close_scope(self) -> None:
        containers = self._containers[-1]
        if not containers:
            return
        leaked: set[int] = set()
        for name, mutations in containers.items():
            if any(
                _references_tainted(value, frozenset({name}), _is_class_name_or_length_only)
                for _, value in self._returns[-1]
            ):
                leaked.update(mutations)
        self.container_findings.extend((self._func_stack[-1], lineno) for lineno in sorted(leaked))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._func_stack.append(node.name)
        self._fn_taint.append(set())
        self._containers.append({})
        self._returns.append([])
        self.generic_visit(node)
        self._close_scope()
        self._func_stack.pop()
        self._fn_taint.pop()
        self._containers.pop()
        self._returns.pop()

    # noqa reason: dispatched by name from ast.NodeVisitor.generic_visit, exactly as in
    # _TaintWalker above — a snake_case alias would never be called.
    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]  # noqa: N815

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:  # noqa: N802
        tainted = _taint_from_except_handler(node, self._taint_stack[-1])
        self._fn_taint[-1] |= tainted - ({node.name} if node.name else set())
        self._taint_stack.append(tainted)
        self.generic_visit(node)
        self._taint_stack.pop()

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        tainted = self._live_taint()
        if (
            tainted
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _CONTAINER_MUTATORS
            and (
                any(
                    _references_tainted(a, tainted, _is_class_name_or_length_only)
                    for a in node.args
                )
                or any(
                    _references_tainted(kw.value, tainted, _is_class_name_or_length_only)
                    for kw in node.keywords
                )
            )
        ):
            self._note_container(node.func.value, node.lineno)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        tainted = self._live_taint()
        if (
            tainted
            and len(node.targets) == 1
            and _references_tainted(node.value, tainted, _is_class_name_or_length_only)
        ):
            target = node.targets[0]
            # A plain `x = str(e)` is scanner 2/3's job (it taints the NAME). Only an
            # assignment THROUGH a subscript or attribute writes into a container.
            if isinstance(target, (ast.Subscript, ast.Attribute)):
                self._note_container(target, node.lineno)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:  # noqa: N802
        tainted = self._live_taint()
        if tainted and _references_tainted(node.value, tainted, _is_class_name_or_length_only):
            self._note_container(node.target, node.lineno)
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:  # noqa: N802
        if node.value is not None:
            self._returns[-1].append((node.lineno, node.value))
        self.generic_visit(node)


def _collect_class_bases(tree: ast.Module) -> dict[str, set[str]]:
    """``{class_name: {base_name, ...}}`` for every ``ClassDef`` in *tree*."""
    bases: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        names: set[str] = set()
        for b in node.bases:
            if isinstance(b, ast.Name):
                names.add(b.id)
            elif isinstance(b, ast.Attribute):
                names.add(b.attr)
        bases.setdefault(node.name, set()).update(names)
    return bases


def _derive_error_subclasses(
    base_map: dict[str, set[str]], root: str = "OpenTranscribeError"
) -> frozenset[str]:
    """Transitive closure of every class name that (transitively) subclasses *root*.

    Derived dynamically from the real class hierarchy under ``app/`` — never
    hardcoded — so a new ``OpenTranscribeError`` subclass is covered automatically.
    """
    subclasses = {root}
    changed = True
    while changed:
        changed = False
        for cls, cls_bases in base_map.items():
            if cls not in subclasses and cls_bases & subclasses:
                subclasses.add(cls)
                changed = True
    return frozenset(subclasses)


def _open_transcribe_error_subclasses_under(root: Path) -> frozenset[str]:
    base_map: dict[str, set[str]] = {}
    for path in _iter_py_files(root):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - a syntax error fails collection anyway
            continue
        for cls, cls_bases in _collect_class_bases(tree).items():
            base_map.setdefault(cls, set()).update(cls_bases)
    return _derive_error_subclasses(base_map)


@functools.cache
def _scan() -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    """Return ``(raise_by_key, return_by_key)``.

    ``raise_by_key`` is scanned over the WHOLE ``app/`` tree (the #891 requirement).
    ``return_by_key`` is scanned over ``app/api/`` ONLY (see module docstring).
    """
    error_subclasses = _open_transcribe_error_subclasses_under(_APP_ROOT)

    raise_by_key: dict[str, list[int]] = {}
    for path in _iter_py_files(_APP_ROOT):
        rel = _rel_to_app(path)
        source = path.read_text()
        if "raise " not in source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover
            continue
        walker = _TaintWalker(error_subclasses)
        walker.visit(tree)
        for fn, lineno in walker.raise_findings:
            raise_by_key.setdefault(f"{rel}::{fn}", []).append(lineno)

    return_by_key: dict[str, list[int]] = {}
    for path in _iter_py_files(_API_ROOT):
        rel = _rel_to_app(path)
        source = path.read_text()
        if "except" not in source or "return" not in source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover
            continue
        walker = _TaintWalker(error_subclasses)
        walker.visit(tree)
        for fn, lineno in walker.return_findings:
            return_by_key.setdefault(f"{rel}::{fn}", []).append(lineno)

    return raise_by_key, return_by_key


@functools.cache
def _scan_service_returns() -> dict[str, list[int]]:
    """Scanner 3 (#914): the return-path scan, extended to ``app/services/``.

    A separate cached function rather than a third element of ``_scan()``'s
    tuple, so every existing ``raise_by_key, return_by_key = _scan()`` call
    site (including in the selftest) keeps working unchanged.
    """
    error_subclasses = _open_transcribe_error_subclasses_under(_APP_ROOT)

    service_return_by_key: dict[str, list[int]] = {}
    for path in _iter_py_files(_SERVICES_ROOT):
        rel = _rel_to_app(path)
        source = path.read_text()
        if "except" not in source or "return" not in source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover
            continue
        walker = _TaintWalker(error_subclasses)
        walker.visit(tree)
        for fn, lineno in walker.return_findings:
            service_return_by_key.setdefault(f"{rel}::{fn}", []).append(lineno)

    return service_return_by_key


@functools.cache
def _scan_container_returns() -> dict[str, list[int]]:
    """Scanner 4: tainted value -> container -> ``return`` of that container.

    Rooted at ``app/api/`` + ``app/services/``; see the module docstring for why
    ``app/tasks/`` is excluded and which test pins that premise.
    """
    container_by_key: dict[str, list[int]] = {}
    for root in (_API_ROOT, _SERVICES_ROOT):
        for path in _iter_py_files(root):
            rel = _rel_to_app(path)
            source = path.read_text()
            if "except" not in source or "return" not in source:
                continue
            try:
                tree = ast.parse(source)
            except SyntaxError:  # pragma: no cover
                continue
            walker = _ContainerTaintWalker()
            walker.visit(tree)
            for fn, lineno in walker.container_findings:
                container_by_key.setdefault(f"{rel}::{fn}", []).append(lineno)
    return container_by_key


#: (expected_count, written reason) per ``"<path relative to app/>::<function>"`` key.
#: Every entry here raises/echoes OUR OWN authored message, never a caught library
#: exception's raw text — verified against the real source, not copied blind.
_RAISE_ALLOWLIST: dict[str, tuple[int, str]] = {
    "api/endpoints/admin_group_mappings.py::create_group_mapping": (
        1,
        "our own RoleNotGrantableError message (assert_grantable_role's refusal), "
        "never a caught library exception",
    ),
    "api/endpoints/admin_group_mappings.py::update_group_mapping": (
        1,
        "same RoleNotGrantableError re-check after a patch",
    ),
    "api/endpoints/admin_group_mappings.py::_ldap_claims_for": (
        1,
        "our own LdapDirectoryUnavailableError message — superuser-only diagnostic "
        "describing the admin's own directory configuration, not a caller-triggered leak",
    ),
    "api/endpoints/auth/dependencies.py::_authenticate_external_token": (
        1,
        "our own ExternalIdentityLinkRefusedError message from sync_external_user_to_db "
        "(a refused link, e.g. unverified email match), not a raw library exception. "
        "Renamed from PermissionError in #914 STEP 6 -- the builtin is also an OSError "
        "subclass a real EACCES anywhere in this call chain could raise, which this "
        "narrowing keeps out of the except clause that echoes str(e) into the response",
    ),
    "api/endpoints/auth_config.py::update_config_category": (
        1,
        "already has a written justification in the source: 'A rejected payload is "
        "the caller's fault and its detail is safe to return: it names the offending "
        "keys so the admin can fix the request' — a ValueError from our own "
        "bulk_update_category validator",
    ),
    "api/endpoints/auth_email_delivery.py::update_auth_mail_designation": (
        1,
        "our own ValueError from designation.set_designation (malformed UUID, "
        "unknown or disabled config) — a validator message, not a caught library error",
    ),
    "api/endpoints/backup_settings.py::update_backup_settings": (
        1,
        "our own ValueError from backup_service.update_settings — a validator message",
    ),
    "api/endpoints/directory_sync_settings.py::update_directory_sync_settings": (
        1,
        "our own ValueError from directory_sync_service.update_settings — a validator message",
    ),
    "api/endpoints/files/subtitles.py::get_subtitles": (
        1,
        "narrow ValueError from our own SubtitleService generator, not a caught "
        "third-party exception",
    ),
    "api/endpoints/files/transcript_export.py::export_transcript": (
        1,
        "narrow ValueError from our own build_export_content service, not a caught "
        "third-party exception",
    ),
    "api/endpoints/media_mirror_settings.py::update_mirror_settings": (
        1,
        "our own ValueError from media_mirror_service.update_settings — a validator message",
    ),
    "api/endpoints/search.py::_summary_search_payload": (
        1,
        "our own SummaryMaskingUnavailableError — a fail-closed masking-outage "
        "message we author, not a caught library exception",
    ),
    "api/endpoints/search.py::trigger_reindex": (
        1,
        "our own ReindexDispatchError message — only true now that "
        "services/search/model_switch.py::dispatch_reindex_for_every_owner (#891 "
        "exemplar) no longer interpolates the raw broker exception into that message",
    ),
    "api/endpoints/search.py::_switch_model": (
        3,
        "our own UnknownEmbeddingModelError / EmbeddingModelNotDeployedError / "
        "ReindexDispatchError messages, all authored by model_switch.py itself",
    ),
    "api/endpoints/speaker_clusters.py::analyze_outliers": (
        1,
        "verified: SpeakerClusteringService.analyze_gender_outliers raises only the "
        "fixed literal 'Cluster not found', never a path/uuid-carrying message",
    ),
    "api/endpoints/speaker_clusters.py::unassign_speakers": (
        1,
        "verified: SpeakerClusteringService.unassign_speakers raises only fixed "
        "literals like 'No valid speakers found'",
    ),
    "api/endpoints/tags/_common.py::_apply": (
        1,
        "our own TagNotFoundError message (the InvalidTagNameError branch beside it "
        "raises a fixed literal and is not tainted at all)",
    ),
    "api/endpoints/tags/sharing.py::create_tag_share": (
        1,
        "our own TagShareError message from services/tag_sharing.share_tag",
    ),
    "api/endpoints/watch_sources.py::browse_directories": (
        1,
        "verified: folder_browser.list_directories raises only three fixed-literal "
        "ValueErrors, never a host path",
    ),
    "auth/external_sync.py::sync_external_user_to_db": (
        1,
        "our own ExternalIdentityLinkRefusedError re-wrap of an HTTPException raised by "
        "assert_provider_id_link_permitted with a fixed failure_detail= literal "
        "('External identity could not be verified') -- new in #914 STEP 6, "
        "surfaced because ExternalIdentityLinkRefusedError is itself an "
        "OpenTranscribeError subclass the raise-scanner now tracks",
    ),
    "services/email_service.py::_send_via_env_smtp": (
        1,
        "already scrubs every embedded email address via _scrub(str(e)) before "
        "interpolating, per this module's own documented contract "
        "(reason = _scrub(str(e)); raise EmailDeliveryError(f'...{reason}') from e)",
    ),
    "services/search/model_switch.py::apply_embedding_model_switch": (
        1,
        "narrow re-wrap of our OWN ReindexDispatchError — 'The embedding model was "
        "switched, but the re-embed could not be queued: {e}' where e is itself the "
        "already-sanitized ReindexDispatchError from dispatch_reindex_for_every_owner "
        "(#891 exemplar, fixed in this same change)",
    ),
}

#: (expected_count, written reason) per ``"<path>::<function>"`` key, return-path.
_RETURN_ALLOWLIST: dict[str, tuple[int, str]] = {
    "api/endpoints/asr_settings.py::test_asr_connection": (
        1,
        "already calls _sanitize_message(str(exc), api_key) before returning it",
    ),
    "api/endpoints/asr_settings.py::test_saved_asr_config": (
        1,
        "same as test_asr_connection beside it: already calls "
        "_sanitize_message(str(exc), api_key) before storing/returning it. Only "
        "surfaced as its own finding once the 4b taint-through-assignment fix "
        "landed (message is assigned in the except handler, read after it closes)",
    ),
    "api/endpoints/files/__init__.py::_ready_frame": (
        1,
        "re-surfaces e.detail of an HTTPException WE raised ourselves two lines "
        "above (the 422 'Transcript is not ready yet'), not a caught library error",
    ),
    "api/endpoints/llm_settings.py::get_ollama_models": (
        1,
        "narrow aiohttp.ClientError against a caller-SUPPLIED base_url; the dial "
        "result IS the product this endpoint exists to report",
    ),
    "api/endpoints/llm_settings.py::get_openai_compatible_models": (
        1,
        "same: narrow aiohttp.ClientError against a caller-supplied base_url, the "
        "dial result IS the product",
    ),
    "api/endpoints/llm_settings.py::get_anthropic_models": (
        1,
        "same: narrow aiohttp.ClientError, the dial result IS the product",
    ),
    "api/endpoints/watch_sources.py::test_multipart_regex": (
        1,
        "narrow ValueError echoing the caller's OWN regex back — the entire point "
        "of a regex-test endpoint",
    ),
}

#: (expected_count, written reason) per ``"<path>::<function>"`` key, SERVICE
#: return-path (#914 STEP 8 / scanner 3). Two families:
#:
#: §2b — dial results. All nine ASR ``validate_connection`` implementations plus
#: the two ``test_s3_connection`` helpers report the outcome of a connection
#: attempt the caller (an admin testing THEIR OWN configured provider, via
#: asr_settings.py / backup_settings.py / media_mirror_settings.py) explicitly
#: asked for — the dial result IS the product these endpoints exist to report,
#: the same precedent already set for the LLM/ASR test-connection routes in
#: ``_RETURN_ALLOWLIST`` above. Every ASR provider routes its message through
#: ``self._sanitize_error`` -> ``services/asr/base.py::sanitize_provider_error``,
#: which strips API keys and bearer/token-shaped credentials — but that is its
#: honest limit: it scrubs CREDENTIALS ONLY, never hostnames/endpoints, so a
#: provider message naming its own API host is not something this allowlist
#: pretends is impossible, only that the credential half is actually handled.
#:
#: §2c — internal-only. The tainted value never reaches an HTTP response body;
#: every entry names the real consumer(s) traced by hand so a reason can go
#: stale visibly rather than silently ("internal" alone is not a reason).
_SERVICE_RETURN_ALLOWLIST: dict[str, tuple[int, str]] = {
    "services/asr/assemblyai_provider.py::validate_connection": (
        1,
        "dial result: self._sanitize_error(str(e), api_key) -> sanitize_provider_error "
        "(credentials scrubbed, not hosts) -- the caller's own connection test",
    ),
    "services/asr/aws_provider.py::validate_connection": (
        1,
        "dial result, same self._sanitize_error path as assemblyai_provider.py",
    ),
    "services/asr/azure_provider.py::validate_connection": (
        1,
        "dial result, same self._sanitize_error path as assemblyai_provider.py",
    ),
    "services/asr/deepgram_provider.py::validate_connection": (
        1,
        "dial result, same self._sanitize_error path as assemblyai_provider.py",
    ),
    "services/asr/gladia_provider.py::validate_connection": (
        1,
        "dial result, same self._sanitize_error path as assemblyai_provider.py",
    ),
    "services/asr/google_provider.py::validate_connection": (
        1,
        "dial result, same self._sanitize_error path as assemblyai_provider.py",
    ),
    "services/asr/openai_provider.py::validate_connection": (
        1,
        "dial result, same self._sanitize_error path as assemblyai_provider.py",
    ),
    "services/asr/pyannote_provider.py::validate_connection": (
        1,
        "dial result, same self._sanitize_error path as assemblyai_provider.py "
        "(this is the ASR provider, services/asr/ -- not the diarization-only "
        "services/diarization/pyannote_provider.py entry below, a different class "
        "with zero call sites)",
    ),
    "services/asr/speechmatics_provider.py::validate_connection": (
        1,
        "dial result, same self._sanitize_error path as assemblyai_provider.py",
    ),
    "services/backup_service.py::test_s3_connection": (
        1,
        "dial result: the admin's own S3 destination test, via "
        "POST /admin/backup/test-s3 -- the connection outcome IS the product",
    ),
    "services/media_mirror_service.py::test_s3_connection": (
        1,
        "dial result: the admin's own S3 destination test, via the media-mirror "
        "settings test action -- the connection outcome IS the product",
    ),
    "services/chat/legs.py::_run_leg": (
        1,
        "internal-only: LegOutcome.error is read only by LegOutcome.ok (:141) and "
        "a logger.warning; FanOutResult.as_metadata (:159-176) emits leg NAMES and "
        "timings, never a leg's error text",
    ),
    "services/opensearch_service/client.py::probe_knn_health": (
        4,
        "internal-only: KnnHealthProbe.detail is read only by logger calls in "
        "repair.py:565,571 and index_health.py:245,249; repair.py:44 reads only "
        "probe.is_serviceable, never probe.detail",
    ),
    "services/redaction/service.py::detect_and_store": (
        1,
        "internal-only: the returned dict's error is read by redaction_task.py:134, "
        "whose _notify (:205-221) forwards only status/segments/pii_entities_found/"
        "language/skipped_detectors to the WS event -- error is dropped, never sent",
    ),
    "services/search/ml_model_service.py::verify_model_can_embed": (
        1,
        "internal-only: the one call site (:657) reads the result into a logger "
        "call only, never a response",
    ),
    "services/watch_email_service.py::send_email": (
        1,
        "internal-only: both callers discard or pre-scrub the message before any "
        "HTTP surface sees it -- email_service.py:416 runs it through _scrub() "
        "before logging, watch_source_tasks.py:634 unpacks it as `_msg` and never "
        "reads it at all",
    ),
    "services/diarization/pyannote_provider.py::validate_connection": (
        1,
        "internal-only in the strongest sense: ZERO call sites. Required only "
        "because DiarizationProvider (base.py:41) declares it @abstractmethod -- "
        "see the comment on the method itself for why this is reported, not fixed",
    ),
    "services/backup_recovery.py::write_companion": (
        1,
        "exc.returncode is a small int (the gpg process exit code) interpolated "
        "into our own authored 'gpg failed (exit N)' message, never message text -- "
        "the same authored-safe-fact shape as the type(e).__name__ prune (4a), for "
        "a different attribute the scanner has no structural exemption for",
    ),
    "services/backup_service.py::_perform_backup_local": (
        1,
        "same exc.cmd[0]/exc.returncode safe-attribute shape as "
        "backup_recovery.py::write_companion -- 'result' is tainted only because "
        "one except branch builds it from exc.cmd[0] (a fixed argv[0] like "
        "'pg_dump') and exc.returncode, both ints/literals, never message text; "
        "the shared `return result` also serves the (untainted) success path",
    ),
    "services/backup_service.py::_perform_backup_s3": (
        1,
        "same as _perform_backup_local -- the S3 backend's twin CalledProcessError branch",
    ),
    "services/directory_sync_service.py::sweep_ldap": (
        1,
        "already-sanitized upstream: error = str(e) where e is "
        "LdapDirectoryUnavailableError, scrubbed at the auth/ldap_auth.py raiser "
        "(#914 STEP 5) to type(exc).__name__ only -- same 'narrow re-wrap of our "
        "OWN already-sanitized exception' shape as the raise-allowlist's "
        "model_switch.py::apply_embedding_model_switch entry above",
    ),
    "services/tag_bulk.py::_require_editor": (
        1,
        "our own HTTPException.detail from PermissionService.check_file_access, "
        "which raises only two fixed literals ('Not authorized to access this "
        "file' / 'Requires {min_permission} permission on this file') -- a "
        "validator message, not a caught library exception",
    ),
}


#: (expected_count, written reason) per ``"<path>::<function>"`` key, CONTAINER
#: return-path (scanner 4). Same two families as ``_SERVICE_RETURN_ALLOWLIST``:
#:
#: §4b — the caller is entitled to the value. Either a dial result the admin asked
#: for against their own configured destination, or an ``HTTPException.detail`` we
#: authored ourselves.
#:
#: §4c — internal-only. The container never reaches an HTTP response body; every
#: entry names the real consumer(s), traced by hand to a terminating read.
_CONTAINER_RETURN_ALLOWLIST: dict[str, tuple[int, str]] = {
    "api/endpoints/asr_settings.py::test_saved_asr_config": (
        1,
        "dial result, and the SAME finding _RETURN_ALLOWLIST already carries for this "
        "function -- scanner 4 re-surfaces it through the `config.test_message = "
        "_sanitize_message(message, api_key)` attribute write rather than the returned "
        "`message`. Already sanitized on both paths; note the returned expression only "
        "references `config` via str(config.uuid), never the tainted attribute",
    ),
    "api/endpoints/files/management.py::bulk_file_action": (
        1,
        "our own HTTPException detail: the surviving tainted branch is `except "
        "HTTPException as e: message=str(e.detail)`, the same re-surfacing of a detail "
        "WE raised that _RETURN_ALLOWLIST allows for files/__init__.py::_ready_frame. "
        "That those details are ours is not an assertion here but a mechanical "
        "consequence of scanner 1: it scans the whole app/ tree for a tainted "
        "HTTPException detail and has zero unallowlisted findings, so no reachable "
        "raise can put caught library text into one. The sibling `except Exception` "
        "branch WAS a real leak and is fixed (type(e).__name__ + logger.exception)",
    ),
    "services/auto_label_service.py::retroactive_apply": (
        1,
        "internal-only: ZERO call sites in app/ -- the only `retroactive_apply` hits "
        'are the WS-event label `file_id="retroactive_apply"` in tasks/auto_labeling.py '
        "(:154,179,207,314,335), which is a string, not a call. Nothing consumes the "
        'returned `result["errors"]` at all',
    ),
    "services/backup_service.py::s3_bucket_status": (
        1,
        "dial result: reachability of the ADMIN'S OWN S3 backup destination, reached "
        "via backup_settings.py::_s3_status (:192) -> S3Status(error=raw.get('error')) "
        "on GET /admin/backup/settings. Same §2b family as this module's "
        "test_s3_connection entry above, and the two facts a botocore message adds -- "
        "the bucket and the endpoint URL -- are already explicit sibling FIELDS of the "
        "same S3Status response. The secret key is never interpolated by botocore",
    ),
    "services/media_mirror_service.py::s3_bucket_status": (
        1,
        "dial result, the media-mirror twin of backup_service.py::s3_bucket_status: "
        "media_mirror_settings.py::_s3_status (:124) -> MirrorS3Status(**...), whose "
        "s3_endpoint_url/s3_bucket are likewise already fields of the same settings "
        "response (and s3_secret_key_set is a bool, never the secret)",
    ),
    "services/file_cleanup_service.py::_load_purge_plan": (
        1,
        "internal-only: `plan['speaker_read_error']` is read at :465 by "
        "_purge_external_copies, folded into `residual` (:467), and residual's only "
        "consumers are purge_account_external_copies' -- see that entry below",
    ),
    "services/file_cleanup_service.py::purge_account_external_copies": (
        2,
        "internal-only: the two call sites are admin.py:1022 and users.py:872, and "
        "BOTH pass it only to audit_user_deleted(..., residual_errors), which "
        "deliberately records counters and stage NAMES and never the error strings "
        "(account_security_service.py:302-308, with the no-free-text rule written into "
        "its own docstring at :297-300). The HTTP responses carry "
        "len(residual_errors) (admin.py:1034) and None (users.py:877)",
    ),
    "services/file_cleanup_service.py::force_cleanup_orphaned_files": (
        2,
        "internal-only: one call site, tasks/cleanup.py:88 inside the "
        "`cleanup.deep_cleanup` Celery task, which logs the errors (:98) and returns "
        "them as the TASK result -- never read back (see "
        "test_a_celery_result_is_never_read_into_a_response)",
    ),
    "services/file_cleanup_service.py::run_cleanup_cycle": (
        2,
        "internal-only: one call site, tasks/cleanup.py:49 inside the "
        "`cleanup.periodic` Celery task; same log-and-return-as-task-result shape as "
        "force_cleanup_orphaned_files above",
    ),
    "services/media_download_service.py::_process_playlist_videos": (
        1,
        "internal-only: `skipped_videos` is returned to process_youtube_playlist_sync "
        "(:1812), which puts it on its result dict (:1843); the one consumer of that "
        "dict, tasks/youtube_processing.py::_handle_playlist_result (via :764), reads "
        "`result.get('skipped_count', 0)` (:810) and never the per-video reasons",
    ),
}


def test_the_scanner_actually_finds_call_sites() -> None:
    """A scanner that matches nothing is indistinguishable from a clean tree."""
    raise_by_key, return_by_key = _scan()
    assert len(raise_by_key) >= 18, (
        f"expected at least 18 distinct raise-path findings in the real tree, "
        f"found {len(raise_by_key)} — either the scanner regressed or the codebase "
        "genuinely eliminated most of them (update this floor deliberately if so)"
    )
    assert len(return_by_key) >= 5, (
        f"expected at least 5 distinct return-path findings under app/api/, "
        f"found {len(return_by_key)}"
    )
    # Measured post-fix (#914 STEP 8): 22 distinct service-return keys. Set ONE
    # below deliberately, so a genuine future reduction doesn't need a same-day
    # floor bump, while a scanner regression to near-zero still trips this.
    service_return_by_key = _scan_service_returns()
    assert len(service_return_by_key) >= 21, (
        f"expected at least 21 distinct service-return-path findings under "
        f"app/services/, found {len(service_return_by_key)} — either the "
        "scanner regressed or the codebase genuinely eliminated most of them "
        "(update this floor deliberately if so)"
    )
    # Measured post-fix (scanner 4's own remediation wave): 10 distinct container-return
    # keys across app/api/ + app/services/, from 14 before six response-bound sites were
    # sanitized. Set ONE below deliberately, same convention as the floor above.
    container_by_key = _scan_container_returns()
    assert len(container_by_key) >= 9, (
        f"expected at least 9 distinct container-return-path findings under app/api/ + "
        f"app/services/, found {len(container_by_key)} — either the scanner regressed "
        "or the codebase genuinely eliminated most of them (update this floor "
        "deliberately if so)"
    )


def test_no_unallowlisted_raise_path_exception_echo() -> None:
    raise_by_key, _ = _scan()
    unallowlisted = sorted(set(raise_by_key) - set(_RAISE_ALLOWLIST))
    assert not unallowlisted, (
        "These raise-path sites echo caught exception text into a response-bound "
        "construct with no allowlist entry. Rewrite with a fixed literal (see "
        "app/utils/error_handlers.py::ErrorHandler), or add a reasoned "
        "_RAISE_ALLOWLIST entry if the message is genuinely our own authored "
        "text:\n  " + "\n  ".join(unallowlisted)
    )


def test_a_raise_site_may_not_exceed_its_allowlisted_count() -> None:
    raise_by_key, _ = _scan()
    over: list[str] = []
    for key, findings in raise_by_key.items():
        expected = _RAISE_ALLOWLIST.get(key)
        if expected is not None and len(findings) > expected[0]:
            over.append(f"{key} (found {len(findings)}, allowlisted {expected[0]})")
    assert not over, (
        "These functions have MORE tainted raise sites than their allowlist entry "
        "accounts for — a new leak was added beside an already-allowlisted one:\n  "
        + "\n  ".join(over)
    )


def test_the_raise_allowlist_is_honest() -> None:
    raise_by_key, _ = _scan()
    stale: list[str] = []
    inflated: list[str] = []
    unexplained: list[str] = []
    for key, (expected_count, reason) in sorted(_RAISE_ALLOWLIST.items()):
        actual = len(raise_by_key.get(key, []))
        if actual == 0:
            stale.append(key)
        elif actual < expected_count:
            inflated.append(f"{key} (allowlisted {expected_count}, found {actual})")
        if not reason.strip():
            unexplained.append(key)
    assert not stale, f"allowlist entries point at functions with no finding at all: {stale}"
    assert not inflated, f"allowlist entries claim more findings than exist: {inflated}"
    assert not unexplained, f"allowlist entries need a written reason: {unexplained}"


def test_no_unallowlisted_return_path_exception_echo() -> None:
    _, return_by_key = _scan()
    unallowlisted = sorted(set(return_by_key) - set(_RETURN_ALLOWLIST))
    assert not unallowlisted, (
        "These return-path sites (under app/api/) echo caught exception text into "
        "a returned value with no allowlist entry:\n  " + "\n  ".join(unallowlisted)
    )


def test_a_return_site_may_not_exceed_its_allowlisted_count() -> None:
    _, return_by_key = _scan()
    over: list[str] = []
    for key, findings in return_by_key.items():
        expected = _RETURN_ALLOWLIST.get(key)
        if expected is not None and len(findings) > expected[0]:
            over.append(f"{key} (found {len(findings)}, allowlisted {expected[0]})")
    assert not over, (
        "These functions have MORE tainted return sites than their allowlist entry "
        "accounts for:\n  " + "\n  ".join(over)
    )


def test_the_return_allowlist_is_honest() -> None:
    _, return_by_key = _scan()
    stale: list[str] = []
    inflated: list[str] = []
    unexplained: list[str] = []
    for key, (expected_count, reason) in sorted(_RETURN_ALLOWLIST.items()):
        actual = len(return_by_key.get(key, []))
        if actual == 0:
            stale.append(key)
        elif actual < expected_count:
            inflated.append(f"{key} (allowlisted {expected_count}, found {actual})")
        if not reason.strip():
            unexplained.append(key)
    assert not stale, f"allowlist entries point at functions with no finding at all: {stale}"
    assert not inflated, f"allowlist entries claim more findings than exist: {inflated}"
    assert not unexplained, f"allowlist entries need a written reason: {unexplained}"


def test_no_unallowlisted_service_return_path_exception_echo() -> None:
    """Scanner 3 (#914 STEP 8): the return-path scan, extended to app/services/."""
    service_return_by_key = _scan_service_returns()
    unallowlisted = sorted(set(service_return_by_key) - set(_SERVICE_RETURN_ALLOWLIST))
    assert not unallowlisted, (
        "These return-path sites (under app/services/) echo caught exception text "
        "into a returned value with no allowlist entry. Rewrite with the "
        "type(exc).__name__ pattern (see the #914 fix commits), or add a reasoned "
        "_SERVICE_RETURN_ALLOWLIST entry naming the traced consumer if the value "
        "genuinely never reaches an HTTP response body:\n  " + "\n  ".join(unallowlisted)
    )


def test_a_service_return_site_may_not_exceed_its_allowlisted_count() -> None:
    service_return_by_key = _scan_service_returns()
    over: list[str] = []
    for key, findings in service_return_by_key.items():
        expected = _SERVICE_RETURN_ALLOWLIST.get(key)
        if expected is not None and len(findings) > expected[0]:
            over.append(f"{key} (found {len(findings)}, allowlisted {expected[0]})")
    assert not over, (
        "These functions have MORE tainted return sites than their allowlist entry "
        "accounts for -- a new leak was added beside an already-allowlisted one:\n  "
        + "\n  ".join(over)
    )


def test_the_service_return_allowlist_is_honest() -> None:
    service_return_by_key = _scan_service_returns()
    stale: list[str] = []
    inflated: list[str] = []
    unexplained: list[str] = []
    for key, (expected_count, reason) in sorted(_SERVICE_RETURN_ALLOWLIST.items()):
        actual = len(service_return_by_key.get(key, []))
        if actual == 0:
            stale.append(key)
        elif actual < expected_count:
            inflated.append(f"{key} (allowlisted {expected_count}, found {actual})")
        if not reason.strip():
            unexplained.append(key)
    assert not stale, f"allowlist entries point at functions with no finding at all: {stale}"
    assert not inflated, f"allowlist entries claim more findings than exist: {inflated}"
    assert not unexplained, f"allowlist entries need a written reason: {unexplained}"


def test_no_unallowlisted_container_return_path_exception_echo() -> None:
    """Scanner 4: taint into a container that is later returned."""
    container_by_key = _scan_container_returns()
    unallowlisted = sorted(set(container_by_key) - set(_CONTAINER_RETURN_ALLOWLIST))
    assert not unallowlisted, (
        "These sites write caught exception text into a CONTAINER that is later "
        "returned -- invisible to scanners 2 and 3, which check the return expression "
        "itself. Keep the per-item identity and an app-level reason, drop the "
        "interpolation, and logger.exception the detail (see the #914 fix commits); or "
        "add a reasoned _CONTAINER_RETURN_ALLOWLIST entry naming the traced "
        "consumer:\n  " + "\n  ".join(unallowlisted)
    )


def test_a_container_return_site_may_not_exceed_its_allowlisted_count() -> None:
    container_by_key = _scan_container_returns()
    over: list[str] = []
    for key, findings in container_by_key.items():
        expected = _CONTAINER_RETURN_ALLOWLIST.get(key)
        if expected is not None and len(findings) > expected[0]:
            over.append(f"{key} (found {len(findings)}, allowlisted {expected[0]})")
    assert not over, (
        "These functions have MORE tainted container-return sites than their allowlist "
        "entry accounts for -- a new leak was added beside an already-allowlisted "
        "one:\n  " + "\n  ".join(over)
    )


def test_the_container_return_allowlist_is_honest() -> None:
    container_by_key = _scan_container_returns()
    stale: list[str] = []
    inflated: list[str] = []
    unexplained: list[str] = []
    for key, (expected_count, reason) in sorted(_CONTAINER_RETURN_ALLOWLIST.items()):
        actual = len(container_by_key.get(key, []))
        if actual == 0:
            stale.append(key)
        elif actual < expected_count:
            inflated.append(f"{key} (allowlisted {expected_count}, found {actual})")
        if not reason.strip():
            unexplained.append(key)
    assert not stale, f"allowlist entries point at functions with no finding at all: {stale}"
    assert not inflated, f"allowlist entries claim more findings than exist: {inflated}"
    assert not unexplained, f"allowlist entries need a written reason: {unexplained}"


def test_a_celery_result_is_never_read_into_a_response() -> None:
    """The premise scanner 4's ``app/tasks/`` exclusion rests on.

    A Celery task's return value lands in the result backend, so it is response-bound
    only if something reads it back. This application reads ``AsyncResult`` in exactly
    two places and touches only ``.state`` — never ``.result``/``.get()``/``.wait()``.
    If that ever changes, the scan root is wrong and this is what says so, rather than
    the exclusion quietly becoming a hole.
    """
    bindings: list[tuple[str, int, str]] = []  # (rel, lineno, attribute read)
    binding_count = 0
    safe_attributes = {"state"}
    for path in _iter_py_files(_APP_ROOT):
        source = path.read_text()
        if "AsyncResult" not in source:
            continue
        # Deliberately NOT guarded with `except SyntaxError: continue` the way the
        # module-level scanners are: this is a test body, and a file under app/ that
        # does not parse is a failure worth seeing, not one to skip past.
        tree = ast.parse(source)
        rel = _rel_to_app(path)
        names: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Call)
                and _call_target_name(node.value) == "AsyncResult"
            ):
                names.add(node.targets[0].id)
                binding_count += 1
        if not names:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in names
                and node.attr not in safe_attributes
            ):
                bindings.append((rel, node.lineno, node.attr))

    # Non-vacuity: a scan that found no AsyncResult binding at all would pass the
    # assertion below while proving nothing.
    assert binding_count >= 2, (
        f"expected at least 2 AsyncResult bindings under app/ (they live in "
        f"utils/task_utils.py), found {binding_count} — either they moved or this "
        "guard stopped matching, and scanner 4's app/tasks/ exclusion is unproven"
    )
    assert not bindings, (
        "A Celery result is now read beyond .state:\n  "
        + "\n  ".join(f"{rel}:{lineno} reads .{attr}" for rel, lineno, attr in bindings)
        + "\nScanner 4 excludes app/tasks/ BECAUSE a task's return value reaches no "
        "HTTP response body. Reading .result/.get()/.wait() makes it response-bound, "
        "so either revert that read or add app/tasks/ to _scan_container_returns()."
    )


def test_the_scan_root_includes_the_service_layer() -> None:
    """The #891 acceptance criterion, made mechanical.

    If someone "simplifies" the raise-path scan root back down to app/api/, both
    the allowlist and the findings would lose every services/-prefixed key. This
    fails loudly the moment that happens, rather than quietly losing coverage.
    """
    raise_by_key, _ = _scan()
    service_keys_found = {k for k in raise_by_key if k.startswith("services/")}
    service_keys_allowlisted = {k for k in _RAISE_ALLOWLIST if k.startswith("services/")}

    # Concrete, not just non-empty: a real service-layer finding must be among
    # the live findings, and a real allowlisted service-layer site must survive
    # beside it — a bare-truthy check on the two sets would still pass if an
    # unrelated services/ finding replaced both of these.
    #
    # NOTE: the #891 exemplar itself, dispatch_reindex_for_every_owner, is
    # fixed so thoroughly it no longer appears in raise_by_key AT ALL — its
    # raised message references only `len(tasks)`/`len(ordered)`, neither of
    # which is ever assigned from the caught exception, so there is nothing
    # left to taint. apply_embedding_model_switch is the sibling that still
    # legitimately re-wraps that (now-sanitized) ReindexDispatchError's `e`.
    assert "services/search/model_switch.py::apply_embedding_model_switch" in service_keys_found, (
        "the model_switch.py re-wrap of ReindexDispatchError is no longer a live "
        "raise-path finding — either it regressed to interpolating something new, "
        "or the scan root was narrowed back to app/api/ (update this test "
        "deliberately only in the latter case)"
    )
    assert "services/email_service.py::_send_via_env_smtp" in service_keys_allowlisted, (
        "the raise-path allowlist lost its services/-prefixed entry for the "
        "already-scrubbed email-delivery raise — same #891 regression check as "
        "above, applied to the allowlist rather than the live findings"
    )
    assert service_keys_found, (
        "the raise-path scanner found NO services/-prefixed findings — either the "
        "scan root was narrowed back to app/api/ (the #891 regression this test "
        "exists to catch), or the service layer genuinely has zero response-bound "
        "exception-echo sites left (update this test deliberately if so)"
    )
    assert service_keys_allowlisted, (
        "the raise-path allowlist has no services/-prefixed entry — same regression "
        "check as above, applied to the allowlist rather than the live findings"
    )


def test_logging_is_not_a_finding() -> None:
    """A plain log call with no accompanying raise/return must never be a finding."""
    source = """
        def handler():
            try:
                do_thing()
            except Exception as e:
                logger.error(f"failed: {e}")
                logger.exception("failed")
    """
    import textwrap

    tree = ast.parse(textwrap.dedent(source))
    walker = _TaintWalker(frozenset({"OpenTranscribeError"}))
    walker.visit(tree)
    assert walker.raise_findings == []
    assert walker.return_findings == []
