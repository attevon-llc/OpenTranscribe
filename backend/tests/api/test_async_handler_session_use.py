"""No ``async def`` route handler may touch the request's SQLAlchemy session on the event loop.

FastAPI, "Concurrency and async / await" (https://fastapi.tiangolo.com/async/): a path
operation declared ``async def`` runs ON the event loop, and must not block it; one
declared plain ``def`` runs in the threadpool. A SQLAlchemy ``Session`` is synchronous,
and its first statement checks a connection out of the pool, which blocks the calling
thread for up to ``pool_timeout`` (30 s) when the pool is exhausted. On the event loop
that thread is every request, WebSocket and stream the process serves (concurrency audit
F5, ``docs/design/concurrency_foundations_plan.md``).

``tests/api/test_handler_blocking_io.py`` fails an ``async def`` handler with no ``await``;
it cannot see one that awaits something AND uses the session directly. This guard can.
For every ``async def`` route handler under ``backend/app`` (and the async helpers that
receive its session, :data:`SESSION_TAKING_ASYNC_HELPERS`), it fails on:

* a call on the session (``db.query(...)``, ``db.commit()``) anywhere but as the
  callable handed to ``run_in_threadpool`` (``run_in_threadpool(db.close)`` is fine);
* the session passed as an argument to anything but ``run_in_threadpool`` (or an async
  helper in :data:`SESSION_TAKING_ASYNC_HELPERS`, which this file then checks itself);
* the same inside a ``lambda`` or nested function, unless that callable is what is handed
  to ``run_in_threadpool``.

The fix is the one the docs give: make the handler ``def`` when it awaits only threadpool
hops, or keep it ``async`` and move each piece of session work into a sync helper run
through ``run_in_threadpool``. What the scan cannot see: an ORM attribute read that lazily
loads (or refreshes an expired row) on the loop. Handlers here pass plain values across the
``await`` boundary for that reason.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_APP = Path(__file__).resolve().parents[2] / "app"

_ROUTE_DECORATORS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "api_route", "websocket"}
)

#: Calls that run their callable argument in a worker thread.
_OFFLOAD_CALLS = frozenset({"run_in_threadpool", "run_sync", "to_thread"})

#: Async helpers that are handed the request session and offload every use of it
#: themselves. Each is checked by the same rules as a handler, so listing one here moves
#: the check rather than skipping it. ``(module path under app/, function name)``.
SESSION_TAKING_ASYNC_HELPERS: tuple[tuple[str, str], ...] = (
    ("api/endpoints/auth/sessions.py", "_end_federated_session"),
    ("api/endpoints/files/upload.py", "process_file_upload"),
    ("tasks/summary_retry.py", "retry_summary_if_available"),
)


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _session_params(fn: ast.AsyncFunctionDef) -> set[str]:
    """Parameters that hold a SQLAlchemy session: annotated ``Session``, or ``Depends(get_db)``."""
    args = fn.args
    positional = [*args.posonlyargs, *args.args]
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults))
    defaults += list(args.defaults)
    pairs = list(zip(positional, defaults, strict=True))
    pairs += list(zip(args.kwonlyargs, args.kw_defaults, strict=True))
    names: set[str] = set()
    for arg, default in pairs:
        annotation = ast.unparse(arg.annotation) if arg.annotation is not None else ""
        annotated = annotation.split("|")[0].strip().rsplit(".", 1)[-1] in {
            "Session",
            "AsyncSession",
        }
        injected = default is not None and "get_db" in ast.unparse(default)
        if annotated or injected:
            names.add(arg.arg)
    return names


def _is_route_handler(fn: ast.AsyncFunctionDef) -> bool:
    for decorator in fn.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and target.attr in _ROUTE_DECORATORS:
            return True
    return False


def _uses(node: ast.AST, session: set[str]) -> bool:
    return any(isinstance(n, ast.Name) and n.id in session for n in ast.walk(node))


def session_violations(fn: ast.AsyncFunctionDef, allowed_async: frozenset[str]) -> list[str]:
    """Every place ``fn`` touches its session on the event loop, as ``"<what>@<line>"``."""
    session = _session_params(fn)
    if not session:
        return []

    offloaded: set[int] = set()  # ids of nodes handed to an offload call
    nested_names_offloaded: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and _call_name(node.func) in _OFFLOAD_CALLS and node.args:
            target = node.args[0]
            offloaded.add(id(target))
            if isinstance(target, ast.Name):
                nested_names_offloaded.add(target.id)

    found: list[str] = []

    def check(node: ast.AST) -> None:
        if isinstance(node, ast.Lambda | ast.FunctionDef | ast.AsyncFunctionDef):
            if id(node) in offloaded:
                return
            if not isinstance(node, ast.Lambda) and node.name in nested_names_offloaded:
                return
            if _uses(node, session):
                found.append(f"session used in a callable run on the loop@{node.lineno}")
            return
        if isinstance(node, ast.Call):
            func = node.func
            name = _call_name(func)
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id in session
            ):
                found.append(f"{func.value.id}.{func.attr}()@{node.lineno}")
            elif name not in _OFFLOAD_CALLS and name not in allowed_async:
                passed = [*node.args, *(kw.value for kw in node.keywords)]
                if any(isinstance(a, ast.Name) and a.id in session for a in passed):
                    found.append(f"session passed to {name or ast.unparse(func)}()@{node.lineno}")
        for child in ast.iter_child_nodes(node):
            check(child)

    for statement in fn.body:
        check(statement)
    return found


def _scan_handlers() -> dict[str, list[str]]:
    allowed = frozenset(name for _, name in SESSION_TAKING_ASYNC_HELPERS)
    offenders: dict[str, list[str]] = {}
    for path in sorted(_APP.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and _is_route_handler(node):
                hits = session_violations(node, allowed)
                if hits:
                    offenders[f"{path.relative_to(_APP)}::{node.name}"] = hits
    return offenders


def test_no_async_route_handler_uses_the_session_on_the_event_loop() -> None:
    offenders = _scan_handlers()
    assert not offenders, (
        "these `async def` route handlers use the SQLAlchemy session on the event loop; make "
        "the handler `def`, or move the session work into a sync helper run through "
        f"`run_in_threadpool` (FastAPI, 'Concurrency and async / await'): {offenders}"
    )


@pytest.mark.parametrize(
    ("module", "name"),
    SESSION_TAKING_ASYNC_HELPERS,
    ids=[f"{m.rsplit('/', 1)[-1]}::{n}" for m, n in SESSION_TAKING_ASYNC_HELPERS],
)
def test_async_helpers_given_the_session_offload_it(module: str, name: str) -> None:
    tree = ast.parse((_APP / module).read_text())
    matches = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == name]
    assert len(matches) == 1, f"{module}::{name} is no longer one `async def`; update the list"
    fn = matches[0]
    assert _session_params(fn), f"{module}::{name} no longer takes a session; drop it from the list"
    allowed = frozenset(n for _, n in SESSION_TAKING_ASYNC_HELPERS)
    assert session_violations(fn, allowed) == []


# --- the detector itself: it must fire, and it must not fire on the cure --------------------
_MUST_FIRE = {
    "direct query": (
        "@router.get('/x')\n"
        "async def h(db: Session = Depends(get_db)):\n"
        "    await something()\n"
        "    return db.query(M).first()\n"
    ),
    "session passed to a sync helper": (
        "@router.post('/x')\n"
        "async def h(db: Session = Depends(get_db)):\n"
        "    row = load_row(db, 1)\n"
        "    await notify(row)\n"
    ),
    "commit after an await, unannotated Depends(get_db)": (
        "@router.post('/x')\n"
        "async def h(session=Depends(get_db)):\n"
        "    await something()\n"
        "    session.commit()\n"
    ),
    "a lambda that is CALLED on the loop": (
        "@router.get('/x')\n"
        "async def h(db: Session = Depends(get_db)):\n"
        "    read = lambda: db.query(M).first()\n"
        "    await something()\n"
        "    return read()\n"
    ),
    "a session-taking async helper that is not listed": (
        "@router.get('/x')\n"
        "async def h(db: Session = Depends(get_db)):\n"
        "    return await unlisted_async_helper(db)\n"
    ),
}

_MUST_STAY_CLEAN = {
    "work offloaded by name and by lambda": (
        "@router.get('/x')\n"
        "async def h(db: Session = Depends(get_db), user=Depends(u)):\n"
        "    row = await run_in_threadpool(load_row, db, 1)\n"
        "    await run_in_threadpool(lambda: require(db, owner=user.id))\n"
        "    await run_in_threadpool(db.close)\n"
        "    return await dial(row)\n"
    ),
    "a nested function handed to the threadpool": (
        "@router.get('/x')\n"
        "async def h(db: Session = Depends(get_db)):\n"
        "    def _read():\n"
        "        return db.query(M).first()\n"
        "    return await run_in_threadpool(_read)\n"
    ),
    "a sync handler is the threadpool already": (
        "@router.get('/x')\ndef h(db: Session = Depends(get_db)):\n    return db.query(M).first()\n"
    ),
    "an async handler with no session": (
        "@router.get('/x')\nasync def h(user=Depends(u)):\n    return await dial(user.id)\n"
    ),
    "a listed helper": (
        "@router.get('/x')\n"
        "async def h(db: Session = Depends(get_db)):\n"
        "    return await listed_helper(db)\n"
    ),
}


def _only_handler(source: str) -> ast.AsyncFunctionDef | None:
    tree = ast.parse(source)
    found = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]
    return found[0] if found and _is_route_handler(found[0]) else None


@pytest.mark.parametrize("case", sorted(_MUST_FIRE))
def test_the_detector_fires(case: str) -> None:
    fn = _only_handler(_MUST_FIRE[case])
    assert fn is not None
    assert session_violations(fn, frozenset({"listed_helper"})), f"did not fire: {case}"


@pytest.mark.parametrize("case", sorted(_MUST_STAY_CLEAN))
def test_the_detector_stays_clean(case: str) -> None:
    fn = _only_handler(_MUST_STAY_CLEAN[case])
    if fn is None:  # a sync handler is out of scope by construction
        assert "def h(" in _MUST_STAY_CLEAN[case]
        return
    assert session_violations(fn, frozenset({"listed_helper"})) == [], case
