"""Regression guards for issue #284 Phase 2 (A2.4 / A2.5).

An ``async def`` FastAPI handler runs **on the event loop**. If its body is nothing
but blocking work — synchronous SQLAlchemy, a Redis round trip, a yt-dlp metadata
fetch — then every other request served by that process is stalled for the duration.
A plain ``def`` handler is dispatched to Starlette's threadpool instead, so the loop
stays free.

These tests encode that rule for the modules hardened in Phase 2:

1. ``test_no_awaitless_async_handlers`` is the self-maintaining guard: it AST-parses
   each hardened module and fails if a route handler is ``async def`` yet contains no
   ``await`` / ``async for`` / ``async with``. Adding such a handler regresses the fix.
2. ``test_hardened_handlers_are_sync`` pins the specific handlers by name via the
   mounted app, so a rename or a silent revert is caught at the routing layer.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path

import pytest

import app.api.endpoints.files.url_processing as url_processing
import app.api.endpoints.media_collections as media_collections
import app.api.endpoints.tasks as tasks_endpoints
import app.api.endpoints.topics as topics

# Modules whose route handlers must never be awaitless coroutines.
HARDENED_MODULES = [
    media_collections,
    topics,
    tasks_endpoints,
    url_processing,
]

_ROUTE_METHODS = (
    "get",
    "post",
    "put",
    "patch",
    "delete",
    "head",
    "options",
)


class _AwaitFinder(ast.NodeVisitor):
    """Collect awaits in a function body, without descending into nested functions."""

    def __init__(self) -> None:
        self.found: list[str] = []

    def visit_Await(self, node: ast.Await) -> None:  # noqa: N802 - ast API
        self.found.append(f"await@{node.lineno}")
        self.generic_visit(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:  # noqa: N802 - ast API
        self.found.append(f"async for@{node.lineno}")
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:  # noqa: N802 - ast API
        self.found.append(f"async with@{node.lineno}")
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802 - ast API
        return  # nested function: its awaits belong to it, not the handler

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802 - ast API
        return


def _is_route_handler(node: ast.AsyncFunctionDef) -> bool:
    """True when the function carries a ``@router.<method>(...)`` decorator."""
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        source = ast.unparse(target)
        if source.startswith("router.") and source.split(".", 1)[1] in _ROUTE_METHODS:
            return True
    return False


@pytest.mark.parametrize("module", HARDENED_MODULES, ids=lambda m: m.__name__)
def test_no_awaitless_async_handlers(module) -> None:
    """No route handler in a hardened module may be an ``async def`` with no ``await``."""
    source = Path(module.__file__).read_text()
    tree = ast.parse(source)

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or not _is_route_handler(node):
            continue
        finder = _AwaitFinder()
        for statement in node.body:
            finder.visit(statement)
        if not finder.found:
            offenders.append(f"{node.name} (line {node.lineno})")

    assert not offenders, (
        f"{module.__name__}: these handlers are `async def` but never await — they block "
        f"the event loop with synchronous I/O. Declare them `def` so Starlette runs them "
        f"in its threadpool (issue #284 A2.5): {offenders}"
    )


HARDENED_HANDLERS = [
    # A2.4 — yt-dlp metadata fetch + Redis rate limiting
    (url_processing, "process_media_url"),
    (url_processing, "get_youtube_download_quota"),
    # A2.5 — sync SQLAlchemy in async handlers
    (media_collections, "list_collections"),
    (media_collections, "get_collection_media"),
    (media_collections, "list_shared_collections"),
    (media_collections, "create_collection_share"),
    (topics, "batch_extract_topics"),
    (topics, "get_topic_suggestions"),
    (topics, "extract_topics"),
    (tasks_endpoints, "task_system_health"),
    (tasks_endpoints, "recover_all_stuck_tasks"),
    (tasks_endpoints, "retry_file_processing"),
]


@pytest.mark.parametrize(
    ("module", "name"),
    HARDENED_HANDLERS,
    ids=[f"{m.__name__.rsplit('.', 1)[-1]}.{n}" for m, n in HARDENED_HANDLERS],
)
def test_hardened_handlers_are_sync(module, name: str) -> None:
    """Each hardened handler stays a plain function so FastAPI threadpools it."""
    handler = getattr(module, name)
    assert not asyncio.iscoroutinefunction(handler), (
        f"{module.__name__}.{name} was converted back to `async def`; its body is blocking "
        f"I/O and would run on the event loop again (issue #284 A2.4/A2.5)."
    )
    assert inspect.isfunction(handler)


def test_hardened_handlers_are_sync_on_the_mounted_app(client) -> None:
    """The routes actually mounted on the app resolve to the sync handlers.

    Matching is by function identity, not by name: ``retry_file_processing`` exists in
    both ``endpoints/tasks.py`` and ``endpoints/user_files.py``, and only the former is
    in scope here.
    """
    wanted = {
        getattr(module, name): f"{module.__name__}.{name}" for module, name in HARDENED_HANDLERS
    }
    seen = set()

    for route in client.app.routes:
        endpoint = getattr(route, "endpoint", None)
        label = wanted.get(endpoint)
        if label is None:
            continue
        seen.add(label)
        assert not asyncio.iscoroutinefunction(endpoint), (
            f"Mounted route {route.path} -> {label} is a coroutine function."
        )

    missing = set(wanted.values()) - seen
    assert not missing, f"handlers missing from the mounted app: {sorted(missing)}"
