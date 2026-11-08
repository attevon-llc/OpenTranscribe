"""A streaming route must give its request session back before the stream starts (audit H4).

FastAPI runs the exit half of a ``yield`` dependency only after the response has been
SENT. For a ``StreamingResponse`` that is when the last frame goes out, so a route that
authenticates through ``get_db`` and then streams SSE holds a pooled connection, idle in
a transaction, for the whole stream: a chat reply, a download that is still rendering, a
bulk export that is still zipping. A few dozen open streams then exhaust the pool for
every other request.

The rule this enforces: every route that returns a ``StreamingResponse`` (directly, or
through a helper annotated to return one) and whose dependency tree reaches ``get_db``
takes the session as a parameter and calls ``.close()`` on it. The session is cached per
request, so closing the route's ``db`` closes the one authentication used too.

The check reads FastAPI's own dependency graph (``route.dependant``), so it sees
``get_db`` reached through ``get_current_active_user`` or ``get_current_context``, which a
grep for ``Depends(get_db)`` would miss.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
import typing
from collections.abc import Callable
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.responses import StreamingResponse

from app.db.base import get_db

#: ``module:function`` -> reason. A route here streams a body that is already in memory,
#: so the response is sent (and the session closed) in one pass with no wait in between.
EXEMPT: dict[str, str] = {
    "app.api.endpoints.files.media_delivery:get_thumbnail": (
        "body is an in-memory BytesIO read from MinIO before the response is built; it "
        "is sent in one pass, so the session closes as soon as the bytes are written"
    ),
}


def _returns_streaming(func: Callable[..., Any]) -> bool:
    try:
        hint = typing.get_type_hints(func).get("return")
    except (NameError, TypeError):  # an unresolvable hint is simply not a streaming one
        return False
    return isinstance(hint, type) and issubclass(hint, StreamingResponse)


def _function_ast(func: Callable[..., Any]) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    node = tree.body[0]
    assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    return node


def _streams(endpoint: Callable[..., Any]) -> bool:
    """Whether the endpoint returns a StreamingResponse, by annotation or by what it returns."""
    # Rate-limit decorators wrap the route; the wrapper's globals are the decorator's module.
    endpoint = inspect.unwrap(endpoint)
    if _returns_streaming(endpoint):
        return True
    for node in ast.walk(_function_ast(endpoint)):
        if not (isinstance(node, ast.Return) and isinstance(node.value, ast.Call)):
            continue
        target = node.value.func
        name = target.id if isinstance(target, ast.Name) else None
        resolved = endpoint.__globals__.get(name) if name else None
        if isinstance(resolved, type) and issubclass(resolved, StreamingResponse):
            return True
        if callable(resolved) and _returns_streaming(resolved):
            return True
    return False


def _reaches_get_db(dependant: Any) -> bool:
    return any(dep.call is get_db or _reaches_get_db(dep) for dep in dependant.dependencies)


def _closes_its_session(route: APIRoute) -> bool:
    names = {dep.name for dep in route.dependant.dependencies if dep.call is get_db}
    for node in ast.walk(_function_ast(route.endpoint)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "close"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in names
        ):
            return True
    return False


def _route_key(route: APIRoute) -> str:
    return f"{route.endpoint.__module__}:{route.endpoint.__name__}"


def offenders(app: FastAPI) -> Iterator[str]:
    """Streaming routes that reach ``get_db`` but never close their session."""
    for route in app.routes:
        if not isinstance(route, APIRoute) or _route_key(route) in EXEMPT:
            continue
        if not _streams(route.endpoint) or not _reaches_get_db(route.dependant):
            continue
        if not _closes_its_session(route):
            yield f"{_route_key(route)} ({route.path})"


def _streaming_routes(app: FastAPI) -> list[str]:
    return [
        _route_key(route)
        for route in app.routes
        if isinstance(route, APIRoute) and _streams(route.endpoint)
    ]


def test_every_streaming_route_releases_its_session() -> None:
    from app.main import app

    assert list(offenders(app)) == []


def test_the_real_streaming_routes_are_all_seen() -> None:
    """Vacuity guard: the detector must actually find the routes it exists for."""
    from app.main import app

    seen = set(_streaming_routes(app))
    for expected in (
        "app.api.endpoints.chat.messages:send_message",
        "app.api.endpoints.chat.messages:regenerate",
        "app.api.endpoints.chat.messages:edit_message",
        "app.api.endpoints.files.media_delivery:download_stream",
        "app.api.endpoints.files.subtitles:bulk_export_stream",
    ):
        assert expected in seen, expected


def test_every_exemption_names_a_live_streaming_route() -> None:
    from app.main import app

    seen = set(_streaming_routes(app))
    assert set(EXEMPT) <= seen


# --------------------------------------------------------------------------------------
# Self-test: the detector fires on the bad shape and stays quiet on the good ones.
# --------------------------------------------------------------------------------------


def _user(db=Depends(get_db)):
    return db


def _frames() -> Iterator[str]:
    yield "data: x\n\n"


def _helper_stream() -> StreamingResponse:
    return StreamingResponse(_frames())


def _selftest_app() -> FastAPI:
    router = APIRouter()

    @router.get("/held-via-auth")
    def held_via_auth(user=Depends(_user)) -> StreamingResponse:
        return StreamingResponse(_frames())

    @router.get("/held-via-helper")
    def held_via_helper(db=Depends(get_db)):
        return _helper_stream()

    @router.get("/released")
    def released(db=Depends(get_db), user=Depends(_user)) -> StreamingResponse:
        db.close()
        return StreamingResponse(_frames())

    @router.get("/no-session")
    def no_session() -> StreamingResponse:
        return StreamingResponse(_frames())

    @router.get("/plain")
    def plain(db=Depends(get_db)) -> dict:
        return {}

    app = FastAPI()
    app.include_router(router)
    return app


def test_selftest_fires_on_a_held_session() -> None:
    found = sorted(offenders(_selftest_app()))
    assert [f.split(" ")[1] for f in found] == ["(/held-via-auth)", "(/held-via-helper)"]


def test_selftest_stays_quiet_on_released_sessionless_and_plain_routes() -> None:
    found = " ".join(offenders(_selftest_app()))
    for clean in ("/released", "/no-session", "/plain"):
        assert f"({clean})" not in found
