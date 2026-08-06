"""Regression guards for issue #284 Phase 2 (A2.4 / A2.5) and issue #320.

An ``async def`` FastAPI handler runs **on the event loop**. If its body is nothing
but blocking work — synchronous SQLAlchemy, a Redis round trip, a yt-dlp metadata
fetch — then every other request served by that process is stalled for the duration.
A plain ``def`` handler is dispatched to Starlette's threadpool instead, so the loop
stays free. Production runs a single uvicorn worker (``app/core/metrics.py`` assumes
one process), so there is no second worker to absorb the stall.

These tests encode that rule for the modules hardened in Phase 2 and issue #320:

1. ``test_no_awaitless_async_handlers`` is the self-maintaining guard: it AST-parses
   each hardened module and fails if a route handler is ``async def`` yet contains no
   ``await`` / ``async for`` / ``async with``. Adding such a handler regresses the fix.
2. ``test_hardened_handlers_are_sync`` pins the specific handlers by name via the
   mounted app, so a rename or a silent revert is caught at the routing layer.
3. ``test_blocking_helpers_are_sync`` covers the blocking helpers that are not route
   handlers, so the AST guard cannot see them.
4. ``test_sse_generators_do_not_call_blocking_helpers_inline`` covers the SSE async
   generators. Making their *handlers* ``def`` does nothing for the generator body:
   Starlette iterates that on the event loop for the whole life of the stream, so a
   synchronous Redis/object-storage call in there blocks far longer than one request.
5. ``test_sse_streams_keep_the_check_subscribe_recheck_order`` pins the lost-wakeup
   fixes (#284 A1.22 for ``download_stream``, #334 for ``bulk_export_stream``), which
   the offload above must not reorder.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from pathlib import Path

import pytest

import app.api.endpoints.admin as admin
import app.api.endpoints.auth.keycloak as auth_keycloak
import app.api.endpoints.auth.methods as auth_methods
import app.api.endpoints.auth.pki as auth_pki
import app.api.endpoints.auth_config as auth_config
import app.api.endpoints.combined_speaker_migration as combined_speaker_migration
import app.api.endpoints.embedding_migration as embedding_migration
import app.api.endpoints.files as files_endpoints
import app.api.endpoints.files.subtitles as subtitles
import app.api.endpoints.files.url_processing as url_processing
import app.api.endpoints.llm_settings as llm_settings
import app.api.endpoints.llm_status as llm_status
import app.api.endpoints.media_collections as media_collections
import app.api.endpoints.speaker_attribute_migration as speaker_attribute_migration
import app.api.endpoints.summarization as summarization
import app.api.endpoints.system as system_endpoints
import app.api.endpoints.tags as tags
import app.api.endpoints.tasks as tasks_endpoints
import app.api.endpoints.topics as topics
import app.api.endpoints.user_files as user_files
import app.api.endpoints.user_settings as user_settings
import app.api.websockets as websockets
import app.main as app_main
import app.utils.file_hash as file_hash
import app.utils.thumbnail as thumbnail

# Modules whose route handlers must never be awaitless coroutines.
HARDENED_MODULES = [
    # Phase 2 (A2.4 / A2.5)
    media_collections,
    topics,
    tasks_endpoints,
    url_processing,
    # Issue #320
    admin,
    auth_config,
    auth_keycloak,
    auth_methods,
    auth_pki,
    combined_speaker_migration,
    embedding_migration,
    files_endpoints,
    llm_settings,
    llm_status,
    speaker_attribute_migration,
    subtitles,
    summarization,
    system_endpoints,
    tags,
    user_files,
    user_settings,
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
        f"in its threadpool (issue #284 A2.5 / #320): {offenders}"
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
    # Issue #320 — heavy aggregate queries, blocking OpenSearch, blocking HTTP probes
    (admin, "admin_search_users"),
    (admin, "get_account_status_report"),
    (admin, "preview_retention_deletion"),
    (admin, "repair_profile_embeddings"),
    (auth_config, "get_all_configs"),
    (auth_config, "update_config_category"),
    (auth_keycloak, "keycloak_login"),
    (auth_methods, "get_auth_methods"),
    (auth_pki, "pki_login"),
    (combined_speaker_migration, "get_combined_migration_status"),
    (embedding_migration, "get_migration_status"),
    (embedding_migration, "start_migration"),
    (files_endpoints, "download_stream"),
    (llm_settings, "test_llm_connection"),
    (llm_settings, "test_active_configuration"),
    (llm_settings, "test_specific_configuration"),
    (llm_status, "test_llm_connection"),
    (speaker_attribute_migration, "get_attribute_migration_status"),
    (subtitles, "get_subtitles"),
    (subtitles, "bulk_export_stream"),
    (summarization, "get_file_summary"),
    (summarization, "search_summaries"),
    (summarization, "delete_summary"),
    (system_endpoints, "get_system_stats"),
    (tags, "add_tag_to_file"),
    (user_files, "request_user_recovery"),
    (user_settings, "get_auto_label_settings"),
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


BLOCKING_HELPERS = [
    # ldap3 binds block for up to the 10 s connect timeout; the caller hands this to
    # ``run_in_threadpool`` because ``test_auth_connection`` must stay a coroutine for
    # the genuinely-async Keycloak branch (issue #320).
    (auth_config, "_test_ldap_connection"),
    # Issue #320 follow-ups — awaitless coroutines outside the endpoint layer.
    # Blocking object storage, dispatched from the lifespan via run_in_threadpool.
    (app_main, "_setup_minio"),
    # Blocking OpenSearch (model-id write + ingest-pipeline PUT).
    (app_main, "_adopt_managed_embedding_model"),
    # Builds a lazily-connecting async pool and schedules a task; nothing to await.
    (websockets, "setup_redis"),
    # In-memory bookkeeping only, mirroring ConnectionManager.disconnect.
    (websockets, "ConnectionManager.connect"),
    # Blocking SQLAlchemy (+ object-storage deletes) on the upload request path.
    (file_hash, "check_duplicate_by_fingerprint"),
    (file_hash, "cleanup_failed_duplicates"),
    # ffmpeg + object-storage PUT; had a byte-identical `async def` twin until #320.
    (thumbnail, "generate_and_upload_thumbnail"),
]


def _resolve(module, dotted: str):
    """Resolve a possibly dotted attribute path (``ConnectionManager.connect``)."""
    target = module
    for part in dotted.split("."):
        target = getattr(target, part)
    return target


@pytest.mark.parametrize(
    ("module", "name"),
    BLOCKING_HELPERS,
    ids=[f"{m.__name__.rsplit('.', 1)[-1]}.{n}" for m, n in BLOCKING_HELPERS],
)
def test_blocking_helpers_are_sync(module, name: str) -> None:
    """Blocking helpers awaited from coroutine handlers must not be coroutines.

    The AST guard only inspects route handlers, so these need pinning by name.
    """
    helper = _resolve(module, name)
    assert not asyncio.iscoroutinefunction(helper), (
        f"{module.__name__}.{name} is a coroutine again; awaiting it never yields, so the "
        f"blocking call runs on the event loop. Keep it `def` and offload it with "
        f"`run_in_threadpool` (issue #320)."
    )


# ---------------------------------------------------------------------------
# SSE async generators
# ---------------------------------------------------------------------------

# (module, handler, generator, names that must never be *called* inside the generator)
SSE_GENERATORS = [
    # ``_ready_frame`` stats object storage and presigns; ``_ensure_prepare_enqueued``
    # is a synchronous Redis SETNX plus a Celery dispatch. Both go through
    # ``run_in_threadpool`` (passed as a bare name, never called inline).
    (
        files_endpoints,
        "download_stream",
        "event_stream",
        {"_ready_frame", "_ensure_prepare_enqueued"},
    ),
    # The result-cache read uses the async client; the synchronous ``get_redis()``
    # singleton must not come back.
    (subtitles, "bulk_export_stream", "event_stream", {"get_redis"}),
]


def _find_function(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """Return the first (async) function definition called ``name`` anywhere in ``tree``."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found")


def _generator_ast(module, handler: str, generator: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(Path(module.__file__).read_text())
    return _find_function(_find_function(tree, handler), generator)


@pytest.mark.parametrize(
    ("module", "handler", "generator", "blocking"),
    SSE_GENERATORS,
    ids=[f"{m.__name__.rsplit('.', 1)[-1]}.{h}" for m, h, _, _ in SSE_GENERATORS],
)
def test_sse_generators_do_not_call_blocking_helpers_inline(
    module, handler: str, generator: str, blocking: set[str]
) -> None:
    """An SSE generator is iterated on the event loop for the life of the stream.

    Converting the handler to ``def`` (PR #329) only moved the few lines that build
    the generator object; a synchronous call *inside* it still stalls every other
    request, and for far longer than one handler would.
    """
    offenders = [
        f"{node.func.id}() at line {node.lineno}"
        for node in ast.walk(_generator_ast(module, handler, generator))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in blocking
    ]

    assert not offenders, (
        f"{module.__name__}.{handler}.{generator} calls blocking code inline: {offenders}. "
        f"Use the async Redis client, or hand the callable to `run_in_threadpool` "
        f"(issue #320)."
    )


def _threadpooled_ready_frame_lines(tree: ast.AST) -> list[int]:
    """Lines of ``run_in_threadpool(_ready_frame)`` — download_stream's readiness check."""
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "run_in_threadpool"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "_ready_frame"
    ]


def _awaited_ready_frame_lines(tree: ast.AST) -> list[int]:
    """Lines of ``ready_frame()`` — bulk_export_stream's async result-cache read."""
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ready_frame"
    ]


# (module, handler, generator, locator for that stream's readiness checks)
READINESS_STRADDLE = [
    (files_endpoints, "download_stream", "event_stream", _threadpooled_ready_frame_lines),
    (subtitles, "bulk_export_stream", "event_stream", _awaited_ready_frame_lines),
]


@pytest.mark.parametrize(
    ("module", "handler", "generator", "find_checks"),
    READINESS_STRADDLE,
    ids=[h for _, h, _, _ in READINESS_STRADDLE],
)
def test_sse_streams_keep_the_check_subscribe_recheck_order(
    module, handler: str, generator: str, find_checks
) -> None:
    """Each SSE stream's readiness check must straddle its pub/sub subscribe.

    Check-then-subscribe alone loses a completion published in the gap and the stream
    hangs forever — #284 A1.22 for ``download_stream``, #334 for ``bulk_export_stream``
    (where the ZIP sat ready in object storage while the browser waited). Neither the
    threadpool offload nor a later refactor may collapse the second check into the first.
    """
    tree = _generator_ast(module, handler, generator)

    checks = find_checks(tree)
    subscribes = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "subscribe"
    ]

    assert len(checks) == 2, f"{handler}: expected two readiness checks, found {checks}"
    assert len(subscribes) == 1, f"{handler}: expected one pubsub.subscribe, found {subscribes}"
    assert checks[0] < subscribes[0] < checks[1], (
        f"{handler}: readiness checks at {checks} no longer straddle subscribe at {subscribes[0]}"
    )
